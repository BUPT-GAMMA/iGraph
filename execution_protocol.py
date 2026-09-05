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
