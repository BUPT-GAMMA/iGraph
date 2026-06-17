from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, TYPE_CHECKING, Tuple

import backend.data_paths as _dp
from backend.agent.provider_gateway import ProviderGateway
from backend.agent.memory.paths import MemoryPathResolver
from backend.agent.memory.safety import scan_memory_content
from backend.agent.memory.store import append_jsonl, atomic_json_write
from backend.agent.sub_agents.memory import MemoryUpdateSubAgent
from backend.logging.session_store import SessionStore

if TYPE_CHECKING:
    from backend.logging.logger import SessionLogger


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _clamp_confidence(value: Any, default: float = 0.75) -> float:
    try:
        v = float(value)
    except Exception:
        v = default
    if v < 0.05:
        return 0.05
    if v > 0.99:
        return 0.99
    return round(v, 2)


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.split())


def _extract_terms(text: str) -> List[str]:
    import re

    if not text:
        return []
    terms = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text)
    dedup: List[str] = []
    seen = set()
    for t in terms:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(key)
    return dedup


def _problem_type_similarity(a: str, b: str) -> float:
    """Similarity between two problem_type strings for fuzzy dedup.

    Uses Jaccard on CJK characters + ASCII tokens.
    When one string's tokens are a subset of the other (e.g. "最短路径" inside
    "无向图最短路径"), applies a containment score to catch near-subset cases.
    """
    import re as _re

    def _tokens(text: str) -> set:
        cjk = set(_re.findall(r"[一-鿿]", text))
        eng = set(t.lower() for t in _re.findall(r"[A-Za-z0-9_]{2,}", text))
        return cjk | eng

    sa, sb = _tokens(a), _tokens(b)
    if not sa or not sb:
        return 0.0
    jaccard = len(sa & sb) / len(sa | sb)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(shorter) >= 3 and shorter <= longer:
        return max(jaccard, 0.75)
    return jaccard


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


_DEDUP_MIN_PLAYBOOKS = 10
_DEDUP_INSERT_INTERVAL = 5


class MemoryEngine:
    """
    长期记忆子代理（memory sub-agent）。

    核心策略：
    - 每轮都评估（auto evaluate）
    - 仅在有价值时写入（selective write）
    - 结构化增删改（upsert/delete）
    - 持久化到 backend/agent/memory/default_user/
    """

    def __init__(self, logs_dir: str, session_store: SessionStore, user_id: str = "") -> None:
        self.logs_dir = logs_dir
        self.store = session_store

        effective_user = user_id.strip()
        if effective_user:
            memory_dir = _dp.user_memory_dir(effective_user)
        else:
            # Fallback: anonymous / legacy path inside the package directory.
            agent_dir = os.path.dirname(os.path.abspath(__file__))
            memory_dir = _dp.data_root() / "users" / "default_user" / "memory"
            # Keep reading from the old location if the new one is still empty.
            _legacy_dir = os.path.join(agent_dir, "default_user")
            if not memory_dir.exists() and os.path.isdir(_legacy_dir):
                memory_dir = type(memory_dir)(_legacy_dir)  # stay as Path

        memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir = str(memory_dir)
        self._nested_legacy_memory_dir = ""  # no longer used

        self.snapshot_path = str(memory_dir / "long_term_memory.json")
        self.events_path = str(memory_dir / "memory_events.jsonl")
        self.playbooks_path = str(memory_dir / "playbooks.json")
        self.user_profile_path = str(memory_dir / "user_profile.json")
        # Global playbooks path — merged at load_and_freeze time.
        self._global_playbooks_path = str(_dp.global_playbooks_path())

        self._lock = threading.RLock()
        self._step_lock = threading.Lock()
        self._memory_step_seq = 0
        self._snapshot_block = ""
        self._snapshot_frozen = False
        self._memory_sub_agent = MemoryUpdateSubAgent()
        self._dedup_insert_count = 0

        self._migrate_nested_memory_dir_if_needed()
        self._migrate_legacy_memory_if_needed()

    def load_and_freeze(self) -> str:
        with self._lock:
            self._snapshot_block = self._build_full_snapshot_block()
            self._snapshot_frozen = True
            return self._snapshot_block

    def refresh_snapshot(self) -> Tuple[bool, str]:
        """Rebuild the frozen snapshot from disk and update the cached
        block. Returns ``(changed, new_block)``. ``changed=False`` means
        callers can skip prompt rebuilds and keep prefix-cache hits.

        Call this between user turns (after the memory sub-agent has
        committed) so writes from the previous turn become visible in
        the next turn's snapshot, instead of waiting for the next session.
        """
        with self._lock:
            new_block = self._build_full_snapshot_block()
            changed = new_block != self._snapshot_block
            self._snapshot_block = new_block
            self._snapshot_frozen = True
            return changed, new_block

    def get_system_block(self) -> str:
        if self._snapshot_frozen:
            return self._snapshot_block
        return self._build_full_snapshot_block()

    def build_memory_prompt(self, user_input: str = "") -> str:
        _ = user_input
        return self.get_system_block()

    def _build_full_snapshot_block(self) -> str:
        with self._lock:
            snap = self._load_snapshot()
            playbook_snap = self._load_merged_playbook_snapshot()
            user_profile = self._load_user_profile()
        items = [it for it in snap.get("items", []) if isinstance(it, dict) and it.get("status") == "active"]
        playbooks = [
            it
            for it in playbook_snap.get("items", [])
            if isinstance(it, dict) and self._playbook_visible_in_snapshot(it)
        ]
        raw_profile_items = user_profile.get("items", []) if isinstance(user_profile.get("items"), list) else []
        if not items and not playbooks and not raw_profile_items:
            return ""

        picked_memories = self._enforce_item_char_limit(
            sorted(items, key=self._score_item_for_snapshot, reverse=True),
            limit=8000,
            formatter=self._format_memory_item,
        )
        picked_playbooks = self._enforce_item_char_limit(
            sorted(playbooks, key=self._score_item_for_snapshot, reverse=True),
            limit=4000,
            formatter=self._format_playbook_item,
        )
        # Use structured UserProfile for priority-ordered field rendering
        from backend.agent.memory.user_profile import load_from_items, to_snapshot_lines
        profile_obj = load_from_items(raw_profile_items)
        profile_lines = to_snapshot_lines(profile_obj, char_limit=1500)

        lines: List[str] = []
        if profile_lines:
            lines.extend(
                [
                    "## 用户档案（frozen memory snapshot）",
                    "以下为会话开始时冻结的用户档案事实；如与本轮明确指令冲突，以本轮指令为准。",
                    *profile_lines,
                ]
            )
        if picked_memories:
            lines.extend(
                [
                    "## 用户长期记忆（frozen memory snapshot）",
                    "以下信息由记忆子代理维护，并在会话开始时全量加载后冻结。",
                ]
            )
            for item in picked_memories:
                lines.append(self._format_memory_item(item))

        if picked_playbooks:
            lines.extend(
                [
                    "## 同类题求解经验（frozen memory snapshot）",
                    "以下是从历史会话中抽取的可复用解法；按需参考，不替代当前证据。",
                ]
            )
            for item in picked_playbooks:
                lines.append(self._format_playbook_item(item))

        if not lines:
            return ""
        lines.append("如与用户本轮明确指令冲突，以本轮指令为准。")
        return "\n".join(lines)

    async def maybe_update_from_turn(
        self,
        *,
        session_id: str,
        user_input: str,
        assistant_output: str,
        tool_observations: List[dict],
        turn_messages: Optional[List[dict]] = None,
        provider_gateway: ProviderGateway,
        session_logger: "SessionLogger",
    ) -> None:
        user_text_raw = str(user_input or "")
        assistant_text_raw = str(assistant_output or "")
        user_text = _normalize_text(user_text_raw)
        assistant_text = _normalize_text(assistant_text_raw)
        if not user_text:
            return

        with self._lock:
            snap = self._load_snapshot()

        payload = self._build_sub_agent_payload(
            session_id=session_id,
            user_input=user_text_raw,
            assistant_output=assistant_text_raw,
            tool_observations=tool_observations,
            turn_messages=turn_messages if isinstance(turn_messages, list) else [],
            snapshot=snap,
        )

        decision_text = ""
        decision: dict[str, Any] = {}
        try:
            decision_text, decision = await self._memory_sub_agent.decide(
                provider_gateway=provider_gateway,
                payload=payload,
                step=self._next_memory_step(),
            )
        except Exception as e:
            session_logger.log_trace_event(
                "MEMORY_AGENT_ERROR",
                {
                    "session_id": session_id,
                    "error": str(e),
                },
            )
            return

        should_write = _as_bool(decision.get("should_write", False))
        ops = decision.get("operations") if isinstance(decision.get("operations"), list) else []
        should_write_playbook = _as_bool(
            decision.get("should_write_playbook", bool(decision.get("playbook_operations")))
        )
        playbook_ops = (
            decision.get("playbook_operations")
            if isinstance(decision.get("playbook_operations"), list)
            else []
        )

        session_logger.log_trace_event(
            "MEMORY_AGENT_DECISION",
            {
                "session_id": session_id,
                "should_write": should_write,
                "should_write_playbook": should_write_playbook,
                "reason": str(decision.get("reason") or ""),
                "operations": ops,
                "playbook_reason": str(decision.get("playbook_reason") or ""),
                "playbook_operations": playbook_ops,
                "raw": decision_text[:2000],
            },
        )

        if (not should_write or not ops) and (not should_write_playbook or not playbook_ops):
            self._append_event(
                {
                    "type": "memory_skip",
                    "session_id": session_id,
                    "user_input": user_text[:600],
                    "reason": str(decision.get("reason") or ""),
                    "playbook_reason": str(decision.get("playbook_reason") or ""),
                }
            )
            return

        memory_changed: List[dict] = []
        profile_changed: List[dict] = []
        playbook_changed: List[dict] = []
        _need_dedup = False
        with self._lock:
            if should_write and ops:
                current = self._load_snapshot()
                profile_current = self._load_user_profile()
                memory_changed, profile_changed = self._apply_operations(
                    current, ops, session_id=session_id, profile_snap=profile_current
                )
                if memory_changed:
                    current["updated_at"] = _now_iso()
                    self._prune_snapshot(current)
                    self._save_snapshot(current)
                if profile_changed:
                    profile_current["updated_at"] = _now_iso()
                    self._save_user_profile(profile_current)

            if should_write_playbook and playbook_ops:
                playbook_current = self._load_playbook_snapshot()
                playbook_changed = self._apply_playbook_operations(
                    playbook_current,
                    playbook_ops,
                    session_id=session_id,
                )
                if playbook_changed:
                    review_changes: List[dict] = []
                    try:
                        from backend.agent.skills.playbook_reviewer import PlaybookReviewer

                        review_changes = PlaybookReviewer().review_snapshot(playbook_current)
                    except Exception:
                        review_changes = []
                    if review_changes:
                        playbook_changed.extend(
                            {"op": "candidate_skill_generated", **change} for change in review_changes
                        )
                    playbook_current["updated_at"] = _now_iso()
                    self._prune_playbook_snapshot(playbook_current)
                    self._save_playbook_snapshot(playbook_current)

                    new_inserts = [c for c in playbook_changed if c.get("op") == "insert_playbook"]
                    if new_inserts:
                        self._dedup_insert_count += 1
                        active_count = sum(
                            1 for it in playbook_current.get("items", [])
                            if not self._playbook_is_archived(it)
                        )
                        if (active_count >= _DEDUP_MIN_PLAYBOOKS
                                and self._dedup_insert_count % _DEDUP_INSERT_INTERVAL == 0):
                            _need_dedup = True

        if _need_dedup:
            await self._run_playbook_dedup(
                provider_gateway=provider_gateway,
                session_logger=session_logger,
                session_id=session_id,
            )

        if memory_changed or profile_changed or playbook_changed:
            changed = {
                "memory": memory_changed,
                "profile": profile_changed,
                "playbooks": playbook_changed,
            }
            self._append_event(
                {
                    "type": "memory_update",
                    "session_id": session_id,
                    "user_input": user_text[:600],
                    "assistant_output": assistant_text[:600],
                    "decision": {
                        "should_write": should_write,
                        "reason": str(decision.get("reason") or ""),
                        "operations": ops,
                        "should_write_playbook": should_write_playbook,
                        "playbook_reason": str(decision.get("playbook_reason") or ""),
                        "playbook_operations": playbook_ops,
                    },
                    "changed": changed,
                }
            )
            session_logger.log_trace_event(
                "MEMORY_UPDATED",
                {
                    "session_id": session_id,
                    "changes": changed,
                },
            )
        else:
            self._append_event(
                {
                    "type": "memory_skip",
                    "session_id": session_id,
                    "user_input": user_text[:600],
                    "reason": "write_requested_but_no_effect",
                }
            )

    async def _run_playbook_dedup(
        self,
        *,
        provider_gateway: "ProviderGateway",
        session_logger: "SessionLogger",
        session_id: str,
    ) -> None:
        from backend.agent.sub_agents.playbook_dedup import PlaybookDeduplicateSubAgent

        with self._lock:
            snap = self._load_playbook_snapshot()
        active_items = [it for it in snap.get("items", []) if not self._playbook_is_archived(it)]
        if len(active_items) < 2:
            return

        payload = [
            {
                "playbook_id": it.get("playbook_id"),
                "problem_type": it.get("problem_type"),
                "approach": it.get("approach"),
                "trigger_terms": it.get("trigger_terms", []),
                "confidence": it.get("confidence", 0.7),
            }
            for it in active_items
        ]

        sub = PlaybookDeduplicateSubAgent()
        try:
            _, decision = await sub.decide(
                provider_gateway=provider_gateway,
                playbooks=payload,
                step=self._next_memory_step(),
            )
        except Exception as e:
            session_logger.log_trace_event(
                "PLAYBOOK_DEDUP_ERROR",
                {"session_id": session_id, "error": str(e)},
            )
            return

        if not _as_bool(decision.get("has_duplicates")):
            return

        merge_groups = decision.get("merge_groups")
        if not isinstance(merge_groups, list) or not merge_groups:
            return

        valid_ids = {it.get("playbook_id") for it in active_items}
        changed: List[dict] = []
        now = _now_iso()

        with self._lock:
            snap = self._load_playbook_snapshot()
            items = snap.get("items", [])

            for group in merge_groups:
                if not isinstance(group, dict):
                    continue
                canonical_id = _normalize_text(group.get("canonical_id"))
                archive_ids = [_normalize_text(aid) for aid in (group.get("archive_ids") or []) if aid]
                if not canonical_id or canonical_id not in valid_ids:
                    continue
                archive_ids = [aid for aid in archive_ids if aid and aid != canonical_id and aid in valid_ids]
                if not archive_ids:
                    continue

                canonical = next((it for it in items if it.get("playbook_id") == canonical_id), None)
                if canonical is None or self._playbook_is_archived(canonical):
                    continue

                if group.get("merged_problem_type"):
                    canonical["problem_type"] = _normalize_text(group["merged_problem_type"])
                if group.get("merged_approach"):
                    canonical["approach"] = _normalize_text(group["merged_approach"])
                raw_merged_terms = group.get("merged_trigger_terms")
                if isinstance(raw_merged_terms, list):
                    existing = canonical.get("trigger_terms") if isinstance(canonical.get("trigger_terms"), list) else []
                    merged_terms = list(dict.fromkeys([*existing, *[_normalize_text(t) for t in raw_merged_terms if t]]))
                    canonical["trigger_terms"] = [t for t in merged_terms if t][:16]
                canonical["updated_at"] = now

                for arc_id in archive_ids:
                    arc_item = next((it for it in items if it.get("playbook_id") == arc_id), None)
                    if arc_item is None or self._playbook_is_archived(arc_item):
                        continue
                    existing_sessions = canonical.get("evidence_sessions") if isinstance(canonical.get("evidence_sessions"), list) else []
                    arc_sessions = arc_item.get("evidence_sessions") if isinstance(arc_item.get("evidence_sessions"), list) else []
                    merged_sessions = list(dict.fromkeys([*existing_sessions, *arc_sessions]))
                    canonical["evidence_sessions"] = merged_sessions[-20:]
                    canonical["success_count"] = int(canonical.get("success_count") or 0) + int(arc_item.get("success_count") or 0)
                    canonical["hit_count"] = int(canonical.get("hit_count") or 0) + int(arc_item.get("hit_count") or 0)
                    arc_item["status"] = "archived"
                    arc_item["lifecycle_status"] = "archived"
                    arc_item["archived_reason"] = "merged by dedup"
                    arc_item["updated_at"] = now
                    changed.append({"op": "dedup_archive", "playbook_id": arc_id, "merged_into": canonical_id})

            if changed:
                snap["updated_at"] = now
                self._save_playbook_snapshot(snap)
                self._append_event(
                    {"type": "playbook_dedup", "session_id": session_id, "changed": changed}
                )
                session_logger.log_trace_event(
                    "PLAYBOOK_DEDUP", {"session_id": session_id, "changes": changed}
                )

    # ── Public query/delete APIs (used by virtual tools) ─────────────

    def query_memories(self, scope: str, query: str = "", limit: int = 10) -> list[dict]:
        """Return up to *limit* items from *scope*, ranked by keyword relevance."""
        scope = (scope or "").strip().lower()
        with self._lock:
            if scope == "long_term":
                snap = self._load_snapshot()
                items = [it for it in snap.get("items", []) if isinstance(it, dict) and it.get("status") == "active"]
                terms = _extract_terms(query) if query else []
                scored = [(self._score_item(it, terms), it) for it in items]
                scored.sort(key=lambda x: x[0], reverse=True)
                return [it for _, it in scored[:limit]]
            if scope == "playbook":
                snap = self._load_playbook_snapshot()
                items = [
                    it
                    for it in snap.get("items", [])
                    if isinstance(it, dict) and not self._playbook_is_archived(it)
                ]
                terms = _extract_terms(query) if query else []
                scored = [(self._score_playbook(it, terms), it) for it in items]
                scored.sort(key=lambda x: x[0], reverse=True)
                return [it for _, it in scored[:limit]]
            if scope == "user_profile":
                profile = self._load_user_profile()
                return [it for it in profile.get("items", []) if isinstance(it, dict)][:limit]
        return []

    def delete_memory(self, scope: str, memory_id: str, reason: str = "", session_id: str = "") -> dict | None:
        """Soft-delete a memory or playbook item (set status to 'archived').

        Returns a summary dict on success, or None if not found.
        """
        scope = (scope or "").strip().lower()
        with self._lock:
            if scope == "long_term":
                snap = self._load_snapshot()
                op = {"op": "delete", "memory_id": memory_id, "reason": reason}
                change = self._apply_delete(snap.get("items", []), op)
                if change:
                    snap["updated_at"] = _now_iso()
                    self._save_snapshot(snap)
                    self._append_event({"type": "memory_delete", "scope": scope, "memory_id": memory_id, "reason": reason, "session_id": session_id})
                return change
            if scope == "playbook":
                snap = self._load_playbook_snapshot()
                op = {"op": "delete_playbook", "playbook_id": memory_id, "reason": reason}
                change = self._apply_playbook_delete(snap.get("items", []), op)
                if change:
                    snap["updated_at"] = _now_iso()
                    self._save_playbook_snapshot(snap)
                    self._append_event({"type": "memory_delete", "scope": scope, "memory_id": memory_id, "reason": reason, "session_id": session_id})
                return change
        return None

    def write_memory_manual(
        self,
        *,
        scope: str,
        key: str,
        value: str,
        category: str = "fact",
        confidence: float = 0.8,
        reason: str = "",
        session_id: str = "manual",
    ) -> dict | None:
        """Manual memory write used by API and virtual tools."""
        scope = (scope or "long_term").strip().lower()
        with self._lock:
            if scope == "user_profile":
                snap = self._load_user_profile()
                items = snap.get("items")
                if not isinstance(items, list):
                    items = []
                    snap["items"] = items
                change = self._apply_profile_upsert(items, {"key": key, "value": value}, session_id=session_id)
                if change:
                    snap["updated_at"] = _now_iso()
                    self._save_user_profile(snap)
                    self._append_event({"type": "memory_manual_write", "scope": scope, "key": key, "reason": reason, "session_id": session_id})
                return change

            snap = self._load_snapshot()
            items = snap.get("items")
            if not isinstance(items, list):
                items = []
                snap["items"] = items
            change = self._apply_upsert(
                items,
                {
                    "op": "upsert",
                    "category": category,
                    "key": key,
                    "value": value,
                    "summary": reason,
                    "confidence": confidence,
                },
                session_id=session_id,
            )
            if change:
                snap["updated_at"] = _now_iso()
                self._prune_snapshot(snap)
                self._save_snapshot(snap)
                self._append_event({"type": "memory_manual_write", "scope": scope, "key": key, "reason": reason, "session_id": session_id})
            return change

    def update_memory_manual(
        self,
        *,
        scope: str,
        memory_id: str = "",
        key: str = "",
        value: str = "",
        reason: str = "",
        session_id: str = "manual",
    ) -> dict | None:
        """Manual memory edit. Existing item must be found by id or key."""
        scope = (scope or "long_term").strip().lower()
        target_id = _normalize_text(memory_id)
        target_key = _normalize_text(key).lower()
        if not target_id and not target_key:
            return None
        reject_reason = scan_memory_content([key, value, reason])
        if reject_reason:
            self._append_event({"type": "memory_reject", "target": scope, "reason": reject_reason, "key": key[:200]})
            return None
        with self._lock:
            if scope == "user_profile":
                snap = self._load_user_profile()
                items = snap.get("items") if isinstance(snap.get("items"), list) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_key = _normalize_text(item.get("key")).lower()
                    item_id = _normalize_text(item.get("profile_id") or item.get("memory_id"))
                    if (target_id and item_id == target_id) or (target_key and item_key == target_key):
                        item["value"] = value
                        item["updated_at"] = _now_iso()
                        snap["updated_at"] = _now_iso()
                        self._save_user_profile(snap)
                        self._append_event({"type": "memory_manual_update", "scope": scope, "key": key, "reason": reason, "session_id": session_id})
                        return {"op": "update_profile", "key": item.get("key"), "profile_id": item.get("profile_id")}
                return None

            if scope == "playbook":
                snap = self._load_playbook_snapshot()
                items = snap.get("items") if isinstance(snap.get("items"), list) else []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if self._playbook_is_archived(item):
                        continue
                    if target_id and _normalize_text(item.get("playbook_id")) != target_id:
                        continue
                    if value:
                        item["approach"] = value
                    item["updated_at"] = _now_iso()
                    snap["updated_at"] = _now_iso()
                    self._save_playbook_snapshot(snap)
                    self._append_event({"type": "memory_manual_update", "scope": scope, "memory_id": target_id, "reason": reason, "session_id": session_id})
                    return {"op": "update_playbook", "playbook_id": item.get("playbook_id")}
                return None

            snap = self._load_snapshot()
            items = snap.get("items") if isinstance(snap.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict) or item.get("status") != "active":
                    continue
                item_id = _normalize_text(item.get("memory_id"))
                item_key = _normalize_text(item.get("key")).lower()
                if (target_id and item_id == target_id) or (target_key and item_key == target_key):
                    if key:
                        item["key"] = key
                    if value:
                        item["value"] = value
                    item["updated_at"] = _now_iso()
                    snap["updated_at"] = _now_iso()
                    self._save_snapshot(snap)
                    self._append_event({"type": "memory_manual_update", "scope": scope, "memory_id": item_id, "reason": reason, "session_id": session_id})
                    return {"op": "update", "memory_id": item.get("memory_id"), "key": item.get("key")}
        return None

    def _build_sub_agent_payload(
        self,
        *,
        session_id: str,
        user_input: str,
        assistant_output: str,
        tool_observations: List[dict],
        turn_messages: List[dict],
        snapshot: dict,
    ) -> dict:
        recent = self.store.load_chat_messages(session_id)
        recent_short = []
        for msg in recent[-12:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = _normalize_text(msg.get("content"))
            if role not in {"user", "assistant"} or not content:
                continue
            recent_short.append({"role": role, "content": content[:400]})

        tool_short = []
        failure_signal = False
        for ob in tool_observations[-10:]:
            if not isinstance(ob, dict):
                continue
            name = _normalize_text(ob.get("name") or ob.get("tool"))
            result = _normalize_text(ob.get("result"))
            if not name:
                continue
            lowered_result = result.lower()
            if any(token in lowered_result for token in ("error", "错误", "failed", "失败", "exception", "traceback")):
                failure_signal = True
            tool_short.append({"name": name, "result": result[:400]})

        current_turn_messages: List[dict] = []
        for msg in turn_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            if not role:
                continue
            entry: dict[str, Any] = {"role": role}
            if "content" in msg:
                entry["content"] = msg.get("content")
            if "name" in msg:
                entry["name"] = msg.get("name")
            if "tool_calls" in msg:
                entry["tool_calls"] = msg.get("tool_calls")
            if "tool_call_id" in msg:
                entry["tool_call_id"] = msg.get("tool_call_id")
            current_turn_messages.append(entry)

        existing = []
        for item in snapshot.get("items", []):
            if not isinstance(item, dict):
                continue
            if item.get("status") in {"archived", "promoted_to_skill"}:
                continue
            existing.append(
                {
                    "memory_id": item.get("memory_id"),
                    "category": item.get("category"),
                    "key": item.get("key"),
                    "value": item.get("value"),
                    "summary": item.get("summary", ""),
                    "confidence": item.get("confidence", 0.7),
                }
            )

        with self._lock:
            playbook_snapshot = self._load_playbook_snapshot()
            user_profile_snap = self._load_user_profile()
        existing_playbooks = []
        for item in playbook_snapshot.get("items", []):
            if not isinstance(item, dict):
                continue
            if MemoryEngine._playbook_is_archived(item):
                continue
            existing_playbooks.append(
                {
                    "playbook_id": item.get("playbook_id"),
                    "problem_type": item.get("problem_type"),
                    "approach": item.get("approach"),
                    "trigger_terms": item.get("trigger_terms", []),
                    "tool_sequence": item.get("tool_sequence", []),
                    "confidence": item.get("confidence", 0.7),
                    "status": item.get("status"),
                    "lifecycle_status": item.get("lifecycle_status"),
                    "evidence_sessions": item.get("evidence_sessions", []),
                    "success_count": item.get("success_count", 0),
                    "failure_count": item.get("failure_count", 0),
                    "hit_count": item.get("hit_count", 0),
                    "candidate_skill_name": item.get("candidate_skill_name"),
                }
            )

        existing_profile = [
            {"key": it.get("key"), "value": it.get("value")}
            for it in user_profile_snap.get("items", [])
            if isinstance(it, dict) and it.get("key") and it.get("value")
        ]

        return {
            "session_id": session_id,
            "turn": {
                "user_message": user_input,
                "assistant_message": assistant_output,
                "tool_observations": tool_short,
                "success_signal": bool(tool_short) and not failure_signal,
                "failure_signal": failure_signal,
            },
            "current_turn_messages": current_turn_messages,
            "recent_messages": recent_short,
            "existing_memories": existing[:24],
            "existing_profile": existing_profile[:20],
            "existing_playbooks": existing_playbooks[:30],
            "writing_policy": {
                "only_long_term": True,
                "skip_transient": True,
                "allow_ops": ["upsert", "delete"],
                "allow_playbook_ops": ["upsert_playbook", "delete_playbook"],
            },
        }

    @staticmethod
    def _memory_system_prompt() -> str:
        return MemoryUpdateSubAgent.system_prompt

    @staticmethod
    def _parse_decision_json(raw: str) -> dict[str, Any]:
        return MemoryUpdateSubAgent.parse_decision_json(raw)

    def _apply_operations(
        self,
        snap: dict,
        ops: List[dict],
        *,
        session_id: str,
        profile_snap: dict | None = None,
    ) -> tuple[List[dict], List[dict]]:
        """Apply memory operations to *snap*.

        Operations with ``category == "profile"`` are routed to *profile_snap*
        (user_profile.json) instead of the long-term memory snapshot.

        Returns ``(memory_changes, profile_changes)``.
        """
        changes: List[dict] = []
        profile_changes: List[dict] = []
        items = snap.get("items")
        if not isinstance(items, list):
            items = []
            snap["items"] = items

        profile_items: List[dict] = []
        if profile_snap is not None:
            raw = profile_snap.get("items")
            if not isinstance(raw, list):
                raw = []
                profile_snap["items"] = raw
            profile_items = raw

        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").strip().lower()
            category = str(op.get("category") or "other").strip().lower()
            if kind == "upsert":
                if category == "profile" and profile_snap is not None:
                    change = self._apply_profile_upsert(profile_items, op, session_id=session_id)
                    if change is not None:
                        profile_changes.append(change)
                else:
                    change = self._apply_upsert(items, op, session_id=session_id)
                    if change is not None:
                        changes.append(change)
            elif kind == "delete":
                change = self._apply_delete(items, op)
                if change is not None:
                    changes.append(change)

        return changes, profile_changes

    def _apply_profile_upsert(
        self,
        profile_items: List[dict],
        op: dict,
        *,
        session_id: str,
    ) -> Optional[dict]:
        """Upsert a profile fact into user_profile.json items list."""
        key = _normalize_text(op.get("key"))
        value = _normalize_text(op.get("value"))
        if not key or not value:
            return None
        reject_reason = scan_memory_content([key, value])
        if reject_reason:
            self._append_event({"type": "memory_reject", "target": "profile", "reason": reject_reason, "key": key[:200]})
            return None
        now = _now_iso()
        for item in profile_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "").strip().lower() == key.lower():
                item["key"] = key
                item["value"] = value
                item["updated_at"] = now
                return {"op": "update_profile", "key": key}
        profile_id = f"profile_{uuid.uuid4().hex[:12]}"
        profile_items.append(
            {"profile_id": profile_id, "key": key, "value": value, "updated_at": now, "created_at": now}
        )
        return {"op": "insert_profile", "key": key, "profile_id": profile_id}

    def _apply_playbook_operations(
        self,
        snap: dict,
        ops: List[dict],
        *,
        session_id: str,
    ) -> List[dict]:
        changes: List[dict] = []
        items = snap.get("items")
        if not isinstance(items, list):
            items = []
            snap["items"] = items

        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").strip().lower()
            if kind == "upsert_playbook":
                change = self._apply_playbook_upsert(items, op, session_id=session_id)
                if change is not None:
                    changes.append(change)
            elif kind == "delete_playbook":
                change = self._apply_playbook_delete(items, op)
                if change is not None:
                    changes.append(change)
        return changes

    def _apply_playbook_upsert(
        self,
        items: List[dict],
        op: dict,
        *,
        session_id: str,
    ) -> Optional[dict]:
        problem_type = _normalize_text(op.get("problem_type"))
        approach = _normalize_text(op.get("approach"))
        if not problem_type or not approach:
            return None
        reject_reason = scan_memory_content(
            [
                problem_type,
                approach,
                *(op.get("trigger_terms") if isinstance(op.get("trigger_terms"), list) else []),
                *(op.get("tool_sequence") if isinstance(op.get("tool_sequence"), list) else []),
            ]
        )
        if reject_reason:
            self._append_event(
                {
                    "type": "memory_reject",
                    "target": "playbook",
                    "reason": reject_reason,
                    "problem_type": problem_type[:200],
                }
            )
            return None

        raw_trigger_terms = op.get("trigger_terms")
        trigger_terms: List[str] = []
        if isinstance(raw_trigger_terms, list):
            for value in raw_trigger_terms[:12]:
                term = _normalize_text(value)
                if term:
                    trigger_terms.append(term)

        raw_tool_sequence = op.get("tool_sequence")
        tool_sequence: List[str] = []
        if isinstance(raw_tool_sequence, list):
            for value in raw_tool_sequence[:10]:
                tool = _normalize_text(value)
                if tool:
                    tool_sequence.append(tool)

        confidence = _clamp_confidence(op.get("confidence"), default=0.75)
        norm_problem_type = problem_type.lower()
        now = _now_iso()

        target = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if self._playbook_is_archived(item):
                continue
            if _normalize_text(item.get("problem_type")).lower() == norm_problem_type:
                target = item
                break

        # 精确匹配失败 → 字符/词级 Jaccard 模糊匹配（兜底相似题型合并）
        if target is None:
            best_score, best_item = 0.0, None
            for item in items:
                if not isinstance(item, dict) or self._playbook_is_archived(item):
                    continue
                score = _problem_type_similarity(
                    norm_problem_type,
                    _normalize_text(item.get("problem_type")).lower(),
                )
                if score > best_score:
                    best_score, best_item = score, item
            if best_score >= 0.6:
                target = best_item

        if target is None:
            new_item = {
                "playbook_id": f"pb_{uuid.uuid4().hex[:12]}",
                "problem_type": problem_type,
                "approach": approach,
                "trigger_terms": trigger_terms,
                "tool_sequence": tool_sequence,
                "confidence": confidence,
                # Keep status=active for backward compatibility with existing
                # callers/tests; lifecycle_status carries the new promotion state.
                "status": "active",
                "lifecycle_status": "candidate",
                "source_session_id": session_id,
                "evidence_sessions": [session_id],
                "success_count": 1,
                "failure_count": 0,
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "hit_count": 1,
            }
            items.append(new_item)
            return {
                "op": "insert_playbook",
                "playbook_id": new_item["playbook_id"],
                "problem_type": problem_type,
                "confidence": confidence,
            }

        prev_approach = _normalize_text(target.get("approach"))
        prev_conf = _clamp_confidence(target.get("confidence"), default=0.65)
        merged_conf = max(prev_conf * 0.7 + confidence * 0.3, confidence)

        target["problem_type"] = problem_type
        target["approach"] = approach
        target["confidence"] = _clamp_confidence(merged_conf, default=confidence)
        if trigger_terms:
            existing_terms = target.get("trigger_terms")
            if not isinstance(existing_terms, list):
                existing_terms = []
            merged_terms = list(dict.fromkeys([*existing_terms, *trigger_terms]))
            target["trigger_terms"] = [str(v) for v in merged_terms[:16]]
        if tool_sequence:
            existing_tools = target.get("tool_sequence")
            if not isinstance(existing_tools, list):
                existing_tools = []
            merged_tools = list(dict.fromkeys([*existing_tools, *tool_sequence]))
            target["tool_sequence"] = [str(v) for v in merged_tools[:12]]
        sessions = target.get("evidence_sessions")
        if not isinstance(sessions, list):
            sessions = []
        if session_id not in sessions:
            sessions.append(session_id)
        target["evidence_sessions"] = sessions[-20:]
        target["success_count"] = int(target.get("success_count", 0) or 0) + 1
        target["failure_count"] = int(target.get("failure_count", 0) or 0)
        if (
            len(set(target["evidence_sessions"])) >= 3
            and int(target.get("success_count", 0) or 0) >= 3
            and target["failure_count"] == 0
        ):
            target["status"] = "active"
            target["lifecycle_status"] = "validated"
            target["last_validated_at"] = now
        target["updated_at"] = now
        target["last_seen_at"] = now
        target["source_session_id"] = session_id
        target["hit_count"] = int(target.get("hit_count", 0) or 0) + 1

        return {
            "op": "update_playbook",
            "playbook_id": target.get("playbook_id"),
            "problem_type": problem_type,
            "prev_approach": prev_approach,
            "new_approach": approach,
            "confidence": target["confidence"],
        }

    @staticmethod
    def _apply_playbook_delete(items: List[dict], op: dict) -> Optional[dict]:
        target_id = _normalize_text(op.get("playbook_id"))
        target_problem_type = _normalize_text(op.get("problem_type")).lower()
        if not target_id and not target_problem_type:
            return None

        now = _now_iso()
        for item in items:
            if not isinstance(item, dict):
                continue
            if MemoryEngine._playbook_is_archived(item):
                continue
            item_id = _normalize_text(item.get("playbook_id"))
            item_problem_type = _normalize_text(item.get("problem_type")).lower()
            if (target_id and item_id == target_id) or (
                target_problem_type and item_problem_type == target_problem_type
            ):
                item["status"] = "archived"
                item["updated_at"] = now
                item["archived_reason"] = _normalize_text(op.get("reason"))
                return {
                    "op": "delete_playbook",
                    "playbook_id": item.get("playbook_id"),
                    "problem_type": item.get("problem_type"),
                }
        return None

    def _apply_upsert(self, items: List[dict], op: dict, *, session_id: str) -> Optional[dict]:
        key = _normalize_text(op.get("key"))
        value = _normalize_text(op.get("value"))
        if not key or not value:
            return None
        summary = _normalize_text(op.get("summary"))
        reject_reason = scan_memory_content([key, value, summary])
        if reject_reason:
            self._append_event(
                {
                    "type": "memory_reject",
                    "target": "memory",
                    "reason": reject_reason,
                    "key": key[:200],
                }
            )
            return None

        category = _normalize_text(op.get("category") or "other").lower()
        if category not in {
            "preference",
            "goal",
            "project",
            "constraint",
            "profile",
            "workflow",
            "fact",
            "other",
        }:
            category = "other"

        confidence = _clamp_confidence(op.get("confidence"), default=0.75)
        now = _now_iso()

        target = None
        norm_key = key.lower()
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "active":
                continue
            if str(item.get("key") or "").strip().lower() == norm_key:
                target = item
                break

        if target is None:
            new_item = {
                "memory_id": f"mem_{uuid.uuid4().hex[:12]}",
                "category": category,
                "key": key,
                "value": value,
                "summary": summary,
                "confidence": confidence,
                "status": "active",
                "source_session_id": session_id,
                "created_at": now,
                "updated_at": now,
                "last_seen_at": now,
                "hit_count": 1,
            }
            items.append(new_item)
            return {
                "op": "insert",
                "memory_id": new_item["memory_id"],
                "key": key,
                "value": value,
                "confidence": confidence,
            }

        prev_value = _normalize_text(target.get("value"))
        prev_conf = _clamp_confidence(target.get("confidence"), default=0.65)
        merged_conf = max(prev_conf * 0.7 + confidence * 0.3, confidence)

        target["category"] = category
        target["key"] = key
        target["value"] = value
        if summary:
            target["summary"] = summary
        target["confidence"] = _clamp_confidence(merged_conf, default=confidence)
        target["updated_at"] = now
        target["last_seen_at"] = now
        target["source_session_id"] = session_id
        target["hit_count"] = int(target.get("hit_count", 0) or 0) + 1

        return {
            "op": "update",
            "memory_id": target.get("memory_id"),
            "key": key,
            "prev_value": prev_value,
            "new_value": value,
            "confidence": target["confidence"],
        }

    @staticmethod
    def _apply_delete(items: List[dict], op: dict) -> Optional[dict]:
        target_id = _normalize_text(op.get("memory_id"))
        target_key = _normalize_text(op.get("key")).lower()
        if not target_id and not target_key:
            return None

        now = _now_iso()
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "active":
                continue
            item_id = _normalize_text(item.get("memory_id"))
            item_key = _normalize_text(item.get("key")).lower()
            if (target_id and item_id == target_id) or (target_key and item_key == target_key):
                item["status"] = "archived"
                item["updated_at"] = now
                item["archived_reason"] = _normalize_text(op.get("reason"))
                return {
                    "op": "delete",
                    "memory_id": item.get("memory_id"),
                    "key": item.get("key"),
                }
        return None

    @staticmethod
    def _parse_time_score(value: Any) -> float:
        text = _normalize_text(value)
        if not text:
            return 0.0
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400)
            return max(0.0, 1.0 - min(age_days, 365.0) / 365.0)
        except Exception:
            return 0.0

    @classmethod
    def _score_item_for_snapshot(cls, item: dict) -> float:
        confidence = _clamp_confidence(item.get("confidence"), default=0.65)
        recency = cls._parse_time_score(item.get("updated_at") or item.get("last_seen_at"))
        try:
            hit_rate = min(float(item.get("hit_count", 0) or 0) / 10.0, 1.0)
        except Exception:
            hit_rate = 0.0
        return round(confidence * 0.5 + recency * 0.3 + hit_rate * 0.2, 4)

    @staticmethod
    def _format_memory_item(item: dict) -> str:
        category = str(item.get("category") or "other")
        key = _normalize_text(item.get("key"))
        value = _normalize_text(item.get("value"))
        confidence = _clamp_confidence(item.get("confidence"), default=0.7)
        if not key or not value:
            return ""
        return f"- [{category}] {key}: {value}（置信度 {confidence:.2f}）"

    @staticmethod
    def _format_playbook_item(item: dict) -> str:
        problem_type = _normalize_text(item.get("problem_type"))
        approach = _normalize_text(item.get("approach"))
        tool_sequence = item.get("tool_sequence") if isinstance(item.get("tool_sequence"), list) else []
        confidence = _clamp_confidence(item.get("confidence"), default=0.7)
        if not problem_type or not approach:
            return ""
        tool_hint = ""
        cleaned_tools = [_normalize_text(name) for name in tool_sequence if _normalize_text(name)]
        if cleaned_tools:
            tool_hint = f"；建议工具链 {' -> '.join(cleaned_tools[:5])}"
        return f"- [{problem_type}] {approach}{tool_hint}（置信度 {confidence:.2f}）"

    @staticmethod
    def _playbook_visible_in_snapshot(item: dict) -> bool:
        lifecycle = str(item.get("lifecycle_status") or "").strip().lower()
        if lifecycle:
            return lifecycle == "validated"
        # "active" is kept for backward compatibility with existing stored data.
        return str(item.get("status") or "").strip().lower() in {"active", "validated"}

    @staticmethod
    def _playbook_is_archived(item: dict) -> bool:
        status = str(item.get("status") or "").strip().lower()
        lifecycle = str(item.get("lifecycle_status") or "").strip().lower()
        return status in {"archived", "promoted_to_skill"} or lifecycle in {"archived", "promoted_to_skill"}

    @staticmethod
    def _normalize_playbook_lifecycle(item: dict) -> None:
        lifecycle = str(item.get("lifecycle_status") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        if lifecycle:
            return
        if status == "archived":
            item["lifecycle_status"] = "archived"
            return
        if status == "promoted_to_skill":
            item["lifecycle_status"] = "promoted_to_skill"
            return
        if status == "validated":
            item["status"] = "active"
            item["lifecycle_status"] = "validated"
            return
        if status == "active":
            evidence = item.get("evidence_sessions")
            evidence_count = len(set(str(v) for v in evidence)) if isinstance(evidence, list) else 0
            hit_count = int(item.get("hit_count", 0) or 0)
            item["lifecycle_status"] = "validated" if evidence_count >= 2 or hit_count >= 2 else "candidate"

    @staticmethod
    def _format_profile_item(item: dict) -> str:
        key = _normalize_text(item.get("key") or item.get("name"))
        value = _normalize_text(item.get("value"))
        if not key or not value:
            return ""
        return f"- {key}: {value}"

    @staticmethod
    def _enforce_item_char_limit(items: List[dict], *, limit: int, formatter) -> List[dict]:
        kept: List[dict] = []
        used = 0
        for item in items:
            rendered = formatter(item)
            if not rendered:
                continue
            next_used = used + len(rendered) + 1
            if next_used > limit:
                break
            kept.append(item)
            used = next_used
        return kept

    @staticmethod
    def _enforce_text_lines_char_limit(lines: List[str], *, limit: int) -> List[str]:
        kept: List[str] = []
        used = 0
        for line in lines:
            if not line:
                continue
            next_used = used + len(line) + 1
            if next_used > limit:
                break
            kept.append(line)
            used = next_used
        return kept

    @staticmethod
    def _score_item(item: dict, terms: List[str]) -> float:
        confidence = _clamp_confidence(item.get("confidence"), default=0.65)
        text = " ".join(
            [
                str(item.get("key") or ""),
                str(item.get("value") or ""),
                str(item.get("summary") or ""),
            ]
        ).lower()
        score = confidence
        if str(item.get("category") or "") in {"preference", "constraint", "goal"}:
            score += 0.08
        if terms:
            hit = 0
            for term in terms:
                if term and term in text:
                    hit += 1
            if hit:
                score += min(0.22, hit * 0.04)
        return round(score, 4)

    @staticmethod
    def _score_playbook(item: dict, terms: List[str]) -> float:
        confidence = _clamp_confidence(item.get("confidence"), default=0.65)
        trigger_terms = item.get("trigger_terms") if isinstance(item.get("trigger_terms"), list) else []
        tool_sequence = item.get("tool_sequence") if isinstance(item.get("tool_sequence"), list) else []
        text = " ".join(
            [
                str(item.get("problem_type") or ""),
                str(item.get("approach") or ""),
                " ".join(str(v) for v in trigger_terms),
                " ".join(str(v) for v in tool_sequence),
            ]
        ).lower()
        score = confidence + (0.05 if tool_sequence else 0.0)
        if terms:
            hit = 0
            for term in terms:
                if term and term in text:
                    hit += 1
            if hit:
                score += min(0.24, hit * 0.05)
        return round(score, 4)

    def _load_snapshot(self) -> dict:
        default = {
            "version": 1,
            "user_id": "default_user",
            "updated_at": _now_iso(),
            "items": [],
        }
        try:
            with open(self.snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            if not isinstance(data.get("items"), list):
                data["items"] = []
            data.setdefault("version", 1)
            data.setdefault("user_id", "default_user")
            data.setdefault("updated_at", _now_iso())
            return data
        except Exception:
            return default

    def _load_playbook_snapshot(self) -> dict:
        """Load the user's own playbooks (used for read-modify-write cycles)."""
        default = {
            "version": 1,
            "updated_at": _now_iso(),
            "items": [],
        }
        try:
            with open(self.playbooks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            if not isinstance(data.get("items"), list):
                data["items"] = []
            for item in data["items"]:
                if isinstance(item, dict):
                    self._normalize_playbook_lifecycle(item)
            data.setdefault("version", 1)
            data.setdefault("updated_at", _now_iso())
            return data
        except Exception:
            return default

    def _load_merged_playbook_snapshot(self) -> dict:
        """Load global + user playbooks merged (used for snapshot/display only).

        User items override global items that share the same problem_type.
        Saves are always written only to the user-private playbooks_path.
        """
        def _read_items(path: str) -> list:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    return []
                items = data.get("items") if isinstance(data.get("items"), list) else []
                for item in items:
                    if isinstance(item, dict):
                        self._normalize_playbook_lifecycle(item)
                return items
            except Exception:
                return []

        global_items = _read_items(self._global_playbooks_path)
        user_items = _read_items(self.playbooks_path)

        merged: dict[str, dict] = {}
        for item in global_items:
            key = str(item.get("problem_type") or id(item))
            merged[key] = item
        for item in user_items:
            key = str(item.get("problem_type") or id(item))
            merged[key] = item  # user-private wins on conflict

        return {
            "version": 1,
            "updated_at": _now_iso(),
            "items": list(merged.values()),
        }

    def _load_user_profile(self) -> dict:
        default = {
            "version": 1,
            "user_id": "default_user",
            "updated_at": _now_iso(),
            "items": [],
        }
        try:
            with open(self.user_profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            if not isinstance(data.get("items"), list):
                data["items"] = []
            data.setdefault("version", 1)
            data.setdefault("user_id", "default_user")
            data.setdefault("updated_at", _now_iso())
            return data
        except Exception:
            return default

    def _save_snapshot(self, data: dict) -> None:
        atomic_json_write(self.snapshot_path, data)

    def _save_playbook_snapshot(self, data: dict) -> None:
        atomic_json_write(self.playbooks_path, data)

    def _save_user_profile(self, data: dict) -> None:
        atomic_json_write(self.user_profile_path, data)

    def _append_event(self, event: dict) -> None:
        record = {
            "ts": _now_iso(),
            **event,
        }
        with self._lock:
            append_jsonl(self.events_path, record)

    @staticmethod
    def _prune_snapshot(snapshot: dict, keep_active: int = 120) -> None:
        items = snapshot.get("items")
        if not isinstance(items, list):
            snapshot["items"] = []
            return

        active = [
            it
            for it in items
            if isinstance(it, dict) and str(it.get("status") or "").lower() not in {"archived", "promoted_to_skill"}
        ]
        archived = [
            it
            for it in items
            if isinstance(it, dict) and str(it.get("status") or "").lower() in {"archived", "promoted_to_skill"}
        ]
        active.sort(
            key=lambda it: (
                _clamp_confidence(it.get("confidence"), default=0.6),
                str(it.get("updated_at") or ""),
            ),
            reverse=True,
        )
        snapshot["items"] = active[: max(20, keep_active)] + archived[-100:]

    @staticmethod
    def _prune_playbook_snapshot(snapshot: dict, keep_active: int = 80) -> None:
        items = snapshot.get("items")
        if not isinstance(items, list):
            snapshot["items"] = []
            return

        active = [it for it in items if isinstance(it, dict) and not MemoryEngine._playbook_is_archived(it)]
        archived = [it for it in items if isinstance(it, dict) and MemoryEngine._playbook_is_archived(it)]
        active.sort(
            key=lambda it: (
                _clamp_confidence(it.get("confidence"), default=0.6),
                str(it.get("updated_at") or ""),
            ),
            reverse=True,
        )
        snapshot["items"] = active[: max(20, keep_active)] + archived[-100:]

    def _next_memory_step(self) -> int:
        with self._step_lock:
            self._memory_step_seq += 1
            return 100000 + self._memory_step_seq

    def _migrate_nested_memory_dir_if_needed(self) -> None:
        """Recover data written by the old memory/memory/default_user path bug."""
        nested_dir = getattr(self, "_nested_legacy_memory_dir", "")
        if not nested_dir:
            return
        if os.path.abspath(nested_dir) == os.path.abspath(self.memory_dir):
            return
        if not os.path.isdir(nested_dir):
            return

        # Idempotency: if already migrated, skip
        flag_path = os.path.join(nested_dir, ".migrated")
        if os.path.exists(flag_path):
            return

        migrated: List[str] = []
        memory_src = os.path.join(nested_dir, "long_term_memory.json")
        if self._merge_snapshot_file(
            memory_src,
            self.snapshot_path,
            kind="memory",
        ):
            migrated.append("long_term_memory.json")

        playbook_src = os.path.join(nested_dir, "problem_solving_playbooks.json")
        if self._merge_snapshot_file(
            playbook_src,
            self.playbooks_path,
            kind="playbook",
        ):
            migrated.append("problem_solving_playbooks.json")

        events_src = os.path.join(nested_dir, "memory_events.jsonl")
        if os.path.exists(events_src):
            if not os.path.exists(self.events_path):
                try:
                    shutil.copy2(events_src, self.events_path)
                    migrated.append("memory_events.jsonl")
                except Exception:
                    pass
            else:
                backup_dir = os.path.join(self.memory_dir, "legacy_backup", "nested_memory_path")
                os.makedirs(backup_dir, exist_ok=True)
                try:
                    shutil.copy2(events_src, os.path.join(backup_dir, "memory_events.jsonl"))
                    migrated.append("memory_events.jsonl.backup")
                except Exception:
                    pass

        if not migrated:
            return
        # Write idempotency flag so subsequent startups skip this migration
        try:
            with open(flag_path, "w", encoding="utf-8") as _f:
                _f.write(_now_iso())
        except Exception:
            pass
        self._append_event(
            {
                "type": "memory_migrated",
                "source": nested_dir,
                "reason": "nested_memory_path_bug",
                "files": migrated,
            }
        )

    def _merge_snapshot_file(self, src_path: str, dst_path: str, *, kind: str) -> bool:
        if not os.path.exists(src_path):
            return False

        if not os.path.exists(dst_path):
            try:
                shutil.copy2(src_path, dst_path)
                return True
            except Exception:
                return False

        try:
            with open(src_path, "r", encoding="utf-8") as f:
                src = json.load(f)
            with open(dst_path, "r", encoding="utf-8") as f:
                dst = json.load(f)
        except Exception:
            return False
        if not isinstance(src, dict) or not isinstance(dst, dict):
            return False

        src_items = src.get("items") if isinstance(src.get("items"), list) else []
        dst_items = dst.get("items") if isinstance(dst.get("items"), list) else []
        if not src_items:
            return False

        merged: dict[str, dict] = {}
        anonymous_idx = 0
        for item in [*dst_items, *src_items]:
            if not isinstance(item, dict):
                continue
            key = self._snapshot_item_key(item, kind=kind)
            if not key:
                anonymous_idx += 1
                key = f"anonymous:{anonymous_idx}"
            previous = merged.get(key)
            if previous is None or self._snapshot_item_sort_key(item) >= self._snapshot_item_sort_key(previous):
                merged[key] = item

        dst["items"] = list(merged.values())
        dst["updated_at"] = max(
            str(dst.get("updated_at") or ""),
            str(src.get("updated_at") or ""),
            _now_iso(),
        )
        dst.setdefault("version", src.get("version", 1))
        if kind == "memory":
            dst.setdefault("user_id", src.get("user_id", "default_user"))

        try:
            atomic_json_write(dst_path, dst)
            return True
        except Exception:
            return False

    @staticmethod
    def _snapshot_item_key(item: dict, *, kind: str) -> str:
        if kind == "playbook":
            problem_type = _normalize_text(item.get("problem_type")).lower()
            if problem_type:
                return f"problem_type:{problem_type}"
            playbook_id = _normalize_text(item.get("playbook_id"))
            return f"playbook_id:{playbook_id}" if playbook_id else ""

        key = _normalize_text(item.get("key")).lower()
        category = _normalize_text(item.get("category") or "other").lower()
        if key:
            return f"memory_key:{category}:{key}"
        memory_id = _normalize_text(item.get("memory_id"))
        return f"memory_id:{memory_id}" if memory_id else ""

    @staticmethod
    def _snapshot_item_sort_key(item: dict) -> tuple[str, str]:
        return (
            str(item.get("updated_at") or ""),
            str(item.get("last_seen_at") or ""),
        )

    def _migrate_legacy_memory_if_needed(self) -> None:
        if os.path.exists(self.snapshot_path):
            return

        candidates = [
            os.path.join(self.memory_dir, "memory_snapshot.json"),
            os.path.join(self.logs_dir, "memory", "default_user", "memory_snapshot.json"),
        ]

        src = ""
        for path in candidates:
            if os.path.exists(path):
                src = path
                break
        if not src:
            return

        try:
            with open(src, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except Exception:
            return

        items = legacy.get("items") if isinstance(legacy, dict) else None
        if not isinstance(items, list):
            return

        migrated_items: List[dict] = []
        now = _now_iso()
        for it in items:
            if not isinstance(it, dict):
                continue
            key = _normalize_text(it.get("key"))
            value = _normalize_text(it.get("value"))
            if not key or not value:
                continue
            migrated_items.append(
                {
                    "memory_id": f"mem_{uuid.uuid4().hex[:12]}",
                    "category": "other",
                    "key": key,
                    "value": value,
                    "summary": _normalize_text(it.get("summary")),
                    "confidence": _clamp_confidence(it.get("confidence"), default=0.7),
                    "status": "active",
                    "source_session_id": _normalize_text(it.get("session_id")),
                    "created_at": _normalize_text(it.get("updated_at")) or now,
                    "updated_at": _normalize_text(it.get("updated_at")) or now,
                    "last_seen_at": _normalize_text(it.get("updated_at")) or now,
                    "hit_count": 1,
                }
            )

        if not migrated_items:
            return

        snapshot = {
            "version": 1,
            "user_id": "default_user",
            "updated_at": now,
            "items": migrated_items,
        }
        self._save_snapshot(snapshot)

        # 若旧文件位于日志目录，复制一份到新目录供排障。
        legacy_dir = os.path.join(self.logs_dir, "memory", "default_user")
        if os.path.isdir(legacy_dir):
            backup_dir = os.path.join(self.memory_dir, "legacy_backup")
            os.makedirs(backup_dir, exist_ok=True)
            for name in ("memory_snapshot.json", "memory_state.json"):
                src_file = os.path.join(legacy_dir, name)
                dst_file = os.path.join(backup_dir, name)
                if os.path.exists(src_file) and not os.path.exists(dst_file):
                    try:
                        shutil.copy2(src_file, dst_file)
                    except Exception:
                        pass

        self._append_event(
            {
                "type": "memory_migrated",
                "source": src,
                "item_count": len(migrated_items),
            }
        )
