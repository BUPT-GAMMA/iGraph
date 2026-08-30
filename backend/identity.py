"""Authentication and authorization boundary for Backend."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import HTTPException, Request

from backend.users import UserDirectory, UserRecord


def current_user(request: Request, users: UserDirectory) -> UserRecord:
    # Compatibility contract: the current frontend sends X-User-ID.  JWT can
    # be introduced at this boundary without changing Agent Runtime ownership.
    user_id = request.headers.get("X-User-ID", "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="缺少 X-User-ID 请求头")
    record = users.get(user_id)
    if record is None or not record.enabled:
        raise HTTPException(status_code=401, detail="无效或已禁用的用户")
    return record


def require_admin(request: Request, users: UserDirectory) -> UserRecord:
    record = current_user(request, users)
    if "admin" not in record.roles:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return record


def public_user(record: UserRecord) -> dict:
    payload = asdict(record)
    payload["is_admin"] = "admin" in record.roles
    return payload

