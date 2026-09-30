from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.authentication import (AUTH_COOKIE, AuthenticationStore, request_scheme,
                                    verify_password, verify_write_origin)
from backend.config import BackendConfig
from test_projects import FakeServiceClient

PASSWORD = "Test-Password-2026!"


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    seed = tmp_path / "supported.txt"
    seed.write_text("admin\nalice\n", encoding="utf-8")
    monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "authentication-test-service-key")
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setenv("BACKEND_SERVER_DATASETS_DIR", str(tmp_path / "server_datasets"))
    monkeypatch.setattr(backend_app, "ServiceClient", FakeServiceClient)
    settings = BackendConfig(
        host="127.0.0.1", port=0, agent_runtime_url="http://fake-runtime", graph_engine_url="http://fake-graph",
        agent_id="igraph-agent", request_timeout=1, stream_timeout=1,
        users_file=tmp_path / "backend" / "users.json", projects_file=tmp_path / "backend" / "projects.json",
        supported_ids_file=seed, admin_user_id="admin", default_admin_model="qwen3.6-plus",
        default_user_model="qwen3.6-plus", custom_models_file=tmp_path / "custom_models.json",
    )
    app = backend_app.create_app(settings)
    assert app.state.auth.bootstrap("admin", PASSWORD)
    assert app.state.auth.bootstrap("alice", PASSWORD)
    with TestClient(app) as client:
        yield client, app, settings


def login(client, username="admin", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_registration_login_and_hashed_storage(auth_client):
    client, app, settings = auth_client
    response = client.post("/api/auth/register", json={"username": "Novel_User", "password": PASSWORD})
    assert response.status_code == 201
    assert response.json()["user_id"] == "novel_user"
    assert response.json()["roles"] == ["user"] and not response.json()["is_admin"]
    assert response.json()["allowed_models"]
    assert client.get("/api/auth/me").json()["user_id"] == "novel_user"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie
    token = client.cookies.get(AUTH_COOKIE)
    with app.state.auth._connection() as connection:
        credential = connection.execute("SELECT * FROM credentials WHERE username='novel_user'").fetchone()
        session = connection.execute("SELECT * FROM sessions WHERE user_id='novel_user'").fetchone()
    assert verify_password(PASSWORD, credential["password_hash"])
    assert credential["password_hash"] != PASSWORD
    assert session["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token.encode() not in app.state.auth.path.read_bytes()
    assert PASSWORD not in settings.users_file.read_text(encoding="utf-8")
    assert "password" not in response.text
    if os.name == "posix":
        assert app.state.auth.path.stat().st_mode & 0o077 == 0
    assert client.post("/api/auth/logout").status_code == 200
    assert login(client, "NOVEL_USER").status_code == 200


def test_six_character_password_can_register_and_log_in(auth_client):
    client, _, _ = auth_client
    response = client.post("/api/auth/register", json={"username": "newuser", "password": "abc123"})
    assert response.status_code == 201
    assert client.post("/api/auth/logout").status_code == 200
    assert login(client, "newuser", "abc123").status_code == 200


@pytest.mark.parametrize("username,password", [
    ("admin", "short"),
    ("missing", PASSWORD),
    ("invalid username", PASSWORD),
    ("admin", "x" * 129),
])
def test_login_failures_do_not_reveal_password_rules(auth_client, username, password):
    client, _, _ = auth_client
    response = login(client, username, password)
    assert response.status_code == 401
    assert response.json()["detail"] == {
        "code": "AUTH_INVALID_CREDENTIALS", "message": "用户名或密码不正确"
    }
    assert "6–128" not in response.text


def test_existing_accounts_are_reserved_and_bootstrap_never_overwrites(auth_client):
    client, app, _ = auth_client
    for name in ("admin", "ADMIN", "alice"):
        response = client.post("/api/auth/register", json={"username": name, "password": PASSWORD})
        assert response.status_code == 409
    assert not app.state.auth.bootstrap("admin", "A-Different-Password!")
    assert login(client, password="A-Different-Password!").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/auth/me").json()["is_admin"]


@pytest.mark.parametrize("username", ["../admin", "a/b", "a\\b", "xy", "a" * 33, "人物姓名", "name:admin", "a b c"])
def test_invalid_registration_username_is_rejected(auth_client, username):
    client, _, settings = auth_client
    response = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 400
    assert not settings.users_file.exists()


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_invalid_password_is_rejected_without_echoing_it(auth_client, password):
    client, _, _ = auth_client
    response = client.post("/api/auth/register", json={"username": "newuser", "password": password})
    assert response.status_code in {400, 422}
    assert password not in response.text


def test_validation_errors_never_echo_secret_input(auth_client):
    client, _, _ = auth_client
    response = client.post("/api/auth/login", json={"username": "admin", "password": [PASSWORD]})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert PASSWORD not in response.text


def test_header_forgery_is_rejected_and_cookie_alone_decides_owner(auth_client):
    client, _, _ = auth_client
    assert client.get("/api/projects", headers={"X-User-ID": "admin"}).status_code == 401
    assert client.post("/api/auth/validate", headers={"X-User-ID": "admin"}).status_code == 401
    assert client.get("/api/capabilities").status_code == 401
    assert client.get("/health").status_code == 200
    assert login(client, "alice").status_code == 200
    assert client.get("/api/auth/me", headers={"X-User-ID": "admin"}).json()["user_id"] == "alice"
    assert client.get("/api/admin/users", headers={"X-User-ID": "admin"}).status_code == 403
    validation = client.post("/api/auth/validate", headers={"X-User-ID": "admin"})
    assert validation.status_code == 200 and validation.json()["user_id"] == "alice"


def test_stale_workspace_account_is_rejected_and_private_results_are_not_cached(auth_client):
    client, _, _ = auth_client
    assert login(client, "alice").status_code == 200
    response = client.post("/api/projects", headers={"X-iGraph-Account": "admin"}, json={"name": "Stale page"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ACCOUNT_CHANGED"
    assert client.get("/api/projects").json()["projects"] == []
    response = client.get("/api/auth/me", headers={"X-iGraph-Account": "alice"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "Cookie" in response.headers["vary"]


def test_user_projects_and_datasets_are_isolated_across_logins(auth_client):
    client, _, _ = auth_client
    assert login(client).status_code == 200
    project = client.post("/api/projects", json={"name": "Private novel"}).json()["project"]
    dataset = client.post("/api/personal-datasets", files={"file": ("novel.txt", b"Alice knows Bob.", "text/plain")}).json()["dataset"]
    assert login(client, "alice").status_code == 200
    assert client.get("/api/projects").json()["projects"] == []
    assert client.get("/api/personal-datasets").json()["datasets"] == []
    assert client.get(f"/api/projects/{project['project_id']}/conversations").status_code == 404
    assert client.get(f"/api/personal-datasets/{dataset['dataset_id']}/construction").status_code == 404
    assert login(client).status_code == 200
    assert client.get("/api/projects").json()["projects"][0]["project_id"] == project["project_id"]
    assert client.get("/api/personal-datasets").json()["datasets"][0]["dataset_id"] == dataset["dataset_id"]


def test_persisted_sessions_logout_and_disabled_user(auth_client):
    client, app, _ = auth_client
    assert login(client).status_code == 200
    token = client.cookies.get(AUTH_COOKIE)
    reopened = AuthenticationStore(app.state.auth.path, app.state.users)
    assert reopened.authenticate(token).user_id == "admin"
    assert client.post("/api/auth/logout").status_code == 200
    assert reopened.authenticate(token) is None
    assert client.get("/api/auth/me", headers={"Cookie": f"{AUTH_COOKIE}={token}"}).status_code == 401
    assert login(client, "alice").status_code == 200
    alice = app.state.users.get("alice")
    alice.enabled = False
    app.state.users.save(alice)
    assert client.get("/api/auth/me").status_code == 401
    assert login(client, "alice").status_code == 401


def test_expired_sessions_are_not_accepted(auth_client):
    client, app, _ = auth_client
    assert login(client).status_code == 200
    with app.state.auth._connection(write=True) as connection:
        connection.execute("UPDATE sessions SET expires_at=0")
    assert client.get("/api/auth/me").status_code == 401


def test_write_origin_and_https_cookie(auth_client):
    client, app, _ = auth_client
    assert client.post("/api/auth/login", headers={"Origin": "https://evil.example"},
                       json={"username": "admin", "password": PASSWORD}).status_code == 403
    assert client.post("/api/auth/login", headers={"Origin": "http://testserver"},
                       json={"username": "admin", "password": PASSWORD}).status_code == 200
    with TestClient(app, base_url="https://testserver") as secure:
        response = login(secure)
        assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
        assert secure.get("/api/auth/me").status_code == 200


def test_allowed_origin_receives_auth_errors_and_preflight_without_a_session(auth_client):
    _, _, settings = auth_client
    origin = "https://frontend.example"
    app = backend_app.create_app(replace(settings, auth_allowed_origins=(origin,)))
    with TestClient(app) as client:
        response = client.get("/api/auth/me", headers={"Origin": origin})
        assert response.status_code == 401
        assert response.headers["access-control-allow-origin"] == origin
        assert response.headers["access-control-allow-credentials"] == "true"
        preflight = client.options("/api/auth/login", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-igraph-account",
        })
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert preflight.headers["access-control-allow-credentials"] == "true"
        response = client.post("/api/auth/login", headers={"Origin": origin},
                               json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_forwarded_host_cannot_authorize_an_attacker_origin():
    request = Request({"type": "http", "method": "POST", "scheme": "http", "path": "/api/auth/login",
                       "query_string": b"", "server": ("127.0.0.1", 21109), "client": ("127.0.0.1", 12345),
                       "headers": [(b"host", b"demo.ngrok.app"), (b"x-forwarded-proto", b"https"),
                                   (b"x-forwarded-host", b"evil.example"), (b"origin", b"https://evil.example")]})
    assert request_scheme(request, ("127.0.0.1",)) == "https"
    with pytest.raises(HTTPException) as caught:
        verify_write_origin(request, trusted_proxies=("127.0.0.1",), allowed_origins=())
    assert caught.value.status_code == 403
    request.scope["headers"] = [(b"host", b"demo.ngrok.app"), (b"x-forwarded-proto", b"https"),
                                (b"origin", b"https://demo.ngrok.app")]
    verify_write_origin(Request(request.scope), trusted_proxies=("127.0.0.1",), allowed_origins=())


def test_rate_limits_are_shared_by_store_instances(auth_client):
    _, app, _ = auth_client
    other = AuthenticationStore(app.state.auth.path, app.state.users)
    app.state.auth.consume_limit("probe", attempts=2, window_seconds=60)
    other.consume_limit("probe", attempts=2, window_seconds=60)
    with pytest.raises(HTTPException) as caught:
        other.consume_limit("probe", attempts=2, window_seconds=60)
    assert caught.value.status_code == 429 and int(caught.value.headers["Retry-After"]) > 0


def test_simultaneous_registration_reserves_a_username_once(auth_client):
    _, app, _ = auth_client
    other = AuthenticationStore(app.state.auth.path, app.state.users)
    def create(store):
        try:
            return store.register("sameuser", PASSWORD).user_id
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, [app.state.auth, other]))
    assert sorted(results, key=str) == [409, "sameuser"]
    assert sum(record.user_id == "sameuser" for record in app.state.users.list()) == 1
