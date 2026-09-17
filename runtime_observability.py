"""Structured request correlation shared by Backend and Agent Runtime."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_trace_id = contextvars.ContextVar("igraph_trace_id", default="")
_SECRET_KEYS = {"authorization", "cookie", "set_cookie", "api_key", "token", "password", "secret"}

def configure_logging():
    logger = logging.getLogger("igraph.runtime")
    logger.setLevel(getattr(logging, os.getenv("IGRAPH_LOG_LEVEL", "INFO").upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        directory = os.getenv("IGRAPH_RUNTIME_LOG_DIR", "").strip()
        if directory:
            Path(directory).mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(Path(directory) / "runtime.jsonl", maxBytes=20_000_000,
                                               backupCount=5, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(file_handler)
    return logger

def set_trace_id(value):
    _trace_id.set(str(value or ""))

def redact(value, key=""):
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_KEYS or normalized.endswith(("_api_key", "_token", "_password", "_secret")):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value

def log_event(logger, event, **fields):
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "event_id": uuid.uuid4().hex, "event": event,
              "trace_id": fields.pop("trace_id", None) or _trace_id.get() or None, **fields}
    logger.info(json.dumps(redact(record), ensure_ascii=False, default=str))

class RequestLoggingMiddleware:
    """Pure ASGI middleware: leaves streaming, cancellation and task contexts intact."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        rid = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied) else uuid.uuid4().hex
        token = _trace_id.set(rid)
        started = time.perf_counter()
        logger = configure_logging()
        status = 500
        fields = {"request_id": rid, "method": scope["method"], "path": scope["path"]}
        log_event(logger, "request.started", **fields)
        async def correlated_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [(key, value) for key, value in message.get("headers", [])
                                                  if key.lower() != b"x-request-id"] + [(b"x-request-id", rid.encode())]}
            await send(message)
        try:
            await self.app(scope, receive, correlated_send)
        finally:
            log_event(logger, "request.complete", **fields, status=status,
                      duration_ms=round((time.perf_counter() - started) * 1000, 2))
            _trace_id.reset(token)
