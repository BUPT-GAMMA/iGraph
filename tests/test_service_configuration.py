"""Deployment failures are explicit; no production credentials are needed."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from execution_protocol import (controls_enabled, require_service, service_headers,
                                validate_service_configuration)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [None, "", "   ", "secret with-space", "secret\n", "凭据"])
def test_invalid_service_configuration_fails_without_echoing_secret(monkeypatch, value):
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    if value is None:
        monkeypatch.delenv("IGRAPH_CONTROL_SERVICE_KEY", raising=False)
    else:
        monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", value)
    with pytest.raises(RuntimeError, match="IGRAPH_CONTROL_SERVICE_KEY") as error:
        validate_service_configuration()
    if value and value.strip():
        assert value not in str(error.value)


def test_valid_service_key_keeps_cancellation_optional_and_authentication_required(monkeypatch):
    monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "test-shared-secret")
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    validate_service_configuration()
    assert controls_enabled() is False
    headers = service_headers("alice")
    request = Request({"type": "http", "headers": [(key.lower().encode(), value.encode())
                                                   for key, value in headers.items()]})
    require_service(request)
    for value in (b"", b"different-secret"):
        request = Request({"type": "http", "headers": [(b"x-igraph-service-key", value)]})
        with pytest.raises(HTTPException) as error:
            require_service(request)
        assert error.value.status_code == 403


@pytest.mark.parametrize("module_args", [
    ["backend"], ["agent_runtime"], ["graph_engine", "api"],
    ["graph_engine", "mcp", "http"], ["mcp_service", "http"], ["graph_data_service"],
])
def test_service_entrypoints_reject_missing_key_before_connecting(module_args, tmp_path):
    environment = {**os.environ, "IGRAPH_CONTROL_SERVICE_KEY": "", "IGRAPH_RUN_CANCEL_ENABLED": "false",
                   "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "IGRAPH_DATA_ROOT": str(tmp_path)}
    result = subprocess.run([sys.executable, "-m", *module_args], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "IGRAPH_CONTROL_SERVICE_KEY is required" in result.stderr
    assert "Application startup complete" not in result.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("service", ["backend", "graph", "mcp"])
def test_asgi_and_embedded_mcp_startup_validate_key(service, monkeypatch, tmp_path):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    if service == "backend":
        from backend.app import create_app
        app = create_app()
        context = app.router.lifespan_context(app)
    elif service == "graph":
        from graph_data_service.app import create_app
        app = create_app(SimpleNamespace())
        context = app.router.lifespan_context(app)
    else:
        monkeypatch.setenv("MCP_HOST", "127.0.0.1")
        monkeypatch.setenv("MCP_PORT", "21110")
        monkeypatch.setenv("GRAPH_DATA_SERVER_URL", "http://127.0.0.1:21111")
        from mcp_service.server import service_lifespan
        context = service_lifespan(None)
    # This also covers uvicorn module:app, which bypasses CLI entrypoints.
    monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "")

    async def start():
        async with context:
            pytest.fail("Service accepted requests without its credential")

    with pytest.raises(RuntimeError, match="IGRAPH_CONTROL_SERVICE_KEY"):
        asyncio.run(start())


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is not installed")
def test_compose_requires_key_and_accepts_explicit_test_credential(tmp_path):
    shutil.copyfile(ROOT / "compose.yaml", tmp_path / "compose.yaml")
    shutil.copyfile(ROOT / ".env.example", tmp_path / ".env")
    command = ["docker", "compose", "--project-directory", str(tmp_path), "--env-file", str(tmp_path / ".env"),
               "-f", str(tmp_path / "compose.yaml"), "config", "-q"]
    environment = {**os.environ, "IGRAPH_CONTROL_SERVICE_KEY": ""}
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "IGRAPH_CONTROL_SERVICE_KEY" in result.stderr
    environment["IGRAPH_CONTROL_SERVICE_KEY"] = "compose-test-only"
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
