"""MCP session 映射注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class SessionBinding:
    session_id: str
    user_id: str = ""


class SessionRegistry:
    def __init__(self) -> None:
        self._session_map: Dict[str, SessionBinding] = {}

    @property
    def count(self) -> int:
        return len(self._session_map)

    def register(self, mcp_key: str, agent_session_id: str, user_id: str = "") -> None:
        self._session_map[mcp_key] = SessionBinding(
            session_id=str(agent_session_id or "").strip(),
            user_id=str(user_id or "").strip(),
        )

    def get(self, mcp_key: str) -> Optional[str]:
        binding = self._session_map.get(mcp_key)
        return binding.session_id if binding else None

    def get_user_id(self, mcp_key: str) -> str:
        binding = self._session_map.get(mcp_key)
        return binding.user_id if binding else ""

    def pop(self, mcp_key: str) -> Optional[str]:
        binding = self._session_map.pop(mcp_key, None)
        return binding.session_id if binding else None
