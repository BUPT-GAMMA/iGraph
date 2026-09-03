"""Product-facing Skill administration API owned by Agent Runtime.

Only approved directories are passed to Agno LocalSkills.  Candidate files
remain outside the runtime loader until an explicit approval operation moves
them into the active user directory.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.skills_runtime import (
    bundled_skills_path,
    candidate_skills_path,
    user_skills_path,
)


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillWrite(BaseModel):
    name: str
    body: str = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)
    destination: Literal["user", "review_queue"] = "user"


class SkillUpdate(BaseModel):
    body: str = Field(min_length=1)
    source: Literal["user", "review_queue"] = "user"


class SkillApproval(BaseModel):
    target_name: str = ""
    playbook_id: str = ""


class SkillRejection(BaseModel):
    reason: str = ""
    remove_files: bool = True


def _user_id(request: Request) -> str:
    value = str(getattr(request.state, "user_id", "") or request.headers.get("X-User-ID", "")).strip()
    if not value:
        raise HTTPException(status_code=401, detail="missing user identity")
    return value


def _safe_name(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not _NAME_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Skill name must use lowercase letters, digits and hyphens")
    return normalized


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result: dict = {}
    stack: list[tuple[int, dict]] = [(-1, result)]
    for raw in text[3:end].splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, sep, value = raw.strip().partition(":")
        if not sep:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else result
        value = value.strip()
        if not value:
            child: dict = {}
            parent[key.strip()] = child
            stack.append((indent, child))
        else:
            parent[key.strip()] = value.strip('"\'')
    return result


def _normalize_body_name(body: str, name: str) -> str:
    if body.startswith("---"):
        if re.search(r"(?m)^name:\s*[^\r\n]+", body):
            return re.sub(r"(?m)^name:\s*[^\r\n]+", f"name: {name}", body, count=1)
        return body.replace("---", f"---\nname: {name}", 1)
    return f"---\nname: {name}\ndescription: 用户维护的 iGraph Skill\n---\n\n{body}"


def _entry(skill_dir: Path, source: str) -> dict:
    skill_file = skill_dir / "SKILL.md"
    meta = _frontmatter(skill_file)
    skill_meta = meta.get("metadata")
    if not isinstance(skill_meta, dict):
        skill_meta = {}
    product_meta: dict = {}
    metadata_file = skill_dir / "metadata.json"
    if metadata_file.exists():
        try:
            product_meta = json.loads(metadata_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            product_meta = {}
    return {
        "name": str(meta.get("name") or skill_dir.name),
        "description": str(meta.get("description") or product_meta.get("description") or ""),
        "domain": str(meta.get("domain") or skill_meta.get("domain") or product_meta.get("domain") or "general"),
        "version": str(meta.get("version") or skill_meta.get("version") or product_meta.get("version") or "1.0.0"),
        "risk_level": str(
            meta.get("risk_level") or skill_meta.get("risk_level") or product_meta.get("risk_level") or "low"
        ),
        "source": source,
        "path": str(skill_file),
        "playbook_id": str(product_meta.get("playbook_id") or ""),
        "condition": product_meta.get("condition") or {},
    }


def _iter_skills(root: Path, source: str):
    if not root.exists():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            yield _entry(child, source)


def create_skill_router(config: AgentRuntimeConfig) -> APIRouter:
    router = APIRouter(prefix="/internal/skills", tags=["iGraph Skills"])

    @router.get("")
    def list_skills(request: Request, source: str = "all"):
        uid = _user_id(request)
        rows: list[dict] = []
        if source in {"all", "bundled"}:
            rows.extend(_iter_skills(bundled_skills_path(config), "bundled") or [])
        if source in {"all", "user"}:
            rows.extend(_iter_skills(user_skills_path(config, uid), "user") or [])
        if source in {"all", "review_queue"}:
            rows.extend(_iter_skills(candidate_skills_path(config, uid), "review_queue") or [])
        return {"success": True, "skills": rows}

    @router.get("/{name}")
    def get_skill(name: str, request: Request):
        uid = _user_id(request)
        normalized = _safe_name(name)
        candidates = [
            (bundled_skills_path(config) / normalized, "bundled"),
            (user_skills_path(config, uid) / normalized, "user"),
            (candidate_skills_path(config, uid) / normalized, "review_queue"),
        ]
        for skill_dir, source in candidates:
            skill_file = skill_dir / "SKILL.md"
            if skill_file.is_file():
                payload = _entry(skill_dir, source)
                payload["content"] = skill_file.read_text(encoding="utf-8")
                return {"success": True, "skill": payload}
        raise HTTPException(status_code=404, detail="Skill not found")

    @router.post("")
    def create_skill(req: SkillWrite, request: Request):
        uid = _user_id(request)
        name = _safe_name(req.name)
        root = user_skills_path(config, uid) if req.destination == "user" else candidate_skills_path(config, uid)
        skill_dir = root / name
        if skill_dir.exists():
            raise HTTPException(status_code=409, detail="Skill already exists")
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_normalize_body_name(req.body, name), encoding="utf-8")
        if req.metadata:
            (skill_dir / "metadata.json").write_text(
                json.dumps(req.metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return {"success": True, "ok": True, "name": name, "source": req.destination}

    @router.delete("/users/{target_user_id}/all")
    def delete_user_skills(target_user_id: str, request: Request):
        # This route is private to Backend; consuming the request identity keeps
        # accidental anonymous calls out when the service is exposed directly.
        _user_id(request)
        removed: list[str] = []
        for root in (user_skills_path(config, target_user_id), candidate_skills_path(config, target_user_id)):
            if root.is_dir():
                shutil.rmtree(root)
                removed.append(str(root))
        return {"success": True, "user_id": target_user_id, "removed": removed}

    @router.put("/{name}")
    def update_skill(name: str, req: SkillUpdate, request: Request):
        uid = _user_id(request)
        root = user_skills_path(config, uid) if req.source == "user" else candidate_skills_path(config, uid)
        skill_file = root / _safe_name(name) / "SKILL.md"
        if not skill_file.is_file():
            raise HTTPException(status_code=404, detail="Skill not found")
        skill_file.write_text(_normalize_body_name(req.body, _safe_name(name)), encoding="utf-8")
        return {"success": True, "name": name, "source": req.source}

    @router.delete("/{name}")
    def delete_skill(name: str, request: Request, source: str = "user", reason: str = ""):
        uid = _user_id(request)
        root = user_skills_path(config, uid) if source == "user" else candidate_skills_path(config, uid)
        skill_dir = root / _safe_name(name)
        if not skill_dir.is_dir():
            raise HTTPException(status_code=404, detail="Skill not found")
        shutil.rmtree(skill_dir)
        return {"success": True, "name": name, "source": source, "reason": reason}

    @router.post("/{name}/approve")
    def approve_skill(name: str, req: SkillApproval, request: Request):
        uid = _user_id(request)
        source_name = _safe_name(name)
        target_name = _safe_name(req.target_name or source_name)
        source_dir = candidate_skills_path(config, uid) / source_name
        target_dir = user_skills_path(config, uid) / target_name
        if not source_dir.is_dir():
            raise HTTPException(status_code=404, detail="Candidate Skill not found")
        if target_dir.exists():
            raise HTTPException(status_code=409, detail="Approved Skill already exists")
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_dir), str(target_dir))
        skill_file = target_dir / "SKILL.md"
        skill_file.write_text(
            _normalize_body_name(skill_file.read_text(encoding="utf-8"), target_name),
            encoding="utf-8",
        )
        metadata_file = target_dir / "metadata.json"
        metadata: dict = {}
        if metadata_file.exists():
            try:
                metadata.update(json.loads(metadata_file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        metadata.update({"approved_at": time.time(), "playbook_id": req.playbook_id or metadata.get("playbook_id", "")})
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "ok": True, "name": target_name, "source": "user"}

    @router.post("/{name}/reject")
    def reject_skill(name: str, req: SkillRejection, request: Request):
        uid = _user_id(request)
        skill_dir = candidate_skills_path(config, uid) / _safe_name(name)
        if not skill_dir.is_dir():
            raise HTTPException(status_code=404, detail="Candidate Skill not found")
        if req.remove_files:
            shutil.rmtree(skill_dir)
        else:
            (skill_dir / "REJECTED.json").write_text(
                json.dumps({"reason": req.reason, "rejected_at": time.time()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return {"success": True, "ok": True, "name": name, "reason": req.reason}

    return router
