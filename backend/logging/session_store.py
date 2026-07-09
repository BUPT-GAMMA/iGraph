from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

_MESSAGE_LIST = "message_list.jsonl"
_SESSION_META = "session_meta.json"
_TRACE_FILE = "trace.jsonl"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe_read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class SessionStore:
    """会话持久化存储：消息列表、元数据与调试 trace。"""

    def __init__(self, logs_dir: str) -> None:
        self.logs_dir = logs_dir
        os.makedirs(self.logs_dir, exist_ok=True)
        self._locks: Dict[str, threading.RLock] = {}
        self._global_lock = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = threading.RLock()
            return self._locks[session_id]

    def ensure_session_dir(self, session_id: str) -> str:
        path = os.path.join(self.logs_dir, session_id)
        os.makedirs(path, exist_ok=True)
        return path

    def message_list_path(self, session_id: str) -> str:
        return os.path.join(self.ensure_session_dir(session_id), _MESSAGE_LIST)

    def session_meta_path(self, session_id: str) -> str:
        return os.path.join(self.ensure_session_dir(session_id), _SESSION_META)

    def trace_path(self, session_id: str) -> str:
        return os.path.join(self.ensure_session_dir(session_id), _TRACE_FILE)

    def plan_path(self, session_id: str, plan_id: str) -> str:
        return os.path.join(self.ensure_session_dir(session_id), f"plan_{plan_id}.json")

    def report_path(self, session_id: str, plan_id: str) -> str:
        return os.path.join(self.ensure_session_dir(session_id), f"report_{plan_id}.json")

    def load_meta(self, session_id: str) -> dict:
        return _safe_read_json(self.session_meta_path(session_id))

    def update_meta(self, session_id: str, patch: dict[str, Any]) -> dict:
        now = _now_iso()
        with self._lock_for(session_id):
            meta_path = self.session_meta_path(session_id)
            meta = _safe_read_json(meta_path)
            if not meta:
                meta = {
                    "session_id": session_id,
                    "created_at": now,
                    "updated_at": now,
                    "provider": "",
                    "model": "",
                    "reasoning_mode": "direct",
                    "title": "新会话",
                    "last_message_preview": "",
                    "message_count": 0,
                    "visible_message_count": 0,
                    "last_seq": 0,
                    "archived": False,
                    "last_has_graph": False,
                    "user_id": "",
                }
            meta.update(patch)
            meta["updated_at"] = now
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            return meta

    def append_message_record(self, session_id: str, record: dict[str, Any]) -> dict:
        now = _now_iso()
        with self._lock_for(session_id):
            meta = self.load_meta(session_id)
            seq = int(meta.get("last_seq", 0)) + 1
            record = dict(record)
            record.setdefault("seq", seq)
            record.setdefault("ts", now)
            record.setdefault("session_id", session_id)

            path = self.message_list_path(session_id)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

            patch: dict[str, Any] = {
                "last_seq": record["seq"],
                "message_count": int(meta.get("message_count", 0)) + 1,
                "archived": False,
            }
            if record.get("visible"):
                patch["visible_message_count"] = int(meta.get("visible_message_count", 0)) + 1
                display_text = str(record.get("display_content") or "").strip()
                msg = record.get("message")
                if not display_text and isinstance(msg, dict):
                    display_text = str(msg.get("content") or "").strip()
                if display_text:
                    patch["last_message_preview"] = display_text[:120]
                    if not str(meta.get("title") or "").strip() or meta.get("title") == "新会话":
                        if record.get("message", {}).get("role") == "user":
                            patch["title"] = display_text[:24]
            self.update_meta(session_id, patch)
            return record

    def compact_context_records(
        self,
        session_id: str,
        summary_message: dict[str, Any],
        *,
        stats: dict[str, Any] | None = None,
        preserve_recent_user_turns: int = 3,
    ) -> dict[str, Any]:
        """
        Rewrite context participation after short-term compression.

        Visible chat records stay visible, but older records no longer
        participate in future context restoration. A hidden summary record is
        inserted before the preserved recent user-turn window so restored
        message order matches the live runtime order:
        summary -> recent completed turns -> latest user turn.
        """
        now = _now_iso()
        with self._lock_for(session_id):
            records = self.load_message_records(session_id)
            context_indexes = [
                i
                for i, rec in enumerate(records)
                if rec.get("include_in_context", True)
            ]
            def _is_real_user_turn_record(idx: int) -> bool:
                rec = records[idx]
                msg = rec.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    return False
                if str(rec.get("kind") or "") == "context_compression":
                    return False
                content = str(msg.get("content") or "").lstrip()
                return not content.startswith("[CONTEXT_SUMMARY")

            real_user_indexes = [
                i
                for i in context_indexes
                if _is_real_user_turn_record(i)
            ]
            if real_user_indexes:
                latest_user_idx = real_user_indexes[-1]
                previous_user_indexes = [i for i in real_user_indexes[:-1] if i < latest_user_idx]
                keep_turns = max(int(preserve_recent_user_turns or 0), 0)
                if keep_turns and previous_user_indexes:
                    preserve_start = previous_user_indexes[-keep_turns]
                else:
                    preserve_start = latest_user_idx
                preserved = {i for i in context_indexes if i >= preserve_start}
            else:
                preserved = set()

            for i in context_indexes:
                if i in preserved:
                    continue
                # Never strip the system message — session restore depends on it
                if (
                    isinstance(records[i].get("message"), dict)
                    and records[i]["message"].get("role") == "system"
                ):
                    continue
                records[i]["include_in_context"] = False
                records[i]["compacted_at"] = now

            seq = max([int(rec.get("seq", 0) or 0) for rec in records] or [0]) + 1
            summary_record = {
                "seq": seq,
                "ts": now,
                "session_id": session_id,
                "message": summary_message,
                "visible": False,
                "display_content": "",
                "kind": "context_compression",
                "include_in_context": True,
                "compression": dict(stats or {}),
            }
            insert_at = min(preserved) if preserved else len(records)
            records.insert(insert_at, summary_record)

            path = self.message_list_path(session_id)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)

            visible_count = sum(1 for rec in records if rec.get("visible"))
            self.update_meta(
                session_id,
                {
                    "last_seq": seq,
                    "message_count": len(records),
                    "visible_message_count": visible_count,
                    "short_term_context_tokens": int((stats or {}).get("after_tokens") or 0),
                    "last_context_compressed_at": now,
                    "archived": False,
                },
            )
            return summary_record

    def append_trace_record(self, session_id: str, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("ts", _now_iso())
        record.setdefault("session_id", session_id)
        path = self.trace_path(session_id)
        with self._lock_for(session_id):
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_plan(self, session_id: str, plan_id: str, payload: dict[str, Any]) -> None:
        path = self.plan_path(session_id, plan_id)
        with self._lock_for(session_id):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    def save_report(self, session_id: str, plan_id: str, payload: dict[str, Any]) -> None:
        path = self.report_path(session_id, plan_id)
        with self._lock_for(session_id):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    def list_sessions(self, limit: int = 200, user_id: str = "") -> List[dict]:
        sessions: List[dict] = []
        try:
            names = os.listdir(self.logs_dir)
        except Exception:
            return []
        for name in names:
            session_dir = os.path.join(self.logs_dir, name)
            if not os.path.isdir(session_dir):
                continue
            meta = self.load_meta(name)
            if not meta:
                message_path = os.path.join(session_dir, _MESSAGE_LIST)
                if not os.path.exists(message_path):
                    continue
                meta = {
                    "session_id": name,
                    "created_at": datetime.fromtimestamp(
                        os.path.getctime(message_path), tz=timezone.utc
                    ).astimezone().isoformat(timespec="milliseconds"),
                    "updated_at": datetime.fromtimestamp(
                        os.path.getmtime(message_path), tz=timezone.utc
                    ).astimezone().isoformat(timespec="milliseconds"),
                    "provider": "",
                    "model": "",
                    "reasoning_mode": "direct",
                    "title": "历史会话",
                    "last_message_preview": "",
                    "message_count": 0,
                    "visible_message_count": 0,
                    "last_seq": 0,
                    "archived": True,
                    "last_has_graph": False,
                }
            sessions.append(meta)
        # 过滤掉已被用户显式删除的会话
        sessions = [s for s in sessions if not s.get("deleted")]
        # 按用户过滤：无 user_id 的旧会话对所有用户可见
        if user_id:
            sessions = [
                s for s in sessions
                if not s.get("user_id") or s.get("user_id") == user_id
            ]
        # 按 updated_at 倒序（最近活跃在前）
        sessions.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return sessions[: max(limit, 1)]

    def load_message_records(self, session_id: str) -> List[dict]:
        records: List[dict] = []
        path = self.message_list_path(session_id)
        if not os.path.exists(path):
            return records
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
        return records

    def truncate_before_last_visible_user_turn(self, session_id: str) -> List[dict]:
        with self._lock_for(session_id):
            records = self.load_message_records(session_id)
            cut_idx = None
            for idx in range(len(records) - 1, -1, -1):
                rec = records[idx]
                msg = rec.get("message")
                if (
                    rec.get("visible")
                    and isinstance(msg, dict)
                    and msg.get("role") == "user"
                ):
                    cut_idx = idx
                    break
            if cut_idx is None:
                return records
            kept = records[:cut_idx]
            path = self.message_list_path(session_id)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for rec in kept:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)
            self._rebuild_meta_after_truncate(session_id, kept)
            return kept

    def graph_version_effects_after_last_visible_user_turn(self, session_id: str) -> List[dict]:
        records = self.load_message_records(session_id)
        cut_idx = None
        for idx in range(len(records) - 1, -1, -1):
            rec = records[idx]
            msg = rec.get("message")
            if (
                rec.get("visible")
                and isinstance(msg, dict)
                and msg.get("role") == "user"
            ):
                cut_idx = idx
                break
        if cut_idx is None:
            return []

        effects: List[dict] = []
        for rec in records[cut_idx:]:
            tool_groups = [rec.get("tools")]
            solver_steps = rec.get("solver_steps")
            if isinstance(solver_steps, list):
                tool_groups.extend(step.get("tools") for step in solver_steps if isinstance(step, dict))
            for tools in tool_groups:
                if not isinstance(tools, list):
                    continue
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    effect = tool.get("graph_version_effect")
                    if isinstance(effect, dict):
                        effects.append(dict(effect))
        return effects

    def _rebuild_meta_after_truncate(self, session_id: str, records: List[dict]) -> None:
        meta = self.load_meta(session_id)
        last_seq = int(records[-1].get("seq", 0)) if records else 0
        visible_records = [rec for rec in records if rec.get("visible")]
        preview = ""
        title = str(meta.get("title") or "")
        for rec in reversed(visible_records):
            display_text = str(rec.get("display_content") or "").strip()
            msg = rec.get("message")
            if not display_text and isinstance(msg, dict):
                display_text = str(msg.get("content") or "").strip()
            if display_text:
                preview = display_text[:120]
                break
        if not title or title == "新会话":
            for rec in visible_records:
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    display_text = str(rec.get("display_content") or msg.get("content") or "").strip()
                    if display_text:
                        title = display_text[:24]
                        break
        self.update_meta(
            session_id,
            {
                "last_seq": last_seq,
                "message_count": len(records),
                "visible_message_count": len(visible_records),
                "last_message_preview": preview,
                "title": title or "新会话",
                "archived": False,
            },
        )

    def load_trace_records(self, session_id: str) -> List[dict]:
        records: List[dict] = []
        path = self.trace_path(session_id)
        if not os.path.exists(path):
            return records
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
        return records

    def load_context_messages(self, session_id: str) -> List[dict]:
        messages: List[dict] = []
        for rec in self.load_message_records(session_id):
            if not rec.get("include_in_context", True):
                continue
            msg = rec.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("role"), str):
                messages.append(msg)
        if not messages:
            return messages
        latest_system = None
        for msg in messages:
            if msg.get("role") == "system":
                latest_system = msg
        if latest_system is None:
            return messages
        non_system = [m for m in messages if m.get("role") != "system"]
        return [latest_system, *non_system]

    def load_chat_messages(self, session_id: str) -> List[dict]:
        rows: List[dict] = []
        trace_assistant_meta = self._rebuild_assistant_meta_from_trace(session_id)
        assistant_turn_index = 0
        for rec in self.load_message_records(session_id):
            if not rec.get("visible"):
                continue
            if str(rec.get("kind") or "") == "user_feedback":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = str(rec.get("display_content") or "").strip()
            if not content:
                content = str(msg.get("content") or "")
            row = {
                "role": role,
                "content": content,
                "ts": rec.get("ts"),
                "seq": rec.get("seq"),
            }
            if role == "assistant":
                fallback_meta = (
                    trace_assistant_meta[assistant_turn_index]
                    if assistant_turn_index < len(trace_assistant_meta)
                    else {}
                )
                assistant_turn_index += 1
                tools = rec.get("tools")
                merged_tools = self._merge_tool_lists(tools, fallback_meta.get("tools"))
                if merged_tools:
                    row["tools"] = merged_tools
                thinking = rec.get("thinking")
                if isinstance(thinking, list) and thinking:
                    row["thinking"] = thinking
                elif (
                    isinstance(fallback_meta.get("thinking"), list)
                    and fallback_meta.get("thinking")
                ):
                    row["thinking"] = fallback_meta["thinking"]
                plan = rec.get("plan")
                if isinstance(plan, dict) and plan:
                    row["plan"] = plan
                solver_steps = rec.get("solver_steps")
                if isinstance(solver_steps, list) and solver_steps:
                    row["solverSteps"] = solver_steps
                feedback_request = rec.get("feedback_request")
                if isinstance(feedback_request, dict) and feedback_request:
                    row["feedback_request"] = feedback_request
                progress_items = rec.get("progress_items")
                if isinstance(progress_items, list) and progress_items:
                    row["progress_items"] = progress_items
                graphs = rec.get("graphs")
                if isinstance(graphs, list) and graphs:
                    row["graphs"] = graphs
                reports = rec.get("reports")
                if isinstance(reports, list) and reports:
                    row["reports"] = reports
            rows.append(row)
        return self._merge_assistant_continuations(rows)

    @staticmethod
    def _merge_assistant_continuations(rows: List[dict]) -> List[dict]:
        merged: List[dict] = []
        for row in rows:
            if (
                merged
                and merged[-1].get("role") == "assistant"
                and row.get("role") == "assistant"
            ):
                prev = merged[-1]
                prev_content = str(prev.get("content") or "")
                row_content = str(row.get("content") or "")
                if prev_content and row_content:
                    prev["content"] = f"{prev_content}\n\n{row_content}"
                elif row_content:
                    prev["content"] = row_content
                if isinstance(row.get("tools"), list) and row.get("tools"):
                    prev["tools"] = SessionStore._merge_tool_lists(
                        prev.get("tools"),
                        row.get("tools"),
                    )
                if isinstance(row.get("thinking"), list) and row.get("thinking"):
                    prev["thinking"] = row["thinking"]
                if isinstance(row.get("solverSteps"), list) and row.get("solverSteps"):
                    prev["solverSteps"] = SessionStore._merge_solver_steps(
                        prev.get("solverSteps"),
                        row.get("solverSteps"),
                    )
                if isinstance(row.get("plan"), dict) and row.get("plan"):
                    prev["plan"] = row["plan"]
                if isinstance(row.get("progress_items"), list) and row.get("progress_items"):
                    prev["progress_items"] = row["progress_items"]
                if isinstance(row.get("graphs"), list) and row.get("graphs"):
                    prev_graphs = prev.get("graphs") if isinstance(prev.get("graphs"), list) else []
                    prev["graphs"] = [*prev_graphs, *row["graphs"]]
                if isinstance(row.get("reports"), list) and row.get("reports"):
                    prev["reports"] = SessionStore._merge_report_lists(
                        prev.get("reports"),
                        row.get("reports"),
                    )
                feedback_request = row.get("feedback_request")
                if isinstance(feedback_request, dict) and feedback_request:
                    attached = SessionStore._attach_feedback_to_solver_step(
                        prev,
                        feedback_request,
                    )
                    if not attached:
                        prev["feedback_request"] = feedback_request
                continue
            merged.append(row)
        return merged

    @staticmethod
    def _normalize_tool_item(value: Any) -> dict | None:
        if not isinstance(value, dict):
            return None
        name = str(value.get("tool") or value.get("name") or "").strip()
        if not name:
            return None
        item: dict[str, Any] = {"tool": name}
        if "done" in value:
            item["done"] = bool(value.get("done"))
        else:
            item["done"] = True
        args = value.get("args", value.get("arguments"))
        if isinstance(args, dict):
            item["args"] = args
        call_id = value.get("call_id") or value.get("id")
        if call_id:
            item["call_id"] = str(call_id)
        server = str(value.get("server") or "").strip()
        if server:
            item["server"] = server
        progress = value.get("progress")
        if isinstance(progress, dict):
            item["progress"] = progress
        return item

    @staticmethod
    def _merge_tool_lists(primary: Any, fallback: Any) -> List[dict]:
        primary_tools = [
            item
            for item in (
                SessionStore._normalize_tool_item(tool)
                for tool in (primary if isinstance(primary, list) else [])
            )
            if item is not None
        ]
        fallback_tools = [
            item
            for item in (
                SessionStore._normalize_tool_item(tool)
                for tool in (fallback if isinstance(fallback, list) else [])
            )
            if item is not None
        ]
        if not primary_tools:
            return fallback_tools
        if not fallback_tools:
            return primary_tools

        used: set[int] = set()
        merged: List[dict] = []
        for index, tool in enumerate(primary_tools):
            match_index = -1
            call_id = str(tool.get("call_id") or "")
            if call_id:
                match_index = next(
                    (
                        i
                        for i, candidate in enumerate(fallback_tools)
                        if i not in used
                        and str(candidate.get("call_id") or "") == call_id
                        and candidate.get("tool") == tool.get("tool")
                    ),
                    -1,
                )
            if match_index < 0 and index < len(fallback_tools):
                candidate = fallback_tools[index]
                if candidate.get("tool") == tool.get("tool") and index not in used:
                    match_index = index
            if match_index < 0:
                match_index = next(
                    (
                        i
                        for i, candidate in enumerate(fallback_tools)
                        if i not in used and candidate.get("tool") == tool.get("tool")
                    ),
                    -1,
                )

            if match_index >= 0:
                used.add(match_index)
                merged.append({**fallback_tools[match_index], **tool})
            else:
                merged.append(tool)

        for i, tool in enumerate(fallback_tools):
            if i not in used:
                merged.append(tool)
        return merged

    @staticmethod
    def _merge_report_lists(primary: Any, incoming: Any) -> List[dict]:
        merged: List[dict] = []
        seen_ids: set[str] = set()
        for source in (primary, incoming):
            if not isinstance(source, list):
                continue
            for report in source:
                if not isinstance(report, dict):
                    continue
                report_id = str(report.get("id") or "").strip()
                if report_id and report_id in seen_ids:
                    continue
                merged.append(dict(report))
                if report_id:
                    seen_ids.add(report_id)
        return merged

    @staticmethod
    def _feedback_step_id(request: dict) -> int | None:
        try:
            raw = request.get("step_id", request.get("stepId"))
            if raw is None or raw == "":
                return None
            return int(raw)
        except Exception:
            return None

    @staticmethod
    def _normalize_feedback_requests(value: Any) -> List[dict]:
        if isinstance(value, list):
            return [dict(v) for v in value if isinstance(v, dict)]
        if isinstance(value, dict):
            return [dict(value)]
        return []

    @staticmethod
    def _merge_feedback_requests(*groups: Any) -> List[dict]:
        merged: List[dict] = []
        for group in groups:
            for req in SessionStore._normalize_feedback_requests(group):
                request_id = str(req.get("request_id") or "")
                existing = (
                    next((item for item in merged if str(item.get("request_id") or "") == request_id), None)
                    if request_id
                    else None
                )
                if existing is not None:
                    existing.update(req)
                else:
                    merged.append(req)
        return merged

    @staticmethod
    def _merge_solver_steps(existing_steps: Any, incoming_steps: Any) -> List[dict]:
        merged: List[dict] = []
        for step in existing_steps if isinstance(existing_steps, list) else []:
            if isinstance(step, dict):
                item = dict(step)
                item["feedbackRequests"] = SessionStore._merge_feedback_requests(
                    item.get("feedbackRequests") or item.get("feedback_requests"),
                    item.get("feedbackRequest") or item.get("feedback_request"),
                )
                item.pop("feedbackRequest", None)
                item.pop("feedback_request", None)
                merged.append(item)
        for step in incoming_steps if isinstance(incoming_steps, list) else []:
            if not isinstance(step, dict):
                continue
            item = dict(step)
            item["feedbackRequests"] = SessionStore._merge_feedback_requests(
                item.get("feedbackRequests") or item.get("feedback_requests"),
                item.get("feedbackRequest") or item.get("feedback_request"),
            )
            item.pop("feedbackRequest", None)
            item.pop("feedback_request", None)
            idx = next(
                (i for i, prev in enumerate(merged) if prev.get("step_id") == item.get("step_id")),
                -1,
            )
            if idx < 0:
                merged.append(item)
                continue
            item["feedbackRequests"] = SessionStore._merge_feedback_requests(
                merged[idx].get("feedbackRequests"),
                item.get("feedbackRequests"),
            )
            merged[idx].update(item)
        return merged

    @staticmethod
    def _attach_feedback_to_solver_step(row: dict, feedback_request: dict) -> bool:
        step_id = SessionStore._feedback_step_id(feedback_request)
        if step_id is None:
            return False
        solver_steps = row.get("solverSteps")
        if not isinstance(solver_steps, list):
            return False
        for step in solver_steps:
            if not isinstance(step, dict) or int(step.get("step_id", -1)) != step_id:
                continue
            step["feedbackRequests"] = SessionStore._merge_feedback_requests(
                step.get("feedbackRequests") or step.get("feedback_requests"),
                step.get("feedbackRequest") or step.get("feedback_request"),
                feedback_request,
            )
            step.pop("feedbackRequest", None)
            step.pop("feedback_request", None)
            return True
        return False

    def _rebuild_assistant_meta_from_trace(self, session_id: str) -> List[dict]:
        """
        兼容旧日志：从 trace.jsonl 重建每个 assistant 可见回复对应的 thinking/tools。
        """
        turns: List[dict] = []
        current_thinking: List[dict] = []
        current_tools: List[dict] = []

        def _append_thinking(phase: str, content: str) -> None:
            text = str(content or "")
            if not text.strip():
                return
            normalized_phase = str(phase or "thinking").strip() or "thinking"
            if (
                current_thinking
                and isinstance(current_thinking[-1], dict)
                and str(current_thinking[-1].get("phase")) == normalized_phase
            ):
                current_thinking[-1]["text"] = str(current_thinking[-1].get("text") or "") + text
                return
            current_thinking.append(
                {"phase": normalized_phase, "text": text, "streaming": False}
            )

        def _mark_tool_done(name: str, call_id: str = "") -> None:
            if call_id:
                for item in current_tools:
                    if (
                        str(item.get("tool")) == name
                        and str(item.get("call_id") or "") == call_id
                    ):
                        item["done"] = True
                        return
            for item in current_tools:
                if str(item.get("tool")) == name and not bool(item.get("done")):
                    item["done"] = True
                    return

        def _find_tool_for_raw(name: str, call_id: str) -> dict | None:
            if call_id:
                for item in current_tools:
                    if (
                        str(item.get("tool")) == name
                        and str(item.get("call_id") or "") == call_id
                    ):
                        return item
            for item in reversed(current_tools):
                if str(item.get("tool")) == name and not bool(item.get("done")):
                    return item
            for item in reversed(current_tools):
                if str(item.get("tool")) == name:
                    return item
            return None

        for rec in self.load_trace_records(session_id):
            event = str(rec.get("event") or "").strip()
            if event == "THINKING":
                _append_thinking(str(rec.get("phase") or "thinking"), str(rec.get("content") or ""))
                continue

            if event == "TOOL_CALL":
                tool_name = str(rec.get("tool") or "").strip()
                if tool_name:
                    item: dict[str, Any] = {"tool": tool_name, "done": False}
                    args = rec.get("args", rec.get("arguments"))
                    if isinstance(args, dict):
                        item["args"] = args
                    call_id = rec.get("call_id") or rec.get("id")
                    if call_id:
                        item["call_id"] = str(call_id)
                    current_tools.append(item)
                continue

            if event == "TOOL_EXEC_RAW":
                tool_name = str(rec.get("tool") or "").strip()
                if tool_name:
                    call_id = str(rec.get("call_id") or "")
                    item = _find_tool_for_raw(tool_name, call_id)
                    if item is None:
                        item = {"tool": tool_name, "done": False}
                        current_tools.append(item)
                    if call_id:
                        item["call_id"] = call_id
                    args = rec.get("arguments", rec.get("args"))
                    if isinstance(args, dict):
                        item["args"] = args
                continue

            if event == "TOOL_RESULT":
                tool_name = str(rec.get("tool") or "").strip()
                if tool_name:
                    _mark_tool_done(tool_name, str(rec.get("call_id") or ""))
                continue

            if event == "MODEL_RESPONSE_PARSED":
                final_answer = str(rec.get("final_answer") or "").strip()
                if not final_answer:
                    continue
                turns.append(
                    {
                        "thinking": list(current_thinking),
                        "tools": [
                            {
                                key: value
                                for key, value in {
                                    "tool": str(t.get("tool") or ""),
                                    "args": t.get("args") if isinstance(t.get("args"), dict) else None,
                                    "call_id": str(t.get("call_id") or ""),
                                    "server": str(t.get("server") or ""),
                                    "done": bool(t.get("done", True)),
                                }.items()
                                if value not in (None, "")
                            }
                            for t in current_tools
                            if str(t.get("tool") or "").strip()
                        ],
                    }
                )
                current_thinking = []
                current_tools = []
        return turns

    def session_exists(self, session_id: str) -> bool:
        session_dir = os.path.join(self.logs_dir, session_id)
        if not os.path.isdir(session_dir):
            return False
        if os.path.exists(self.session_meta_path(session_id)):
            return True
        return os.path.exists(self.message_list_path(session_id))
