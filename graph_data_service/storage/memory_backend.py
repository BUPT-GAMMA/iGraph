from __future__ import annotations

from graph_data_service.graph_data import GraphData
from graph_data_service.storage.base import GraphStorageBackend


class MemoryBackend(GraphStorageBackend):
    def __init__(self) -> None:
        self._graphs: dict[str, GraphData | None] = {}

    async def load(self, session_id: str) -> GraphData | None:
        return self._graphs.get(session_id)

    async def save(self, session_id: str, graph: GraphData | None) -> None:
        self._graphs[session_id] = graph

    async def exists(self, session_id: str) -> bool:
        return session_id in self._graphs and self._graphs[session_id] is not None
