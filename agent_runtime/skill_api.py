"""Product-facing Skill administration API owned by Agent Runtime.

Only approved directories are passed to Agno LocalSkills.  Candidate files
remain outside the runtime loader until an explicit approval operation moves
them into the active user directory.
"""

from __future__ import annotations

import io
import zipfile
import json
import re
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
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

    @router.post("/upload")
    async def upload_skill(request: Request, file: UploadFile = File(...)):
        uid = _user_id(request)
        if not (file.filename or "").lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="Skill 上传必须是 .zip 文件")
        raw = await file.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Skill 压缩包不能超过 2MB")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="Skill 压缩包无效") from exc
        members = archive.infolist()
        if len(members) > 256 or sum(item.file_size for item in members) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Skill 解压后不能超过 10MB 或 256 个文件")
        if any(chr(92) in item.filename or chr(0) in item.filename or item.flag_bits & 1 for item in members):
            raise HTTPException(status_code=400, detail="Skill 压缩包包含不支持的条目")
        if any(
            Path(item.filename).is_absolute()
            or ".." in Path(item.filename).parts
            or ((item.external_attr >> 16) & 0o170000) == 0o120000
            for item in members
        ):
            raise HTTPException(status_code=400, detail="Skill 压缩包包含不安全路径")
        skill_files = [item for item in members if Path(item.filename).name == "SKILL.md"]
        if len(skill_files) != 1:
            raise HTTPException(status_code=400, detail="压缩包必须且只能包含一个 SKILL.md")
        root = Path(skill_files[0].filename).parent
        # Derive the skill name strictly from the directory name inside the zip.
        # Never fall back to the client-supplied filename — that lets the uploader
        # influence the resulting path on disk.
        dir_name = root.name
        if not dir_name:
            raise HTTPException(status_code=400, detail="压缩包内 SKILL.md 必须放在一个命名子目录下")
        name = _safe_name(dir_name)
        target = candidate_skills_path(config, uid) / name
        if target.exists():
            raise HTTPException(status_code=409, detail="Skill 已存在")
        target.mkdir(parents=True)
        try:
            for item in members:
                rel = Path(item.filename)
                if rel.parts[:len(root.parts)] != root.parts:
                    continue
                if item.is_dir() or item.filename.endswith("/"):
                    continue
                destination = target / Path(*rel.parts[len(root.parts):])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
            skill_file = target / "SKILL.md"
            if not skill_file.is_file():
                raise HTTPException(status_code=400, detail="Skill 缺少 SKILL.md")
            skill_file.write_text(_normalize_body_name(skill_file.read_text(encoding="utf-8"), name), encoding="utf-8")
        except (UnicodeError, zipfile.BadZipFile, RuntimeError, OSError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Skill 压缩包内容无效") from exc
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return {"success": True, "name": name, "source": "review_queue"}

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
    def get_skill(name: str, request: Request, source: str = "all"):
        uid = _user_id(request)
        normalized = _safe_name(name)
        roots = {
            "bundled": (bundled_skills_path(config), "bundled"),
            "user": (user_skills_path(config, uid), "user"),
            "review_queue": (candidate_skills_path(config, uid), "review_queue"),
        }
        if source == "all":
            candidates = [
                (bundled_skills_path(config), "bundled"),
                (user_skills_path(config, uid), "user"),
                (candidate_skills_path(config, uid), "review_queue"),
            ]
        elif source in roots:
            candidates = [roots[source]]
        else:
            raise HTTPException(status_code=400, detail="Invalid Skill source")
        candidates = [(root / normalized, skill_source) for root, skill_source in candidates]
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
        # This route is internal-only; the caller must present a valid service
        # token via X-Service-Token to prevent lateral deletion of other users'
        # skills when the runtime is inadvertently reachable from the outside.
        _user_id(request)
        service_token = request.headers.get("X-Service-Token", "").strip()
        expected = str(getattr(config, "internal_service_token", "") or "").strip()
        if expected and service_token != expected:
            raise HTTPException(status_code=403, detail="forbidden: internal route requires service token")
        # Sanitise target_user_id to guard against path-traversal variants
        # that survive _user_component (e.g. "...", ". .").
        cleaned = target_user_id.strip()
        if not cleaned or cleaned in {".", ".."} or cleaned.startswith("/") or ".." in cleaned.split("/"):
            raise HTTPException(status_code=400, detail="invalid user id")
        removed: list[str] = []
        for root in (user_skills_path(config, cleaned), candidate_skills_path(config, cleaned)):
            if root.is_dir():
                shutil.rmtree(root)
                removed.append(str(root))
        return {"success": True, "user_id": cleaned, "removed": removed}

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
        if source == "bundled":
            raise HTTPException(status_code=403, detail="Bundled Skill cannot be deleted")
        if source not in {"user", "review_queue"}:
            raise HTTPException(status_code=400, detail="Invalid Skill source")
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
