"""Pure argument adaptation for MCP-to-GraphData tool forwarding."""

from __future__ import annotations


def forwarded_graph_tool_arguments(
    tool_name: str,
    arguments: dict | None,
) -> dict:
    """Inject private async markers without changing public tool schemas."""
    forwarded_arguments = dict(arguments or {})
    if tool_name == "train":
        algo_config = forwarded_arguments.get("algo_config")
        if not isinstance(algo_config, dict):
            algo_config = {}
        forwarded_arguments["algo_config"] = {
            **algo_config,
            "__igraph_async_job": True,
        }
    elif tool_name == "load_graph_classification_dataset":
        forwarded_arguments["__async"] = True
    return forwarded_arguments


__all__ = ["forwarded_graph_tool_arguments"]
