"""Single-user identity boundary for Backend."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from backend.users import UserDirectory, UserRecord


def _origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            return None
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        suffix = "" if port is None or (parsed.scheme, port) in {("http", 80), ("https", 443)} else f":{port}"
        return f"{parsed.scheme}://{host}{suffix}"
    except ValueError:
        return None


def _trusted_proxy(request: Request, trusted_proxies: tuple[str, ...]) -> bool:
    try:
        address = ipaddress.ip_address(request.client.host) if request.client else None
        return address is not None and any(address in ipaddress.ip_network(value) for value in trusted_proxies)
    except ValueError:
        return False


def request_scheme(request: Request, trusted_proxies: tuple[str, ...]) -> str:
    value = request.headers.get("x-forwarded-proto", "")
    if _trusted_proxy(request, trusted_proxies) and value in {"http", "https"}:
        return value
    return request.url.scheme


def verify_write_origin(request: Request, *, trusted_proxies: tuple[str, ...], allowed_origins: tuple[str, ...]) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    supplied = request.headers.get("origin")
    if supplied is None:
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, {"code": "AUTH_ORIGIN_FORBIDDEN", "message": "请求来源无效"})
        return
    scheme = request_scheme(request, trusted_proxies)
    permitted = {_origin(f"{scheme}://{request.headers.get('host', '')}")}
    permitted.update(_origin(value) for value in allowed_origins)
    origin = _origin(supplied)
    if origin is None or origin not in permitted:
        raise HTTPException(403, {"code": "AUTH_ORIGIN_FORBIDDEN", "message": "请求来源无效"})


class SingleUserMiddleware:
    """Bind public API requests to the deployment owner, never a browser ID.

    Internal service authentication and ownership headers remain unchanged.
    This workspace is served through the deployment's loopback/SSH boundary;
    cross-site writes are still rejected because no login cookie protects it.
    """

    def __init__(self, app, user_id: str, *, trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1"),
                 allowed_origins: tuple[str, ...] = ()):
        if any(_origin(value) is None for value in allowed_origins):
            raise ValueError("BACKEND_AUTH_ALLOWED_ORIGINS 必须为明确的 HTTP(S) 来源，不能使用通配符")
        self.app = app
        self.user_id = user_id
        self.trusted_proxies = trusted_proxies
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            return await self.app(scope, receive, send)
        try:
            verify_write_origin(Request(scope), trusted_proxies=self.trusted_proxies,
                                allowed_origins=self.allowed_origins)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers={"Cache-Control": "no-store"})
            return await response(scope, receive, send)
        scope.setdefault("state", {})["authenticated_user_id"] = self.user_id
        scope["headers"] = [(key, value) for key, value in scope.get("headers", [])
                            if key.lower() != b"x-user-id"] + [(b"x-user-id", self.user_id.encode("utf-8"))]
        await self.app(scope, receive, send)


def current_user(request: Request, users: UserDirectory) -> UserRecord:
    user_id = str(getattr(request.state, "authenticated_user_id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "缺少工作区身份"})
    record = users.get(user_id)
    if record is None or not record.enabled:
        raise HTTPException(status_code=401, detail="无效或已禁用的用户")
    return record
