"""Shared approval policy for graph tools."""

from __future__ import annotations


# An explicit user request to train is sufficient authorization for the
# non-destructive training run. Only destructive graph operations pause for
# confirmation.
GRAPH_TOOLS_REQUIRING_CONFIRMATION = frozenset({"clear_graph"})
