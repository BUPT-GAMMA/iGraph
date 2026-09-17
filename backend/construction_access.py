"""Authoritative project/personal ownership checks for construction consumers."""
from __future__ import annotations

import asyncio

from anyio import to_thread
from fastapi import HTTPException, Request

from construction_access import (AccessGrant, AccessRequest, SourceRecord, scope_headers, SCOPE_HEADER,
    CONSTRUCTION_NOT_APPLICABLE, CONSTRUCTION_NOT_APPLICABLE_MESSAGE)
from execution_protocol import require_service


def install_construction_access(app, *, directory, projects, session_for, state_for):
    @app.post("/internal/construction/access", response_model=AccessGrant)
    async def authorize(body: AccessRequest, request: Request):
        require_service(request)
        user_id = request.headers.get("X-User-ID", "").strip()
        if not user_id:
            raise HTTPException(401, {"code": "user_required"})
        scope = body.scope
        if body.session_id:
            try:
                # Finish before the Engine's five-second authorization deadline.
                async with asyncio.timeout(4):
                    session = await session_for(body.session_id, request, user_id)
            except TimeoutError:
                raise HTTPException(503, {"code": "DATASET_AUTHORIZATION_UNAVAILABLE"}) from None
            # The native persisted session, including its owner, is authoritative.
            if session.get("user_id") != user_id or session.get("session_id") != body.session_id:
                raise HTTPException(404, {"code": "DATASET_SCOPE_REQUIRED"})
            project_id = state_for(session).get("project_id") or None
            session_scope = scope_headers(project_id)[SCOPE_HEADER]
            if scope is not None and scope != session_scope:
                raise HTTPException(403, {"code": "DATASET_SCOPE_REQUIRED"})
            scope = session_scope
        if scope == "personal":
            project_id = None
        elif scope and scope.startswith("project:") and scope[8:]:
            project_id = scope[8:]
        else:
            raise HTTPException(403, {"code": "DATASET_SCOPE_REQUIRED"})
        # Cleanup is a separate, service-only operation with an explicit scope;
        # deletion coordinators must still be able to clean a deleting project.
        if body.cleanup and (body.session_id or body.scope is None or body.dataset_id is None):
            raise HTTPException(403, {"code": "DATASET_SCOPE_REQUIRED"})

        def resolve():
            if project_id:
                project = projects.get(project_id, user_id)
                if project is None or projects.is_legacy_default(project):
                    raise HTTPException(404, {"code": "DATASET_SCOPE_REQUIRED"})
                if project.delete_status and not body.cleanup:
                    raise HTTPException(409, {"code": "PROJECT_DELETING"})
            if body.dataset_id is not None:
                item = directory.get(body.dataset_id, user_id, project_id)
                if item is None:
                    raise HTTPException(404, {"code": "DATASET_SCOPE_REQUIRED"})
                if item.file_type not in {"sql", "csv", "txt"}:
                    raise HTTPException(422, {"code": CONSTRUCTION_NOT_APPLICABLE,
                        "message": CONSTRUCTION_NOT_APPLICABLE_MESSAGE})
                return AccessGrant(user_id=user_id, scope=scope, dataset_ids=[item.dataset_id],
                    source=SourceRecord(dataset_id=item.dataset_id, source_path=item.storage_path,
                                        source_filename=item.original_filename))
            return AccessGrant(user_id=user_id, scope=scope, dataset_ids=[item.dataset_id
                for item in directory.list(project_id, user_id) if item.file_type in {"sql", "csv", "txt"}])

        return await to_thread.run_sync(resolve)
