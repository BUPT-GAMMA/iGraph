"""Per-call context and bounded result contract for remote graph products."""
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import QueryRequest, ResolvedConnection
from .policy import NetworkPolicy


@dataclass(frozen=True)
class AdapterContext:
    connection: ResolvedConnection
    deadline: float
    max_rows: int
    max_bytes: int
    network_policy: NetworkPolicy


@dataclass
class AdapterRows:
    columns: list[str]
    rows: list[list[Any]]
    returned_rows: int
    truncated: bool


class GraphAdapter(Protocol):
    async def test(self, ctx: AdapterContext) -> dict: ...

    async def describe(self, ctx: AdapterContext, labels, relationship_types) -> dict: ...

    async def execute(self, ctx: AdapterContext, request: QueryRequest) -> AdapterRows: ...
