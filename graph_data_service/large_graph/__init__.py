"""Partitioned, durable large-graph computation path.

This package deliberately has no dependency on :class:`GraphData` or
NetworkX.  Keeping that boundary explicit prevents a future refactor from
accidentally materialising an entire large graph in the API process.
"""

from .contracts import DataQualityPolicy, LargeGraphCapability, RouteDecision
from .router import ComputeRouter, ResourceManager
from .service import LargeGraphService

__all__ = [
    "ComputeRouter",
    "DataQualityPolicy",
    "LargeGraphCapability",
    "LargeGraphService",
    "ResourceManager",
    "RouteDecision",
]
