"""Shared approval policy for graph tools."""

from __future__ import annotations


# An explicit user request to train is sufficient authorization for the
# non-destructive training run. Destructive graph operations and persistent
# dataset optimizations pause with the concrete tool arguments for approval.
# Read-only optimization previews and loading saved datasets need no approval.
GRAPH_TOOLS_REQUIRING_CONFIRMATION = frozenset({"clear_graph", "commit_graph_optimization"})
