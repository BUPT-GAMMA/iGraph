"""Authentication and authorization boundary for Backend."""

from __future__ import annotations

from fastapi import HTTPException, Request

from backend.users import UserDirectory, UserRecord


def current_user(request: Request, users: UserDirectory) -> UserRecord:
    user_id = str(getattr(request.state, "authenticated_user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "请先登录"})
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
    return {"user_id": record.user_id, "enabled": record.enabled,
            "roles": list(record.roles), "allowed_models": list(record.allowed_models),
            "created_at": record.created_at, "updated_at": record.updated_at,
            "is_admin": "admin" in record.roles}

