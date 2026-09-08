"""Cross-service control wire contract (no Agent or Graph runtime imports)."""
from __future__ import annotations
import hmac
import os
from contextvars import ContextVar
from uuid import UUID

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from fastapi import Request
from pydantic import BaseModel, model_validator

execution_headers: ContextVar[dict[str, str]] = ContextVar("igraph_execution_headers", default={})


def validate_service_configuration() -> None:
    """Fail before serving traffic when internal service authentication is unusable.

    The credential is required independently of the optional run-cancellation
    feature. Never include its value in errors or silently normalize it.
    """
    secret = os.getenv("IGRAPH_CONTROL_SERVICE_KEY", "")
    if not secret or not secret.isascii() or any(not 33 <= ord(char) <= 126 for char in secret):
        raise RuntimeError(
            "IGRAPH_CONTROL_SERVICE_KEY is required and must contain printable ASCII "
            "characters without whitespace. Configure the same random secret for "
            "Backend, Agent Runtime, Graph Engine API and MCP, even when "
            "IGRAPH_RUN_CANCEL_ENABLED=false."
        )


def controls_enabled() -> bool:
    return os.getenv("IGRAPH_RUN_CANCEL_ENABLED", "false").lower() in {"true", "1", "yes"} and bool(os.getenv("IGRAPH_CONTROL_SERVICE_KEY", ""))


def service_headers(user_id: str) -> dict[str, str]:
    return {"X-User-ID": user_id, "X-iGraph-Service-Key": os.getenv("IGRAPH_CONTROL_SERVICE_KEY", "")}


def require_service(request: Request):
    from fastapi import HTTPException
    secret = os.getenv("IGRAPH_CONTROL_SERVICE_KEY", "")
    if not secret or not hmac.compare_digest(request.headers.get("X-iGraph-Service-Key", ""), secret):
        raise HTTPException(403, {"code": "CONTROL_SERVICE_FORBIDDEN"})


class Target(BaseModel):
    run_id: UUID | None = None
    start_request_id: UUID | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.run_id is None) == (self.start_request_id is None):
            raise ValueError("exactly one run_id or start_request_id is required")
        return self

    def lookup(self):
        return self.model_dump(mode="json", exclude_none=True)


class CancelRequest(BaseModel):
    cancel_request_id: UUID
    target: Target


class JobCancelRequest(BaseModel):
    cancel_request_id: UUID
