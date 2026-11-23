"""Persistent accounts and revocable browser sessions at the public API boundary."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from backend.identity import _origin, _trusted_proxy, request_scheme, verify_write_origin
from backend.users import UserDirectory, UserRecord

COOKIE = "igraph_session"
SESSION_SECONDS = 14 * 24 * 3600
ITERATIONS = 600_000


def problem(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def check_password(password):
    if not 10 <= len(password) <= 128:
        raise problem(422, "AUTH_PASSWORD_INVALID", "密码需要 10–128 个字符")


class AccountDirectory(UserDirectory):
    """SQLite is independent of the legacy registry; existing ownership IDs stay intact."""
    def __init__(self, settings):
        super().__init__(settings.users_file, settings.supported_ids_file, settings.admin_user_id,
                         settings.default_admin_model, settings.default_user_model)
        self.owner_id = settings.admin_user_id
        self.db_path = settings.auth_database or settings.users_file.with_name("auth.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.db_path, 0o600)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    user_id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES accounts(user_id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id);
                CREATE TABLE IF NOT EXISTS auth_limits (
                    key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at REAL NOT NULL);
            """)
        # Use an equally expensive verification for unknown accounts.
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _record(self, row):
        if row is None:
            return None
        return UserRecord(user_id=row["user_id"], username=row["username"], enabled=bool(row["enabled"]),
                          roles=["admin", "user"] if row["user_id"] == self.owner_id else ["user"],
                          allowed_models=self._default_user_models() if row["user_id"] == self.owner_id else [], created_at=row["created_at"],
                          updated_at=row["updated_at"])

    def get(self, user_id):
        with self.connect() as db:
            return self._record(db.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchone())

    def list(self):
        with self.connect() as db:
            return [self._record(row) for row in db.execute("SELECT * FROM accounts")]

    def register(self, username, password, *, owner=False):
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}", username):
            raise problem(422, "AUTH_USERNAME_INVALID", "用户名需要 3–32 位字母、数字、点、下划线或短横线")
        if not owner and username.casefold() in {"admin", "administrator", "root", self.owner_id.casefold()}:
            raise problem(409, "AUTH_USERNAME_UNAVAILABLE", "此用户名不可用")
        check_password(password)
        encoded = password_hash(password)
        uid, now = (self.owner_id if owner else "u_" + uuid.uuid4().hex), time.time()
        try:
            with self.connect() as db:
                db.execute("INSERT INTO accounts VALUES (?, ?, ?, 1, ?, ?)", (uid, username, encoded, now, now))
        except sqlite3.IntegrityError:
            raise problem(409, "AUTH_USERNAME_UNAVAILABLE", "此用户名已被使用") from None
        return self.get(uid)

    def authenticate(self, username, password):
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
        valid = password_matches(password, row["password_hash"] if row else self.dummy_hash)
        if not valid or row is None or not row["enabled"]:
            raise problem(401, "AUTH_INVALID_CREDENTIALS", "用户名或密码错误")
        return self._record(row)

    def issue(self, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
            db.execute("INSERT INTO auth_sessions VALUES (?, ?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, time.time() + SESSION_SECONDS))
        return token, csrf

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute("""SELECT s.* FROM auth_sessions s JOIN accounts a USING(user_id)
                WHERE token_hash=? AND expires_at>? AND a.enabled=1""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return dict(row) if row else None

    def revoke(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, user, old_password, new_password):
        self.authenticate(user.username, old_password)
        check_password(new_password)
        encoded = password_hash(new_password)
        with self.connect() as db:
            db.execute("UPDATE accounts SET password_hash=?, updated_at=? WHERE user_id=?", (encoded, time.time(), user.user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user.user_id,))

    def rate_limit(self, key, maximum, seconds):
        key = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        blocked = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_limits WHERE expires_at<=?", (now,))
            row = db.execute("SELECT * FROM auth_limits WHERE key=?", (key,)).fetchone()
            if row and row["attempts"] >= maximum:
                blocked = True
            elif row:
                db.execute("UPDATE auth_limits SET attempts=attempts+1 WHERE key=?", (key,))
            else:
                db.execute("INSERT INTO auth_limits VALUES (?, 1, ?)", (key, now + seconds))
        if blocked:
            raise HTTPException(429, {"code": "AUTH_RATE_LIMITED", "message": "尝试次数过多，请稍后再试"}, headers={"Retry-After": str(seconds)})


class AccountMiddleware:
    def __init__(self, app, accounts, settings):
        if any(_origin(value) is None for value in settings.auth_allowed_origins):
            raise ValueError("BACKEND_AUTH_ALLOWED_ORIGINS 必须为明确的 HTTP(S) 来源")
        self.app, self.accounts, self.settings = app, accounts, settings

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api" or path.startswith("/api/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            verify_write_origin(request, trusted_proxies=self.settings.auth_trusted_proxies,
                                allowed_origins=self.settings.auth_allowed_origins)
            public = path in {"/api/auth/register", "/api/auth/login"}
            if public:
                if request.method != "POST" or request.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise problem(415, "AUTH_JSON_REQUIRED", "请使用 JSON 登录请求")
            else:
                session = self.accounts.session(request.cookies.get(COOKIE, ""))
                if not session:
                    raise problem(401, "AUTH_REQUIRED", "请先登录")
                uid = session["user_id"]
                expected = request.headers.get("x-workspace-user")
                if expected and expected != uid:
                    raise problem(409, "AUTH_ACCOUNT_CHANGED", "登录账户已变化，请重新加载页面")
                if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf_token"]):
                    raise problem(403, "AUTH_CSRF_INVALID", "登录验证已失效，请刷新页面")
                scope.setdefault("state", {}).update(authenticated_user_id=uid, auth_session=session)
                scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-user-id"] + [(b"x-user-id", uid.encode())]
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                headers={"Cache-Control": "no-store", **(exc.headers or {})})(scope, receive, send)
        await self.app(scope, receive, send)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


def public_user(user):
    return {"user_id": user.user_id, "username": user.username}


def install_auth_routes(app, accounts, settings):
    def response_for(user, request):
        accounts.revoke(request.cookies.get(COOKIE, ""))
        token, csrf = accounts.issue(user.user_id)
        response = JSONResponse({"user": public_user(user), "csrf_token": csrf}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                            secure=request_scheme(request, settings.auth_trusted_proxies) == "https", path="/")
        return response

    def client_ip(request):
        if _trusted_proxy(request, settings.auth_trusted_proxies):
            return request.headers.get("x-real-ip") or request.client.host
        return request.client.host if request.client else "unknown"

    @app.post("/api/auth/register")
    def register(body: Credentials, request: Request):
        accounts.rate_limit("register:" + client_ip(request), 20, 3600)
        user = accounts.register(body.username, body.password)
        return response_for(user, request)

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request):
        accounts.rate_limit("login-ip:" + client_ip(request), 120, 600)
        accounts.rate_limit("login-user:" + body.username.strip().casefold(), 10, 600)
        return response_for(accounts.authenticate(body.username, body.password), request)

    @app.get("/api/auth/session")
    def session(request: Request):
        user = accounts.get(request.state.authenticated_user_id)
        return JSONResponse({"user": public_user(user), "csrf_token": request.state.auth_session["csrf_token"]},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/logout")
    def logout(request: Request):
        accounts.revoke(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.post("/api/auth/password")
    def change_password(body: PasswordChange, request: Request):
        uid = request.state.authenticated_user_id
        accounts.rate_limit("password:" + uid, 10, 600)
        user = accounts.get(uid)
        accounts.change_password(user, body.current_password, body.new_password)
        return response_for(user, request)


if __name__ == "__main__":
    import argparse
    from backend.config import BackendConfig
    parser = argparse.ArgumentParser(description="Create the legacy workspace owner's login; never claims it via public signup.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args()
    store = AccountDirectory(BackendConfig.from_env())
    store.register(args.username, args.password_file.read_text().strip(), owner=True)
    print("Workspace owner login created.")
