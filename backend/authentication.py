"""Password credentials and browser sessions owned only by the public Backend."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from backend.identity import current_user, public_user
from backend.users import UserDirectory, UserRecord

AUTH_COOKIE = "igraph_session"
PASSWORD_ITERATIONS = 600_000
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,32}\Z", re.ASCII)


def normalize_username(value: str) -> str:
    value = str(value or "").strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("用户名须为 3–32 位字母、数字、下划线或连字符")
    return value.lower()


def validate_password(value: str) -> None:
    if not isinstance(value, str) or not 6 <= len(value) <= 128:
        raise ValueError("密码长度须为 6–128 个字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("密码包含无效字符") from exc


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(PASSWORD_ITERATIONS),
                     base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        rounds = int(iterations)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= rounds <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     base64.b64decode(salt, validate=True), rounds)
        return hmac.compare_digest(actual, base64.b64decode(expected, validate=True))
    except (ValueError, TypeError):
        return False


class AuthenticationStore:
    def __init__(self, path: Path, users: UserDirectory, *, session_ttl_seconds: int = 604_800):
        self.path = Path(path)
        self.users = users
        self.session_ttl_seconds = max(300, min(int(session_ttl_seconds), 30 * 86400))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS credentials (
                    username TEXT PRIMARY KEY, user_id TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS rate_limits (
                    limiter_key TEXT PRIMARY KEY, window_start REAL NOT NULL, attempts INTEGER NOT NULL
                );
            """)
        # Unknown accounts still pay the same password verification cost.
        self._dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def consume_limit(self, key: str, *, attempts: int, window_seconds: int) -> None:
        now = time.time()
        key = hashlib.sha256(key.encode("utf-8")).hexdigest()
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM rate_limits WHERE limiter_key=?", (key,)).fetchone()
            if row and now - row["window_start"] < window_seconds:
                if row["attempts"] >= attempts:
                    retry = max(1, math.ceil(window_seconds - (now - row["window_start"])))
                    raise HTTPException(429, {"code": "AUTH_RATE_LIMITED", "message": "尝试次数过多，请稍后重试"},
                                        headers={"Retry-After": str(retry)})
                connection.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE limiter_key=?", (key,))
            else:
                connection.execute("INSERT OR REPLACE INTO rate_limits VALUES (?, ?, 1)", (key, now))
            connection.execute("DELETE FROM rate_limits WHERE window_start<?", (now - 86400,))

    def register(self, username: str, password: str) -> UserRecord:
        username = normalize_username(username)
        validate_password(password)
        encoded = password_hash(password)
        with self._connection(write=True) as connection:
            reserved = {str(value).lower() for value in self.users._seed_ids()}
            reserved.add(self.users.admin_user_id.lower())
            reserved.update(record.user_id.lower() for record in self.users.list())
            if username in reserved or connection.execute(
                "SELECT 1 FROM credentials WHERE username=?", (username,)
            ).fetchone():
                raise HTTPException(409, {"code": "AUTH_USERNAME_TAKEN", "message": "该用户名已被使用"})
            connection.execute("INSERT INTO credentials VALUES (?, ?, ?, ?)",
                               (username, username, encoded, time.time()))
            try:
                return self.users.create(UserRecord(user_id=username))
            except ValueError as exc:
                raise HTTPException(409, {"code": "AUTH_USERNAME_TAKEN", "message": "该用户名已被使用"}) from exc

    def bootstrap(self, user_id: str, password: str) -> bool:
        """Trusted deployment helper: set an existing user's password exactly once."""
        username = normalize_username(user_id)
        validate_password(password)
        record = self.users.get(user_id)
        if record is None:
            raise ValueError("只能为已经存在的帐号初始化密码")
        encoded = password_hash(password)
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM credentials WHERE username=? OR user_id=?",
                                  (username, record.user_id)).fetchone():
                return False
            connection.execute("INSERT INTO credentials VALUES (?, ?, ?, ?)",
                               (username, record.user_id, encoded, time.time()))
        return True

    def login(self, username: str, password: str) -> UserRecord:
        try:
            username = normalize_username(username)
            validate_password(password)
        except ValueError:
            username = ""
            password = ""  # Bound the cost for malformed input without logging it.
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM credentials WHERE username=?", (username,)).fetchone()
        valid = verify_password(password, row["password_hash"] if row else self._dummy_hash)
        record = self.users.get(row["user_id"]) if row else None
        if not valid or record is None or not record.enabled:
            raise HTTPException(401, {"code": "AUTH_INVALID_CREDENTIALS", "message": "用户名或密码不正确"})
        return record

    def new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(48)
        now = time.time()
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                               (hashlib.sha256(token.encode()).hexdigest(), user_id, now,
                                now + self.session_ttl_seconds))
        return token

    def authenticate(self, token: str) -> UserRecord | None:
        if not token or len(token) > 128:
            return None
        with self._connection() as connection:
            row = connection.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
                                     (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        record = self.users.get(row["user_id"]) if row else None
        return record if record is not None and record.enabled else None

    def revoke(self, token: str) -> None:
        if token and len(token) <= 128:
            with self._connection(write=True) as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?",
                                   (hashlib.sha256(token.encode()).hexdigest(),))

    def revoke_user(self, user_id: str) -> None:
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            # Keep the username reserved after deletion; re-creating a metadata
            # record must not revive a deleted account's password.
            connection.execute("UPDATE credentials SET password_hash='' WHERE user_id=?", (user_id,))


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


class AuthenticationMiddleware:
    """Pure ASGI gate, preserving streaming and internal service authentication."""
    public_paths = {"/api/auth/login", "/api/auth/register", "/api/auth/logout"}

    def __init__(self, app, *, auth: AuthenticationStore, trusted_proxies: tuple[str, ...],
                 allowed_origins: tuple[str, ...]):
        self.app = app
        self.auth = auth
        self.trusted_proxies = trusted_proxies
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            verify_write_origin(request, trusted_proxies=self.trusted_proxies, allowed_origins=self.allowed_origins)
            if path not in self.public_paths and request.method != "OPTIONS":
                record = await run_in_threadpool(self.auth.authenticate, request.cookies.get(AUTH_COOKIE, ""))
                if record is None:
                    raise HTTPException(401, {"code": "AUTH_REQUIRED", "message": "请先登录"})
                expected_account = request.headers.get("x-igraph-account")
                if expected_account is not None and expected_account != record.user_id:
                    raise HTTPException(409, {"code": "ACCOUNT_CHANGED", "message": "账户已在其他标签页切换，请刷新后重试"})
                scope.setdefault("state", {})["authenticated_user_id"] = record.user_id
                # Some compatibility routes read the header directly. They must
                # receive the cookie owner's identity, never the caller's claim.
                scope["headers"] = [(key, value) for key, value in scope.get("headers", [])
                                     if key.lower() != b"x-user-id"] + [(b"x-user-id", record.user_id.encode("utf-8"))]
            async def private_send(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    vary = [part.strip() for key, value in headers if key.lower() == b"vary"
                            for part in value.decode("latin-1").split(",") if part.strip()]
                    if "cookie" not in {part.lower() for part in vary}:
                        vary.append("Cookie")
                    headers = [(key, value) for key, value in headers
                               if key.lower() not in {b"cache-control", b"vary"}]
                    headers.extend([(b"cache-control", b"private, no-store"),
                                    (b"vary", ", ".join(vary).encode("latin-1"))])
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, private_send)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                    headers={**(exc.headers or {}), "Cache-Control": "no-store"})
            await response(scope, receive, send)


class CredentialsRequest(BaseModel):
    username: str = Field(max_length=64)
    password: SecretStr = Field(max_length=128)


def install_authentication(app, auth: AuthenticationStore, *, trusted_proxies: tuple[str, ...],
                           allowed_origins: tuple[str, ...]) -> None:
    if any(_origin(value) is None for value in allowed_origins):
        raise ValueError("BACKEND_AUTH_ALLOWED_ORIGINS 必须为明确的 HTTP(S) 来源，不能使用通配符")
    app.state.auth = auth
    app.state.users = auth.users
    app.add_middleware(AuthenticationMiddleware, auth=auth, trusted_proxies=trusted_proxies,
                       allowed_origins=allowed_origins)
    router = APIRouter(prefix="/api/auth")

    def signed_in(request: Request, response: Response, record: UserRecord):
        auth.revoke(request.cookies.get(AUTH_COOKIE, ""))
        response.set_cookie(AUTH_COOKIE, auth.new_session(record.user_id), max_age=auth.session_ttl_seconds,
                            path="/", httponly=True, samesite="lax",
                            secure=request_scheme(request, trusted_proxies) == "https")
        response.headers["Cache-Control"] = "no-store"
        return public_user(record)

    @router.post("/register", status_code=201)
    def register(req: CredentialsRequest, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        auth.consume_limit("register:ip:" + address, attempts=12, window_seconds=3600)
        try:
            record = auth.register(req.username, req.password.get_secret_value())
        except ValueError as exc:
            raise HTTPException(400, {"code": "AUTH_INVALID_INPUT", "message": str(exc)}) from exc
        return signed_in(request, response, record)

    @router.post("/login")
    def login(req: CredentialsRequest, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        auth.consume_limit("login:ip:" + address, attempts=60, window_seconds=900)
        auth.consume_limit("login:name:" + req.username.strip().lower()[:32], attempts=15, window_seconds=900)
        return signed_in(request, response, auth.login(req.username, req.password.get_secret_value()))

    @router.get("/me")
    def me(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return public_user(current_user(request, auth.users))

    @router.post("/logout")
    def logout(request: Request, response: Response):
        auth.revoke(request.cookies.get(AUTH_COOKIE, ""))
        response.delete_cookie(AUTH_COOKIE, path="/", httponly=True, samesite="lax",
                               secure=request_scheme(request, trusted_proxies) == "https")
        response.headers["Cache-Control"] = "no-store"
        return {"success": True}

    @app.exception_handler(RequestValidationError)
    async def safe_auth_validation(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/auth/login":
            return JSONResponse({"detail": {"code": "AUTH_INVALID_CREDENTIALS",
                                           "message": "用户名或密码不正确"}},
                                status_code=401, headers={"Cache-Control": "no-store"})
        if request.url.path.startswith("/api/auth/"):
            # FastAPI normally echoes invalid input, including password values.
            detail = [{"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
                      for error in exc.errors()]
            return JSONResponse({"detail": detail}, status_code=422, headers={"Cache-Control": "no-store"})
        return await request_validation_exception_handler(request, exc)

    app.include_router(router)


def bootstrap_password(user_id: str, password: str, config=None) -> bool:
    """Call from a trusted server shell; never expose this operation through HTTP."""
    from backend.config import BackendConfig
    settings = config or BackendConfig.from_env()
    users = UserDirectory(settings.users_file, settings.supported_ids_file, settings.admin_user_id,
                          settings.default_admin_model, settings.default_user_model)
    auth = AuthenticationStore(settings.auth_database_file or settings.users_file.with_name("authentication.sqlite3"),
                               users, session_ttl_seconds=settings.auth_session_ttl_seconds)
    return auth.bootstrap(user_id, password)
