from __future__ import annotations

from abc import ABC, abstractmethod

from graph_data_service.graph_data import GraphData


class GraphStorageBackend(ABC):
    @abstractmethod
    async def load(self, session_id: str) -> GraphData | None:
        raise NotImplementedError

    @abstractmethod
    async def save(self, session_id: str, graph: GraphData | None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def exists(self, session_id: str) -> bool:
        raise NotImplementedError
