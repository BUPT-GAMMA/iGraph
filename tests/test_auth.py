"""Exercise the public cookie boundary and durable account ownership."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.auth import AccountDirectory, COOKIE, password_matches
from backend.config import BackendConfig
from tests.test_projects import client  # resets the service fake and installs it

PASSWORD = "test-password-1234"


@pytest.fixture
def auth_config(client, tmp_path):
    return BackendConfig(host="127.0.0.1", port=0, agent_runtime_url="http://fake-runtime",
        graph_engine_url="http://fake-graph", agent_id="igraph-agent", request_timeout=1,
        stream_timeout=1, users_file=tmp_path / "accounts" / "users.json",
        projects_file=tmp_path / "private-projects.json", supported_ids_file=tmp_path / "supported.txt",
        admin_user_id="admin", default_admin_model="qwen3.6-plus", default_user_model="qwen3.6-plus")


@pytest.fixture
def browser(auth_config):
    with TestClient(backend_app.create_app(auth_config)) as browser:
        yield browser


def signup(browser, username="alice"):
    result = browser.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert result.status_code == 200, result.text
    session = result.json()
    browser.headers.update({"X-CSRF-Token": session["csrf_token"], "X-Workspace-User": session["user"]["user_id"]})
    return session


def test_anonymous_and_spoofed_headers_never_grant_owner(browser):
    for path in ["/api/workspace", "/api/projects", "/api/sessions", "/api/models", "/api/auth/session"]:
        assert browser.get(path, headers={"X-User-ID": "admin"}).status_code == 401
    for name in ["admin", "ADMIN", "root"]:
        assert browser.post("/api/auth/register", json={"username": name, "password": PASSWORD}).status_code == 409
    session = signup(browser)
    result = browser.get("/api/workspace", headers={"X-User-ID": "admin"})
    assert result.status_code == 200
    assert session["user"]["user_id"] != "admin"
    assert result.json()["user_id"] == session["user"]["user_id"]


def test_registration_validation_and_case_insensitive_uniqueness(browser):
    for name, password in [("../escape", PASSWORD), ("ab", PASSWORD), ("valid", "short")]:
        assert browser.post("/api/auth/register", json={"username": name, "password": password}).status_code == 422
    signup(browser)
    assert browser.post("/api/auth/register", json={"username": "ALICE", "password": PASSWORD}).status_code == 409
    assert browser.post("/api/auth/login", json={"username": "alice", "password": "incorrect"}).status_code == 401
    assert browser.post("/api/auth/login", json={"username": "ALICE", "password": PASSWORD}).status_code == 200


def test_csrf_cross_origin_and_stale_tabs(browser):
    signup(browser)
    assert browser.post("/api/projects", json={"name": "private"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert browser.post("/api/projects", json={"name": "private"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/api/projects", headers={"X-Workspace-User": "another-user"}).status_code == 409
    assert browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.post("/api/auth/login", data="username=alice").status_code == 415


def test_projects_are_private_and_survive_new_app_instance(browser, auth_config):
    alice = signup(browser)
    created = browser.post("/api/projects", json={"name": "Alice private project"})
    assert created.status_code == 200, created.text
    pid = created.json()["project"]["project_id"]
    token = browser.cookies.get(COOKIE)
    with TestClient(backend_app.create_app(auth_config)) as bob:
        signup(bob, "bob")
        assert all(p["project_id"] != pid for p in bob.get("/api/projects").json()["projects"])
        assert bob.get(f"/api/projects/{pid}/datasets").status_code == 404
        assert bob.request("DELETE", f"/api/projects/{pid}", json={"conversation_action": "delete"}).status_code == 404
    with TestClient(backend_app.create_app(auth_config)) as restarted:
        restarted.cookies.set(COOKIE, token)
        assert restarted.get("/api/auth/session").json()["user"] == alice["user"]
        assert any(p["project_id"] == pid for p in restarted.get("/api/projects").json()["projects"])


def test_logout_revokes_cookie_and_password_change_revokes_other_sessions(browser, auth_config):
    signup(browser)
    old = browser.cookies.get(COOKIE)
    assert browser.post("/api/auth/logout").status_code == 200
    assert browser.get("/api/auth/session").status_code == 401
    browser.cookies.set(COOKIE, old)
    assert browser.get("/api/auth/session").status_code == 401
    browser.cookies.clear()
    signed = browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).json()
    browser.headers["X-CSRF-Token"] = signed["csrf_token"]
    with TestClient(backend_app.create_app(auth_config)) as other:
        assert other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
        changed = browser.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD + "-new"})
        assert changed.status_code == 200, changed.text
        assert other.get("/api/auth/session").status_code == 401
    assert browser.get("/api/auth/session").status_code == 200
    assert browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401
    assert browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD + "-new"}).status_code == 200


def test_password_and_token_storage_expiry_and_disabled_accounts(browser):
    session = signup(browser)
    accounts = browser.app.state.users
    raw_token = browser.cookies.get(COOKIE)
    with accounts.connect() as db:
        row = dict(db.execute("SELECT * FROM accounts").fetchone())
        assert row["password_hash"] != PASSWORD and password_matches(PASSWORD, row["password_hash"])
        assert db.execute("SELECT token_hash FROM auth_sessions").fetchone()[0] != raw_token
        db.execute("UPDATE auth_sessions SET expires_at=0")
    assert browser.get("/api/auth/session").status_code == 401
    browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    with accounts.connect() as db:
        db.execute("UPDATE accounts SET enabled=0 WHERE user_id=?", (session["user"]["user_id"],))
    assert browser.get("/api/auth/session").status_code == 401


def test_owner_bootstrap_preserves_legacy_identity_and_credentials(auth_config):
    accounts = AccountDirectory(auth_config)
    owner = accounts.register("workspace-owner", PASSWORD, owner=True)
    assert owner.user_id == "admin"
    assert AccountDirectory(auth_config).authenticate("workspace-owner", PASSWORD).user_id == "admin"


def test_concurrent_registration_and_rate_limit_are_atomic(auth_config):
    accounts = AccountDirectory(auth_config)
    def register(_):
        try:
            return accounts.register("same-name", PASSWORD).user_id
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))
    assert sum(isinstance(value, str) for value in results) == 1
    assert 409 in results
    accounts.rate_limit("same-client", 1, 600)
    with pytest.raises(HTTPException) as caught:
        AccountDirectory(auth_config).rate_limit("same-client", 1, 600)
    assert caught.value.status_code == 429


def test_cookie_is_http_only_and_https_secure(browser, auth_config):
    response = browser.post("/api/auth/register", json={"username": "alice", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=" in cookie
    assert response.headers["cache-control"] == "no-store"
    with TestClient(backend_app.create_app(auth_config), base_url="https://testserver") as secure:
        response = secure.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert "secure" in response.headers["set-cookie"].lower()
        assert secure.get("/api/auth/session").status_code == 200
