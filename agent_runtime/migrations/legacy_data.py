"""Migrate legacy iGraph sessions, memories and Skills into AgentOS layout.

This command is intentionally not executed during application startup.  It is
safe to inspect with ``--dry-run`` first and records a checksum manifest so a
source item is never imported twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import httpx


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(str(path).encode("utf-8"))
    if path.is_file():
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def _legacy_messages(session_dir: Path) -> list[dict]:
    messages: list[dict] = []
    for record in _read_jsonl(session_dir / "message_list.jsonl"):
        if not record.get("visible") or record.get("kind") == "user_feedback":
            continue
        message = record.get("message") or {}
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        messages.append({
            "role": role,
            "content": str(record.get("display_content") or message.get("content") or ""),
            "ts": record.get("ts"),
        })
    return messages


def _skill_name(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value or "migrated-skill"


class Migrator:
    def __init__(self, *, data_root: Path, runtime_url: str, dry_run: bool) -> None:
        self.data_root = data_root
        self.runtime_url = runtime_url.rstrip("/")
        self.dry_run = dry_run
        self.manifest_path = data_root / "agent_runtime" / "migration-manifest.json"
        self.manifest = _read_json(self.manifest_path, {"version": 1, "items": {}})
        self.report = {"sessions": 0, "memories": 0, "skills": 0, "skipped": 0, "errors": []}

    def _done(self, key: str, path: Path) -> bool:
        return self.manifest.get("items", {}).get(key) == _checksum(path)

    def _mark(self, key: str, path: Path) -> None:
        self.manifest.setdefault("items", {})[key] = _checksum(path)

    def _request(self, method: str, path: str, *, user_id: str, **kwargs: Any) -> Any:
        if self.dry_run:
            return {}
        with httpx.Client(base_url=self.runtime_url, timeout=120, trust_env=False) as client:
            response = client.request(method, path, headers={"X-User-ID": user_id}, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def migrate_sessions(self, user_id: str, user_root: Path) -> None:
        sessions_root = user_root / "sessions"
        if not sessions_root.is_dir():
            return
        for session_dir in sessions_root.iterdir():
            meta_path = session_dir / "session_meta.json"
            if not session_dir.is_dir() or not meta_path.is_file():
                continue
            key = f"session:{user_id}:{session_dir.name}"
            if self._done(key, meta_path):
                self.report["skipped"] += 1
                continue
            meta = _read_json(meta_path, {})
            state = {
                "model": meta.get("model") or "qwen3.6-plus",
                "reasoning_mode": meta.get("reasoning_mode") or "direct",
                "planning_mode": meta.get("planning_mode") or "auto",
                "has_graph": bool(meta.get("last_has_graph", False)),
                "legacy_messages": _legacy_messages(session_dir),
                "legacy_import": True,
            }
            try:
                self._request(
                    "POST", "/sessions", user_id=user_id, params={"type": "agent"},
                    json={
                        "session_id": session_dir.name,
                        "session_name": meta.get("title") or "新会话",
                        "session_state": state,
                        "metadata": {"legacy_import": True, "owner": user_id},
                        "user_id": user_id,
                        "agent_id": "igraph-agent",
                    },
                )
                self._mark(key, meta_path)
                self.report["sessions"] += 1
            except Exception as exc:
                self.report["errors"].append({"item": key, "error": str(exc)})

    def _memory_candidates(self, user_root: Path):
        memory_root = user_root / "memory"
        paths = list(memory_root.rglob("*.json")) if memory_root.is_dir() else []
        external_playbooks = user_root / "playbooks.json"
        if external_playbooks.is_file():
            paths.append(external_playbooks)
        for path in paths:
            payload = _read_json(path, {})
            values = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
            for index, item in enumerate(values):
                if not isinstance(item, dict):
                    continue
                filename = path.name.lower()
                if "playbook" in filename or item.get("playbook_id") or item.get("problem_type"):
                    scope = "playbook"
                    content = item.get("approach") or item.get("value") or item.get("content") or item.get("memory")
                    category = item.get("lifecycle_status") or item.get("status") or "candidate"
                    key_name = item.get("problem_type") or item.get("key") or "playbook"
                elif "profile" in filename:
                    scope = "user_profile"
                    content = item.get("value") or item.get("content") or item.get("memory")
                    category = item.get("category") or "profile"
                    key_name = item.get("key") or item.get("name") or "profile"
                else:
                    scope = str(item.get("scope") or "long_term")
                    content = item.get("value") or item.get("content") or item.get("memory")
                    category = item.get("category") or "fact"
                    key_name = item.get("key") or "memory"
                if content is not None and content != "":
                    rendered = (
                        json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(content, (dict, list))
                        else str(content)
                    )
                    yield path, index, {**item, "scope": scope, "category": category, "key": key_name}, rendered

    def migrate_memories(self, user_id: str, user_root: Path) -> None:
        for path, index, item, content in self._memory_candidates(user_root) or []:
            key = f"memory:{user_id}:{path}:{index}"
            if self.manifest.get("items", {}).get(key) == hashlib.sha256(content.encode("utf-8")).hexdigest():
                self.report["skipped"] += 1
                continue
            topics = [str(item.get("scope") or "long_term"), str(item.get("category") or "fact"), str(item.get("key") or "memory")]
            try:
                self._request(
                    "POST", "/memories", user_id=user_id,
                    json={"memory": content[:5000], "user_id": user_id, "topics": topics},
                )
                self.manifest.setdefault("items", {})[key] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                self.report["memories"] += 1
            except Exception as exc:
                self.report["errors"].append({"item": key, "error": str(exc)})

    def migrate_skills(self, user_id: str, user_root: Path) -> None:
        roots = [(user_root / "skills", "users"), (user_root / "skills" / ".review_queue", "candidates")]
        for source_root, destination_kind in roots:
            if not source_root.is_dir():
                continue
            for source in source_root.iterdir():
                skill_file = source / "SKILL.md"
                if not source.is_dir() or not skill_file.is_file() or source.name == ".review_queue":
                    continue
                key = f"skill:{user_id}:{destination_kind}:{source.name}"
                if self._done(key, skill_file):
                    self.report["skipped"] += 1
                    continue
                target_name = _skill_name(source.name)
                target = self.data_root / "agent_runtime" / "skills" / destination_kind / user_id / target_name
                try:
                    if not self.dry_run:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(source, target, dirs_exist_ok=True)
                        migrated = (target / "SKILL.md").read_text(encoding="utf-8")
                        migrated = re.sub(r"(?m)^name:\s*[^\n]+", f"name: {target_name}", migrated, count=1)
                        (target / "SKILL.md").write_text(migrated, encoding="utf-8")
                    self._mark(key, skill_file)
                    self.report["skills"] += 1
                except Exception as exc:
                    self.report["errors"].append({"item": key, "error": str(exc)})

    def run(self) -> dict:
        users_root = self.data_root / "users"
        if users_root.is_dir():
            for user_root in users_root.iterdir():
                if not user_root.is_dir():
                    continue
                user_id = user_root.name
                self.migrate_sessions(user_id, user_root)
                self.migrate_memories(user_id, user_root)
                self.migrate_skills(user_id, user_root)
        if not self.dry_run:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default=os.getenv("IGRAPH_DATA_ROOT", "data"))
    parser.add_argument("--runtime-url", default=os.getenv("AGENT_RUNTIME_URL", "http://127.0.0.1:21112"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = Migrator(data_root=Path(args.data_root).resolve(), runtime_url=args.runtime_url, dry_run=args.dry_run).run()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
