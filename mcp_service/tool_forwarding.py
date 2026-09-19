"""Pure argument adaptation for MCP-to-GraphData tool forwarding."""

from __future__ import annotations

import json
from typing import Any


_SCHEMA_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": float,
    # Agno currently serializes nested MCP object arguments as JSON strings.
    # Accept both representations at the FastMCP boundary; the forwarding
    # adapter below remains the single place that normalizes them to dicts.
    "object": dict | str,
    "array": list,
}


def schema_parameter_type(schema_type: str) -> Any:
    """Return a FastMCP-compatible annotation for a JSON Schema type."""
    return _SCHEMA_TYPE_MAP.get(schema_type, Any)


def _decode_object_argument(value):
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return decoded if isinstance(decoded, dict) else value


def forwarded_graph_tool_arguments(
    tool_name: str,
    arguments: dict | None,
) -> dict:
    """Normalize provider arguments and inject private async markers."""
    forwarded_arguments = dict(arguments or {})
    if tool_name == "visualize_subgraph":
        for field, expected_type in (("node_names", list), ("highlights", dict)):
            value = forwarded_arguments.get(field)
            if not isinstance(value, str):
                continue
            try:
                decoded = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"{field} must contain valid JSON") from exc
            if not isinstance(decoded, expected_type):
                raise ValueError(f"{field} must decode to {expected_type.__name__}")
            if field == "node_names" and not all(isinstance(node, str) for node in decoded):
                raise ValueError("node_names must contain only node ID strings")
            forwarded_arguments[field] = decoded
    object_fields = (
        ("data", "train_config", "algo_config")
        if tool_name == "train"
        else ("query", "infer_config") if tool_name == "infer" else ()
    )
    for field in object_fields:
        if field in forwarded_arguments:
            forwarded_arguments[field] = _decode_object_argument(forwarded_arguments[field])
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


__all__ = ["forwarded_graph_tool_arguments", "schema_parameter_type"]
