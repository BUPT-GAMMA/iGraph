from __future__ import annotations

from graph_data_service.graph_data import GraphData
from graph_data_service.storage.base import GraphStorageBackend


class Neo4jBackend(GraphStorageBackend):
    def __init__(self, uri: str, user: str, password: str) -> None:
        self.uri = uri
        self.user = user
        self.password = password

    async def load(self, session_id: str) -> GraphData | None:
        raise NotImplementedError("Neo4j backend is optional and not enabled in this lightweight build")

    async def save(self, session_id: str, graph: GraphData | None) -> None:
        raise NotImplementedError("Neo4j backend is optional and not enabled in this lightweight build")

    async def exists(self, session_id: str) -> bool:
        return False
