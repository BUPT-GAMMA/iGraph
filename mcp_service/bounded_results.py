"""Keep construction inspection out of model context when the graph is large.

The dataset API remains lossless. This adapter only bounds the MCP inspection
view; analytical tools continue to read the complete authorized server graph.
"""
from __future__ import annotations

from collections import Counter
import json


CONSTRUCTION_RESULT_MAX_BYTES = 64 * 1024


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _sample_value(value: object, depth: int = 0) -> object:
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:256] + "…[truncated]"
    if isinstance(value, (dict, list)) and depth >= 5:
        return "[nested value omitted]"
    if isinstance(value, dict):
        return {str(k)[:256]: _sample_value(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        return [_sample_value(item, depth + 1) for item in value[:10]]
    return value


def _sample_element(element: dict) -> dict:
    sample = _sample_value(element)
    # IDs/endpoints and type names are operation arguments, not display text.
    # Preserve them exactly so a sample can still drive subsequent graph edits.
    for key in ("id", "source", "target", "type"):
        if key in element:
            sample[key] = element[key]
    return sample


def bounded_construction_result(tool_name: str, payload: dict) -> dict:
    """Return a labelled, byte-bounded inspection view without mutating input."""
    if tool_name not in {"get_constructed_graph", "preview_graph_optimization", "commit_graph_optimization"}:
        return payload
    graph = payload.get("graph")
    if not isinstance(graph, dict) or _size(payload) <= CONSTRUCTION_RESULT_MAX_BYTES:
        return payload
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    sample_nodes = nodes[:20]
    sample_ids = {node.get("id") for node in sample_nodes}
    sample_edges = [edge for edge in edges
                    if edge.get("source") in sample_ids and edge.get("target") in sample_ids][:40]
    result = {key: _sample_value(value) for key, value in payload.items() if key != "graph"}
    result["graph"] = {"directed": graph.get("directed", True),
                       "nodes": [_sample_element(node) for node in sample_nodes],
                       "edges": [_sample_element(edge) for edge in sample_edges]}
    result["inspection"] = {
        "truncated": True, "complete_graph": False, "properties_may_be_truncated": True,
        "node_count": len(nodes), "edge_count": len(edges),
        "node_types": _sample_value(dict(Counter(str(n.get("type", "")) for n in nodes))),
        "edge_types": _sample_value(dict(Counter(str(e.get("type", "")) for e in edges))),
        "max_response_bytes": CONSTRUCTION_RESULT_MAX_BYTES,
        "message": "仅为有界检查样本，不能据此计算全图结果。完整数据仍保留在服务端。"
                   "请用专用图工具计算；股权任务使用 finance_inspect_ownership 和其他 finance_* 工具。",
    }
    # Pathological wide properties or other response metadata must also obey
    # the byte limit. Count caps alone did not prevent the observed failure.
    if _size(result) > CONSTRUCTION_RESULT_MAX_BYTES:
        result = {key: _sample_value(payload[key]) for key in
                  ("success", "dataset_id", "revision", "base_revision") if key in payload}
        result["graph"] = {"directed": graph.get("directed", True), "nodes": [], "edges": []}
        result["inspection"] = {
            "truncated": True, "complete_graph": False, "properties_may_be_truncated": True,
            "node_count": len(nodes), "edge_count": len(edges),
            "max_response_bytes": CONSTRUCTION_RESULT_MAX_BYTES,
            "message": "属性过大，检查样本已省略；完整数据仍保留在服务端，请使用专用分析工具。",
        }
    return result
