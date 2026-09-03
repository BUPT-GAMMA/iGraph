from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_runtime.agent.memory.memory_engine import MemoryEngine


class MemoryService:
    """Governance wrapper over the built-in MemoryEngine."""

    def __init__(self, engine: MemoryEngine) -> None:
        self._engine = engine

    def list_items(self, *, scope: str = "long_term", query: str = "", limit: int = 100) -> dict[str, Any]:
        return {
            "scope": scope,
            "query": query,
            "items": self._engine.query_memories(scope=scope, query=query, limit=limit),
        }

    def write_item(
        self,
        *,
        scope: str,
        key: str,
        value: str,
        category: str = "fact",
        confidence: float = 0.8,
        reason: str = "",
        session_id: str = "api",
    ) -> dict[str, Any]:
        result = self._engine.write_memory_manual(
            scope=scope,
            key=key,
            value=value,
            category=category,
            confidence=confidence,
            reason=reason,
            session_id=session_id,
        )
        return {"written": bool(result), "result": result}

    def update_item(
        self,
        *,
        scope: str,
        memory_id: str = "",
        key: str = "",
        value: str = "",
        reason: str = "",
        session_id: str = "api",
    ) -> dict[str, Any]:
        result = self._engine.update_memory_manual(
            scope=scope,
            memory_id=memory_id,
            key=key,
            value=value,
            reason=reason,
            session_id=session_id,
        )
        return {"updated": bool(result), "result": result}

    def delete_item(self, *, scope: str, memory_id: str, reason: str = "", session_id: str = "api") -> dict[str, Any]:
        result = self._engine.delete_memory(scope=scope, memory_id=memory_id, reason=reason, session_id=session_id)
        return {"deleted": bool(result), "result": result}

    def events(self, *, limit: int = 200) -> dict[str, Any]:
        path = Path(self._engine.events_path)
        rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-max(1, limit):]:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        rows.append(item)
                except Exception:
                    continue
        return {"events": rows}

    def usage(self) -> dict[str, Any]:
        block = self._engine.get_system_block()
        included_ids: list[str] = []
        excluded_counts = {
            "archived": 0,
            "candidate": 0,
            "promoted_to_skill": 0,
            "char_budget": 0,
            "wrong_scope": 0,
        }

        snap = self._engine._load_snapshot()
        active_memories = [
            item
            for item in snap.get("items", [])
            if isinstance(item, dict) and item.get("status") == "active"
        ]
        picked_memories = self._engine._enforce_item_char_limit(
            sorted(active_memories, key=self._engine._score_item_for_snapshot, reverse=True),
            limit=8000,
            formatter=self._engine._format_memory_item,
        )
        picked_memory_ids = {str(item.get("memory_id") or "") for item in picked_memories}
        included_ids.extend(item_id for item_id in picked_memory_ids if item_id)
        for item in snap.get("items", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("memory_id") or "")
            if item.get("status") == "archived":
                excluded_counts["archived"] += 1
            elif item.get("status") == "active" and item_id and item_id not in picked_memory_ids:
                excluded_counts["char_budget"] += 1

        playbook_snap = self._engine._load_playbook_snapshot()
        visible_playbooks = [
            item
            for item in playbook_snap.get("items", [])
            if isinstance(item, dict) and self._engine._playbook_visible_in_snapshot(item)
        ]
        picked_playbooks = self._engine._enforce_item_char_limit(
            sorted(visible_playbooks, key=self._engine._score_item_for_snapshot, reverse=True),
            limit=4000,
            formatter=self._engine._format_playbook_item,
        )
        picked_playbook_ids = {str(item.get("playbook_id") or "") for item in picked_playbooks}
        included_ids.extend(item_id for item_id in picked_playbook_ids if item_id)
        for item in playbook_snap.get("items", []):
            if not isinstance(item, dict):
                continue
            lifecycle = str(item.get("lifecycle_status") or "").strip().lower()
            status = str(item.get("status") or "").strip().lower()
            item_id = str(item.get("playbook_id") or "")
            if status == "archived" or lifecycle == "archived":
                excluded_counts["archived"] += 1
            elif lifecycle == "candidate":
                excluded_counts["candidate"] += 1
            elif lifecycle == "promoted_to_skill":
                excluded_counts["promoted_to_skill"] += 1
            elif self._engine._playbook_visible_in_snapshot(item) and item_id and item_id not in picked_playbook_ids:
                excluded_counts["char_budget"] += 1

        profile = self._engine._load_user_profile()
        for item in profile.get("items", []):
            if isinstance(item, dict):
                item_id = str(item.get("profile_id") or item.get("key") or "")
                if item_id:
                    included_ids.append(item_id)
        return {
            "snapshot_char_count": len(block),
            "snapshot": block,
            "frozen": bool(getattr(self._engine, "_snapshot_frozen", False)),
            "included_item_ids": included_ids,
            "excluded_counts": excluded_counts,
            "scope": "default_user",
        }
