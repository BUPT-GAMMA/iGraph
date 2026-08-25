"""Factories for the in-process Agent Runtime implementation."""

from __future__ import annotations

from typing import Any

from .contracts import RuntimeSession
from .legacy import LegacyIGraphRuntime


def adapt_legacy_agent(agent: Any) -> RuntimeSession:
    if isinstance(agent, LegacyIGraphRuntime):
        return agent
    return LegacyIGraphRuntime(agent)


def create_runtime(**kwargs: Any) -> RuntimeSession:
    # Lazy import keeps route-only tests free from model dependencies.
    from .agent.igraph import IGraph

    return LegacyIGraphRuntime(IGraph(**kwargs))
