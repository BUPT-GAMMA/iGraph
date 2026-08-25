from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from agent_runtime.agent.memory.store import append_jsonl
from agent_runtime.agent.skills.guard import scan_skill_content, validate_supporting_file
from agent_runtime.agent.skills.paths import (
    per_user_review_queue_dir,
    per_user_skill_events_path,
    per_user_skills_dir,
    review_queue_dir,
    skill_events_path,
    user_skills_dir,
)
from agent_runtime.agent.skills.registry import clear_skill_caches

_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{2,80}$")
_MAX_FILE_CHARS = 64 * 1024
_MAX_SKILL_CHARS = 256 * 1024


class SkillManager:
    """Safe writer for user-owned and review-queue skills."""

    def __init__(self, user_id: str = "") -> None:
        self._user_id = user_id.strip()

    def manage(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        action = (action or "").strip().lower()
        if action == "create":
            return self.create(
                name=str(args.get("name") or ""),
                body=str(args.get("body") or args.get("content") or ""),
                metadata=args.get("metadata") if isinstance(args.get("metadata"), dict) else {},
                destination=str(args.get("destination") or "user"),
            )
        if action == "write_file":
            return self.write_file(
                name=str(args.get("name") or ""),
                file_path=str(args.get("file_path") or ""),
                content=str(args.get("content") or ""),
                destination=str(args.get("destination") or "user"),
            )
        if action == "remove_file":
            return self.remove_file(
                name=str(args.get("name") or ""),
                file_path=str(args.get("file_path") or ""),
                destination=str(args.get("destination") or "user"),
            )
        if action == "approve":
            return self.approve(
                candidate_name=str(args.get("name") or args.get("candidate_name") or ""),
                target_name=str(args.get("target_name") or ""),
                playbook_id=str(args.get("playbook_id") or ""),
            )
        if action == "reject":
            return self.reject(
                candidate_name=str(args.get("name") or args.get("candidate_name") or ""),
                reason=str(args.get("reason") or ""),
                remove_files=bool(args.get("remove_files", False)),
            )
        return self._error("unsupported_action", f"unsupported skill_manage action: {action}")

    def create(self, *, name: str, body: str, metadata: dict[str, Any], destination: str = "user") -> dict[str, Any]:
        name = self._validate_name(name)
        root = self._destination_root(destination)
        skill_dir = root / name
        if skill_dir.exists():
            return self._error("already_exists", f"skill {name} already exists")
        body_text = self._render_skill_body(name=name, body=body, metadata=metadata)
        self._validate_content(body_text)
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_text(body_text, encoding="utf-8")
        self._check_total_size(skill_dir)
        clear_skill_caches()
        self._event("skill_created", {"skill_name": name, "destination": destination})
        return {"ok": True, "action": "create", "name": name, "destination": destination, "path": str(skill_dir)}

    def write_file(self, *, name: str, file_path: str, content: str, destination: str = "user") -> dict[str, Any]:
        name = self._validate_name(name)
        root = self._destination_root(destination)
        skill_dir = root / name
        if not skill_dir.exists():
            return self._error("not_found", f"skill {name} not found")
        target = self._resolve_write_path(skill_dir, file_path)
        if len(content) > _MAX_FILE_CHARS:
            return self._error("file_too_large", "file content exceeds limit")
        self._validate_content(content)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._check_total_size(skill_dir)
        clear_skill_caches()
        self._event("skill_file_written", {"skill_name": name, "destination": destination, "file_path": file_path})
        return {"ok": True, "action": "write_file", "name": name, "file_path": file_path}

    def remove_file(self, *, name: str, file_path: str, destination: str = "user") -> dict[str, Any]:
        name = self._validate_name(name)
        root = self._destination_root(destination)
        skill_dir = root / name
        if not skill_dir.exists():
            return self._error("not_found", f"skill {name} not found")
        if Path(file_path).name == "SKILL.md":
            return self._error("protected_file", "cannot remove SKILL.md with remove_file")
        target = validate_supporting_file(skill_dir, file_path)
        target.unlink(missing_ok=True)
        clear_skill_caches()
        self._event("skill_file_removed", {"skill_name": name, "destination": destination, "file_path": file_path})
        return {"ok": True, "action": "remove_file", "name": name, "file_path": file_path}

    def approve(self, *, candidate_name: str, target_name: str = "", playbook_id: str = "") -> dict[str, Any]:
        candidate_name = self._validate_name(candidate_name)
        target = self._validate_name(target_name or candidate_name)
        src = (per_user_review_queue_dir(self._user_id) if self._user_id else review_queue_dir()) / candidate_name
        dst = (per_user_skills_dir(self._user_id) if self._user_id else user_skills_dir()) / target
        if not src.exists():
            return self._error("not_found", f"candidate {candidate_name} not found")
        if dst.exists():
            return self._error("already_exists", f"user skill {target} already exists")
        resolved_playbook_id = playbook_id or self._read_candidate_playbook_id(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        promoted = self._mark_playbook_promoted(resolved_playbook_id, target) if resolved_playbook_id else False
        clear_skill_caches()
        self._event(
            "skill_approved",
            {
                "candidate_name": candidate_name,
                "skill_name": target,
                "playbook_id": resolved_playbook_id,
                "playbook_promoted": promoted,
            },
        )
        return {
            "ok": True,
            "action": "approve",
            "candidate_name": candidate_name,
            "name": target,
            "path": str(dst),
            "playbook_id": resolved_playbook_id,
            "playbook_promoted": promoted,
        }

    def reject(self, *, candidate_name: str, reason: str = "", remove_files: bool = False) -> dict[str, Any]:
        candidate_name = self._validate_name(candidate_name)
        path = (per_user_review_queue_dir(self._user_id) if self._user_id else review_queue_dir()) / candidate_name
        if not path.exists():
            return self._error("not_found", f"candidate {candidate_name} not found")
        if remove_files:
            shutil.rmtree(path)
        else:
            marker = path / "REJECTED.json"
            marker.write_text(json.dumps({"reason": reason, "ts": self._ts()}, ensure_ascii=False, indent=2), encoding="utf-8")
        clear_skill_caches()
        self._event("skill_rejected", {"candidate_name": candidate_name, "reason": reason, "remove_files": remove_files})
        return {"ok": True, "action": "reject", "candidate_name": candidate_name, "remove_files": remove_files}

    @staticmethod
    def _validate_name(name: str) -> str:
        value = str(name or "").strip()
        if not _NAME_RE.match(value):
            raise ValueError("invalid skill name")
        return value

    def _destination_root(self, destination: str) -> Path:
        dest = (destination or "user").strip().lower()
        if dest == "user":
            return per_user_skills_dir(self._user_id) if self._user_id else user_skills_dir()
        if dest == "review_queue":
            return per_user_review_queue_dir(self._user_id) if self._user_id else review_queue_dir()
        raise ValueError("invalid skill destination")

    @staticmethod
    def _validate_content(content: str) -> None:
        reason = scan_skill_content(content)
        if reason:
            raise ValueError(f"skill content rejected: {reason}")

    @staticmethod
    def _resolve_write_path(skill_dir: Path, file_path: str) -> Path:
        rel = str(file_path or "").strip()
        if not rel:
            raise ValueError("file_path required")
        if Path(rel).is_absolute():
            raise ValueError("file_path must be relative")
        root = skill_dir.resolve()
        target = (skill_dir / rel).resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise ValueError("supporting file path escapes skill directory")
        return target

    @staticmethod
    def _check_total_size(skill_dir: Path) -> None:
        total = 0
        for path in skill_dir.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        if total > _MAX_SKILL_CHARS:
            raise ValueError("skill directory exceeds size limit")

    @staticmethod
    def _render_skill_body(*, name: str, body: str, metadata: dict[str, Any]) -> str:
        if body.strip().startswith("---"):
            return body.strip() + "\n"
        description = str(metadata.get("description") or name).strip()
        domain = str(metadata.get("domain") or "user").strip()
        risk_level = str(metadata.get("risk_level") or "medium").strip()
        version = str(metadata.get("version") or "1.0.0").strip()
        return (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"version: {version}\n"
            f"domain: {domain}\n"
            f"risk_level: {risk_level}\n"
            "---\n\n"
            f"{body.strip()}\n"
        )

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        events_path = per_user_skill_events_path(self._user_id) if self._user_id else skill_events_path()
        append_jsonl(str(events_path), {"ts": self._ts(), "type": event_type, **payload})

    @staticmethod
    def _ts() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _error(self, code: str, message: str) -> dict[str, Any]:
        self._event("skill_write_rejected", {"code": code, "message": message})
        return {"ok": False, "error": code, "message": message}

    @staticmethod
    def _read_candidate_playbook_id(candidate_dir: Path) -> str:
        meta_path = candidate_dir / "metadata.json"
        if not meta_path.exists():
            return ""
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str(data.get("playbook_id") or "").strip()

    def _candidate_playbook_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self._user_id:
            try:
                import backend.data_paths as _dp

                paths.append(_dp.user_memory_dir(self._user_id) / "playbooks.json")
            except Exception:
                pass
        try:
            from agent_runtime.agent.memory.paths import MemoryPathResolver

            root = MemoryPathResolver().root
            if root.exists():
                paths.extend(root.glob("**/playbooks.json"))
        except Exception:
            pass

        if not self._user_id:
            memory_dir = Path(__file__).resolve().parents[1] / "memory"
            paths.append(memory_dir / "default_user" / "problem_solving_playbooks.json")
            paths.append(memory_dir / "memory" / "default_user" / "problem_solving_playbooks.json")
        # Preserve order while dropping duplicates.
        seen: set[str] = set()
        unique: list[Path] = []
        for path in paths:
            key = str(path.resolve()) if path.exists() else str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def _mark_playbook_promoted(self, playbook_id: str, skill_name: str) -> bool:
        playbook_id = str(playbook_id or "").strip()
        if not playbook_id:
            return False
        changed = False
        for path in self._candidate_playbook_paths():
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = data.get("items")
            if not isinstance(items, list):
                continue
            local_changed = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("playbook_id") or "").strip() != playbook_id:
                    continue
                item["status"] = "promoted_to_skill"
                item["lifecycle_status"] = "promoted_to_skill"
                item["candidate_skill_name"] = skill_name
                item["promotion_reason"] = "approved via skill_manage"
                item["updated_at"] = self._ts()
                local_changed = True
            if local_changed:
                try:
                    from agent_runtime.agent.memory.store import atomic_json_write

                    data["updated_at"] = self._ts()
                    atomic_json_write(str(path), data)
                    changed = True
                except Exception:
                    pass
        return changed
