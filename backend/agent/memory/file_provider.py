from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import backend.data_paths as _dp
from backend.agent.memory.provider import MemoryProvider
from backend.agent.memory.safety import scan_memory_content
from backend.agent.memory.store import append_jsonl


MEMORY_TYPES = {"user", "feedback", "project", "reference"}
ENTRYPOINT_NAME = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_CHARS = 25_000
MAX_TOPIC_CHARS = 8_000
MAX_SCAN_FILES = 200


VIRTUAL_FILE_MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "server": "igraph",
        "name": "file_memory_read",
        "description": (
            "Read persistent markdown memories. Use this when long-form user, "
            "feedback, project, or reference memory may help the current task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional search query."},
                "path": {"type": "string", "description": "Optional relative memory file path."},
                "limit": {"type": "integer", "default": 5},
            },
        },
    },
    {
        "server": "igraph",
        "name": "file_memory_write",
        "description": (
            "Write a durable markdown memory topic and add it to MEMORY.md. "
            "Use only for cross-session information that is not derivable from current files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                },
                "name": {"type": "string"},
                "description": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["type", "name", "description", "content"],
        },
    },
    {
        "server": "igraph",
        "name": "file_memory_update",
        "description": "Update an existing durable markdown memory topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative memory file path."},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                },
                "content": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "server": "igraph",
        "name": "file_memory_delete",
        "description": "Archive or delete an existing durable markdown memory topic.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative memory file path."},
                "reason": {"type": "string"},
                "archive": {"type": "boolean", "default": True},
            },
            "required": ["path"],
        },
    },
]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _slugify(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text[:80] or f"memory-{uuid.uuid4().hex[:8]}"


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _frontmatter_block(meta: dict[str, str], content: str) -> str:
    lines = ["---"]
    for key in ("name", "description", "type", "updated_at"):
        value = str(meta.get(key) or "").replace("\n", " ").strip()
        if value:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", content.strip(), ""])
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = -1
    for i in range(1, min(len(lines), 80)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body


def _extract_terms(text: str) -> list[str]:
    if not text:
        return []
    terms = re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text)
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


class FileMemoryProvider(MemoryProvider):
    """File-based markdown memory provider inspired by src/memdir.

    The provider stores a small MEMORY.md index plus typed topic files under
    ``data/users/{user_id}/memory/files``. It is intentionally conservative:
    prompt injection is blocked before writes, paths are contained within the
    memory root, and automatic extraction is gated by
    ``ENABLE_FILE_MEMORY_AUTO_UPDATE``.
    """

    def __init__(self, user_id: str = "", root: str | Path | None = None) -> None:
        effective_user = str(user_id or "default_user").strip() or "default_user"
        self.user_id = effective_user
        self.root = Path(root) if root is not None else _dp.user_memory_dir(effective_user) / "files"
        self.events_path = self.root / "file_memory_events.jsonl"
        self._snapshot_block = ""
        self._snapshot_frozen = False

    @property
    def name(self) -> str:
        return "file_memory"

    def is_available(self) -> bool:
        return _env_flag("ENABLE_FILE_MEMORY_PROVIDER", True)

    def initialize(self, scope, **kwargs: object) -> None:
        _ = scope, kwargs
        self.root.mkdir(parents=True, exist_ok=True)
        for memory_type in sorted(MEMORY_TYPES):
            (self.root / memory_type).mkdir(parents=True, exist_ok=True)
        entry = self._entrypoint_path()
        if not entry.exists():
            _atomic_text_write(entry, "# Persistent File Memory\n")

    def get_tool_schemas(self) -> list[dict]:
        return list(VIRTUAL_FILE_MEMORY_TOOL_SPECS)

    def handle_tool_call(self, name: str, args: dict) -> str:
        try:
            if name == "file_memory_read":
                return json.dumps(self.read(args or {}), ensure_ascii=False, indent=2)
            if name == "file_memory_write":
                return json.dumps(self.write(args or {}, session_id="agent"), ensure_ascii=False, indent=2)
            if name == "file_memory_update":
                return json.dumps(self.update(args or {}, session_id="agent"), ensure_ascii=False, indent=2)
            if name == "file_memory_delete":
                return json.dumps(self.delete(args or {}, session_id="agent"), ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        return f"unknown file memory tool: {name}"

    def load_and_freeze(self, scope) -> str:
        self.initialize(scope)
        self._snapshot_block = self._build_snapshot_block()
        self._snapshot_frozen = True
        return self._snapshot_block

    def system_prompt_block(self) -> str:
        if self._snapshot_frozen:
            return self._snapshot_block
        return self._build_snapshot_block()

    def refresh_snapshot(self) -> tuple[bool, str]:
        new_block = self._build_snapshot_block()
        changed = new_block != self._snapshot_block
        self._snapshot_block = new_block
        self._snapshot_frozen = True
        return changed, new_block

    async def sync_turn(self, turn_data: dict) -> None:
        if not _env_flag("ENABLE_FILE_MEMORY_AUTO_UPDATE", False):
            return
        provider_gateway = turn_data.get("provider_gateway")
        if provider_gateway is None:
            return
        user_input = str(turn_data.get("user_input") or "")
        assistant_output = str(turn_data.get("assistant_output") or "")
        if not user_input.strip() or not assistant_output.strip():
            return
        prompt = self._build_extraction_prompt(
            user_input=user_input,
            assistant_output=assistant_output,
            tool_observations=turn_data.get("tool_observations") if isinstance(turn_data.get("tool_observations"), list) else [],
            turn_messages=turn_data.get("turn_messages") if isinstance(turn_data.get("turn_messages"), list) else [],
        )
        try:
            raw = await provider_gateway.invoke_text(
                [{"role": "user", "content": prompt}],
                step=200000,
            )
        except Exception:
            return
        decision = self._parse_decision(raw)
        if not _as_bool(decision.get("should_write")):
            self._append_event({"type": "file_memory_skip", "reason": decision.get("reason", "")})
            return
        changes: list[dict] = []
        for op in decision.get("operations", []):
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "write").strip().lower()
            try:
                if kind == "delete":
                    changes.append(self.delete(op, session_id=str(turn_data.get("session_id") or "")))
                elif kind == "update":
                    changes.append(self.update(op, session_id=str(turn_data.get("session_id") or "")))
                else:
                    changes.append(self.write(op, session_id=str(turn_data.get("session_id") or "")))
            except Exception as e:
                changes.append({"ok": False, "error": str(e), "op": kind})
        self._append_event({"type": "file_memory_auto_update", "changes": changes})

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        self.initialize(None)
        path_arg = _normalize_text(args.get("path"))
        if path_arg:
            path = self._resolve_topic_path(path_arg)
            meta, content = self._read_topic(path)
            return {
                "ok": True,
                "path": self._relative(path),
                "meta": meta,
                "content": content[:MAX_TOPIC_CHARS],
            }

        query = _normalize_text(args.get("query"))
        limit = self._limit(args.get("limit"), default=5)
        topics = self._scan_topics()
        terms = _extract_terms(query)
        if terms:
            topics.sort(key=lambda item: self._score_topic(item, terms), reverse=True)
        else:
            topics.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        selected = topics[:limit]
        return {
            "ok": True,
            "query": query,
            "index": self._read_index(),
            "items": [
                {
                    "path": item["path"],
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "type": item.get("type", ""),
                    "content": str(item.get("content") or "")[:MAX_TOPIC_CHARS],
                }
                for item in selected
            ],
        }

    def write(self, args: dict[str, Any], *, session_id: str = "manual") -> dict[str, Any]:
        self.initialize(None)
        memory_type = str(args.get("type") or "project").strip().lower()
        if memory_type not in MEMORY_TYPES:
            memory_type = "project"
        name = _normalize_text(args.get("name") or args.get("title") or args.get("key"))
        description = _normalize_text(args.get("description") or args.get("summary") or name)
        content = str(args.get("content") or args.get("value") or "").strip()
        if not name or not content:
            raise ValueError("file_memory_write requires name and content")
        reject_reason = scan_memory_content([memory_type, name, description, content])
        if reject_reason:
            self._append_event({"type": "file_memory_reject", "reason": reject_reason, "name": name})
            raise ValueError(f"content rejected: {reject_reason}")
        rel = Path(memory_type) / f"{_slugify(name)}.md"
        path = self._resolve_topic_path(str(rel))
        meta = {
            "name": name,
            "description": description,
            "type": memory_type,
            "updated_at": _now_iso(),
        }
        _atomic_text_write(path, _frontmatter_block(meta, content))
        self._update_index_entry(self._relative(path), name, description)
        result = {"ok": True, "op": "write", "path": self._relative(path), "type": memory_type, "name": name}
        self._append_event({"type": "file_memory_write", "session_id": session_id, **result})
        return result

    def update(self, args: dict[str, Any], *, session_id: str = "manual") -> dict[str, Any]:
        self.initialize(None)
        rel_path = _normalize_text(args.get("path") or args.get("memory_id"))
        if not rel_path:
            raise ValueError("file_memory_update requires path")
        path = self._resolve_topic_path(rel_path)
        meta, old_content = self._read_topic(path)
        name = _normalize_text(args.get("name")) or meta.get("name") or path.stem
        description = _normalize_text(args.get("description")) or meta.get("description") or name
        memory_type = _normalize_text(args.get("type")) or meta.get("type") or path.parent.name
        if memory_type not in MEMORY_TYPES:
            memory_type = "project"
        content = str(args.get("content") or args.get("value") or old_content).strip()
        reject_reason = scan_memory_content([memory_type, name, description, content])
        if reject_reason:
            self._append_event({"type": "file_memory_reject", "reason": reject_reason, "path": rel_path})
            raise ValueError(f"content rejected: {reject_reason}")
        meta = {"name": name, "description": description, "type": memory_type, "updated_at": _now_iso()}
        _atomic_text_write(path, _frontmatter_block(meta, content))
        self._update_index_entry(self._relative(path), name, description)
        result = {"ok": True, "op": "update", "path": self._relative(path), "type": memory_type, "name": name}
        self._append_event({"type": "file_memory_update", "session_id": session_id, **result})
        return result

    def delete(self, args: dict[str, Any], *, session_id: str = "manual") -> dict[str, Any]:
        self.initialize(None)
        rel_path = _normalize_text(args.get("path") or args.get("memory_id"))
        if not rel_path:
            raise ValueError("file_memory_delete requires path")
        path = self._resolve_topic_path(rel_path)
        archive = _as_bool(args.get("archive", True))
        final_path = ""
        if path.exists():
            if archive:
                archive_dir = self.root / "_archive"
                archive_dir.mkdir(parents=True, exist_ok=True)
                target = archive_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{path.name}"
                path.replace(target)
                final_path = self._relative(target)
            else:
                path.unlink()
        self._remove_index_entry(rel_path)
        result = {"ok": True, "op": "delete", "path": rel_path, "archived_to": final_path}
        self._append_event({
            "type": "file_memory_delete",
            "session_id": session_id,
            "reason": _normalize_text(args.get("reason")),
            **result,
        })
        return result

    def _entrypoint_path(self) -> Path:
        return self.root / ENTRYPOINT_NAME

    def _read_index(self) -> str:
        path = self._entrypoint_path()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return self._truncate_index(text)

    @staticmethod
    def _truncate_index(text: str) -> str:
        lines = text.splitlines()
        truncated = "\n".join(lines[:MAX_INDEX_LINES])
        if len(truncated) > MAX_INDEX_CHARS:
            truncated = truncated[:MAX_INDEX_CHARS]
        if len(lines) > MAX_INDEX_LINES or len(text) > MAX_INDEX_CHARS:
            truncated += "\n\n> WARNING: MEMORY.md was truncated. Keep index entries short."
        return truncated

    def _build_snapshot_block(self) -> str:
        index = self._read_index()
        lines = [
            "## Persistent File Memory (frozen snapshot)",
            "This is a markdown memory system inspired by Codex src memory.",
            "Use it for cross-session information that is useful later and not directly derivable from current files.",
            "Memory types: user, feedback, project, reference.",
            "Do not save transient task state, code structure, git history, debug logs, secrets, or facts already documented elsewhere.",
            "Use file_memory_read to inspect topic files before relying on memory. If memory conflicts with current evidence, trust current evidence and update memory.",
            "MEMORY.md index:",
            index or "(empty)",
        ]
        return "\n".join(lines)

    def _build_extraction_prompt(
        self,
        *,
        user_input: str,
        assistant_output: str,
        tool_observations: list[dict],
        turn_messages: list[dict],
    ) -> str:
        manifest = self._format_manifest(self._scan_topics(include_content=False))
        payload = {
            "user_input": user_input,
            "assistant_output": assistant_output,
            "tool_observations": tool_observations[-8:],
            "turn_messages": turn_messages[-12:],
            "existing_memories": manifest,
        }
        return (
            "You are the file-memory extraction agent for iGraph.\n"
            "Decide whether the latest turn contains durable cross-session memory.\n"
            "Save only: user role/preferences/goals, feedback about how to work, project context not derivable from files, or external reference pointers.\n"
            "Do NOT save: transient task details, code structure, git history, debug recipes, secrets, current conversation scratch state, or anything already documented.\n"
            "Prefer updating existing memories over creating duplicates.\n"
            "Return strict JSON only with this shape:\n"
            '{"should_write":false,"reason":"","operations":[{"op":"write|update|delete","type":"user|feedback|project|reference","name":"","description":"","content":"","path":"","reason":""}]}\n'
            f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}"
        )

    @staticmethod
    def _parse_decision(raw: str) -> dict[str, Any]:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            data = json.loads(text)
        except Exception:
            return {"should_write": False, "reason": "invalid_json", "operations": []}
        if not isinstance(data, dict):
            return {"should_write": False, "reason": "not_object", "operations": []}
        if not isinstance(data.get("operations"), list):
            data["operations"] = []
        return data

    def _resolve_topic_path(self, rel_path: str) -> Path:
        rel = rel_path.replace("\\", "/").strip().lstrip("/")
        if not rel or rel == ENTRYPOINT_NAME or rel.startswith("../") or "/../" in rel:
            raise ValueError("invalid memory path")
        path = (self.root / rel).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as e:
            raise ValueError("memory path escapes root") from e
        if path.name == ENTRYPOINT_NAME:
            raise ValueError("MEMORY.md is managed as an index, not a topic")
        return path

    def _read_topic(self, path: Path) -> tuple[dict[str, str], str]:
        if not path.exists():
            raise ValueError(f"memory topic not found: {self._relative(path)}")
        meta, content = _parse_frontmatter(path.read_text(encoding="utf-8"))
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("type", path.parent.name if path.parent.name in MEMORY_TYPES else "project")
        return meta, content

    def _scan_topics(self, *, include_content: bool = True) -> list[dict[str, str]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, str]] = []
        files = [
            p
            for p in self.root.rglob("*.md")
            if p.is_file() and p.name != ENTRYPOINT_NAME and "_archive" not in p.parts
        ][:MAX_SCAN_FILES]
        for path in files:
            try:
                meta, content = _parse_frontmatter(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append(
                {
                    "path": self._relative(path),
                    "name": meta.get("name") or path.stem,
                    "description": meta.get("description") or "",
                    "type": meta.get("type") or (path.parent.name if path.parent.name in MEMORY_TYPES else "project"),
                    "updated_at": meta.get("updated_at") or "",
                    "content": content if include_content else "",
                }
            )
        return rows

    @staticmethod
    def _score_topic(item: dict[str, str], terms: list[str]) -> float:
        haystack = " ".join(
            [
                item.get("path", ""),
                item.get("name", ""),
                item.get("description", ""),
                item.get("type", ""),
                item.get("content", ""),
            ]
        ).lower()
        score = 0.0
        for term in terms:
            if term and term in haystack:
                score += 1.0
        return score

    @staticmethod
    def _format_manifest(items: list[dict[str, str]]) -> str:
        lines = []
        for item in items[:MAX_SCAN_FILES]:
            prefix = f"[{item.get('type')}] " if item.get("type") else ""
            desc = f": {item.get('description')}" if item.get("description") else ""
            lines.append(f"- {prefix}{item.get('path')} ({item.get('name')}){desc}")
        return "\n".join(lines)

    def _update_index_entry(self, rel_path: str, name: str, description: str) -> None:
        entry = self._entrypoint_path()
        try:
            lines = entry.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = ["# Persistent File Memory"]
        normalized = rel_path.replace("\\", "/")
        new_line = f"- [{name}]({normalized}) - {description}"
        kept = [line for line in lines if f"]({normalized})" not in line and normalized not in line]
        if not kept:
            kept = ["# Persistent File Memory"]
        kept.append(new_line)
        _atomic_text_write(entry, "\n".join(kept))

    def _remove_index_entry(self, rel_path: str) -> None:
        entry = self._entrypoint_path()
        try:
            lines = entry.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        normalized = rel_path.replace("\\", "/")
        kept = [line for line in lines if f"]({normalized})" not in line and normalized not in line]
        _atomic_text_write(entry, "\n".join(kept))

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except Exception:
            return path.name

    @staticmethod
    def _limit(value: Any, *, default: int) -> int:
        try:
            return max(1, min(int(value or default), 20))
        except Exception:
            return default

    def _append_event(self, event: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        append_jsonl(str(self.events_path), {"ts": _now_iso(), **event})
