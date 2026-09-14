"""Resolve construction access without trusting tool arguments or cached catalogs."""
from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from construction_access import (AccessGrant, AccessRequest,
    CONSTRUCTION_NOT_APPLICABLE, CONSTRUCTION_NOT_APPLICABLE_MESSAGE)
from execution_protocol import service_headers


AUTHORIZATION_TIMEOUT_SECONDS = 5.0


class ConstructionAccessClient:
    def __init__(self, base_url: str | None = None, *, transport=None):
        self.base_url = (base_url or os.getenv("BACKEND_CONTROL_URL", "http://backend:21109")).rstrip("/")
        self.transport = transport

    async def authorize(self, user_id: str, *, session_id: str | None = None,
                        scope: str | None = None, dataset_id: str | None = None,
                        cleanup: bool = False) -> AccessGrant:
        if not user_id or (session_id is None and scope is None):
            raise HTTPException(403, {"code": "DATASET_SCOPE_REQUIRED"})
        try:
            body = AccessRequest(session_id=session_id, scope=scope, dataset_id=dataset_id, cleanup=cleanup)
        except ValidationError:
            raise HTTPException(422, {"code": "INVALID_DATASET_SCOPE"}) from None
        try:
            # Bound both network waits and pool waits. No stale grant cache or
            # retries: an unavailable authority never expands the read scope.
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(base_url=self.base_url, timeout=AUTHORIZATION_TIMEOUT_SECONDS,
                                             trust_env=False, transport=self.transport) as client:
                    response = await client.post("/internal/construction/access", headers=service_headers(user_id),
                                                 json=body.model_dump())
            if response.status_code in {401, 403, 404, 409, 422}:
                # Only this typed applicability result is public, after the
                # authority has verified live ownership. Never relay its body.
                if response.status_code == 422:
                    payload = response.json()
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    if isinstance(detail, dict) and detail.get("code") == CONSTRUCTION_NOT_APPLICABLE:
                        raise HTTPException(422, {"code": CONSTRUCTION_NOT_APPLICABLE,
                            "message": CONSTRUCTION_NOT_APPLICABLE_MESSAGE})
                # Do not relay upstream bodies (paths, session state, diagnostics).
                code = "PROJECT_DELETING" if response.status_code == 409 else "DATASET_SCOPE_REQUIRED"
                raise HTTPException(response.status_code, {"code": code})
            response.raise_for_status()
            grant = AccessGrant.model_validate(response.json())
            if (grant.user_id != user_id or (scope is not None and grant.scope != scope)
                    or (dataset_id is not None and (grant.dataset_ids != [dataset_id]
                        or grant.source is None or grant.source.dataset_id != dataset_id))):
                raise ValueError("Mismatched access grant")
            return grant
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise HTTPException(503, {"code": "DATASET_AUTHORIZATION_UNAVAILABLE",
                                     "message": "数据集权限服务暂不可用，请稍后重试。"}) from None

    async def authorize_tool(self, user_id: str, tool_name: str, *, dataset_id: str,
                             session_id: str | None = None, scope: str | None = None) -> dict | None:
        """Status may report non-applicability; all other operations require a grant."""
        try:
            await self.authorize(user_id, session_id=session_id, scope=scope, dataset_id=dataset_id)
        except HTTPException as exc:
            if (tool_name != "get_graph_construction_status" or exc.status_code != 422
                    or not isinstance(exc.detail, dict)
                    or exc.detail.get("code") != CONSTRUCTION_NOT_APPLICABLE):
                raise
            return {"success": True, "dataset_id": dataset_id, "status": "not_applicable",
                    "construction_applicable": False, "code": CONSTRUCTION_NOT_APPLICABLE,
                    "message": CONSTRUCTION_NOT_APPLICABLE_MESSAGE}
        return None
