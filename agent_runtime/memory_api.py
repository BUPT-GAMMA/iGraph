"""Manage the individual memories recalled by Agno's UserMemoryStore.

Agno stores the user's memory list inside one learning record. Mutations lock
that row and preserve its other entries and metadata, rather than deleting
the entire learning or writing to the unrelated legacy memories table.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import time

from agno.learn.utils import build_learning_id
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from execution_protocol import require_service


MEMORY_PREFIX = "learning:"


class LearnedMemoryUpdate(BaseModel):
    value: str = Field(min_length=1)
    key: str = ""


def _require_service(request: Request) -> None:
    require_service(request)


def _user_id(request: Request) -> str:
    uid = str(getattr(request.state, "user_id", "") or request.headers.get("X-User-ID", "")).strip()
    if not uid:
        raise HTTPException(401, "missing user identity")
    return uid


def _items(content: dict) -> list[dict]:
    return [
        {
            "memory_id": MEMORY_PREFIX + str(entry["id"]),
            "scope": "long_term",
            "category": entry.get("category") or "fact",
            "key": entry.get("key") or " ".join(str(entry.get("content") or "").split())[:80],
            "value": str(entry.get("content") or ""),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
            "source": "learning",
        }
        for entry in content.get("memories") or []
        if isinstance(entry, dict) and entry.get("id")
    ]


def create_memory_router(db) -> APIRouter:
    router = APIRouter(prefix="/internal/memory", tags=["iGraph Memory"], dependencies=[Depends(_require_service)])

    def table_and_owner(uid):
        # Pinned Agno 2.9 schema resolver respects the configured DB/schema/table.
        table = db._get_table(table_type="learnings", create_table_if_not_found=True)
        if table is None:
            raise HTTPException(503, "Memory storage unavailable")
        owner = (
            (table.c.learning_id == build_learning_id("user_memory", user_id=uid))
            & (table.c.learning_type == "user_memory")
            & (table.c.user_id == uid)
        )
        return table, owner

    @router.get("")
    def list_memories(request: Request):
        uid = _user_id(request)
        table, owner = table_and_owner(uid)
        with db.db_engine.connect() as connection:
            row = connection.execute(select(table.c.content).where(owner)).first()
        return {"success": True, "items": _items(row.content or {}) if row else [], "source": "learning"}

    def mutate(uid: str, memory_id: str, patch: LearnedMemoryUpdate | None):
        if not memory_id.startswith(MEMORY_PREFIX):
            raise HTTPException(404, "Memory not found")
        item_id = memory_id[len(MEMORY_PREFIX):]
        table, owner = table_and_owner(uid)
        with db.db_engine.begin() as connection:
            row = connection.execute(select(table.c.content).where(owner).with_for_update()).first()
            content = deepcopy(row.content) if row else {}
            memories = content.get("memories") or []
            entry = next((item for item in memories if isinstance(item, dict) and str(item.get("id")) == item_id), None)
            if entry is None:
                raise HTTPException(404, "Memory not found")
            now = datetime.now(timezone.utc).isoformat()
            if patch is None:
                content["memories"] = [item for item in memories if item is not entry]
            else:
                entry.update(content=patch.value, updated_at=now)
                if patch.key:
                    entry["key"] = patch.key
            content["updated_at"] = now
            connection.execute(update(table).where(owner).values(content=content, updated_at=int(time.time())))
        payload = {"success": True, "memory_id": memory_id, "source": "learning"}
        if patch is not None:
            payload["item"] = next(item for item in _items(content) if item["memory_id"] == memory_id)
        return payload

    @router.delete("/{memory_id}")
    def delete_memory(memory_id: str, request: Request):
        return mutate(_user_id(request), memory_id, None)

    @router.patch("/{memory_id}")
    def update_memory(memory_id: str, patch: LearnedMemoryUpdate, request: Request):
        return mutate(_user_id(request), memory_id, patch)

    return router
