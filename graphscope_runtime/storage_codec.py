"""Compact, backwards-compatible rows for immutable large-graph snapshots."""

from __future__ import annotations

import json
from typing import Any


SNAPSHOT_FORMAT_V1 = "igraph-partitioned-jsonl-v1"
SNAPSHOT_FORMAT_V2 = "igraph-partitioned-jsonl-v2"
SUPPORTED_SNAPSHOT_FORMATS = frozenset({SNAPSHOT_FORMAT_V1, SNAPSHOT_FORMAT_V2})
RECORD_ENCODING_V2 = "compact-json-array-v1"


def encode_edge_row(
    source: str, target: str, weight: float = 1.0, relation: str = ""
) -> str:
    row: list[Any] = [str(source), str(target)]
    numeric_weight = float(weight)
    relation_text = str(relation)
    if relation_text:
        row.extend((numeric_weight, relation_text))
    elif numeric_weight != 1.0:
        row.append(numeric_weight)
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def decode_edge_row(payload: str | list[Any] | dict[str, Any]) -> tuple[str, str, float, str]:
    row = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(row, dict):
        return (
            str(row["source"]),
            str(row["target"]),
            float(row.get("weight", 1.0)),
            str(row.get("relation", "")),
        )
    if not isinstance(row, list) or not 2 <= len(row) <= 4:
        raise ValueError("快照边记录格式错误")
    return (
        str(row[0]),
        str(row[1]),
        float(row[2]) if len(row) >= 3 else 1.0,
        str(row[3]) if len(row) >= 4 else "",
    )


def encode_node_row(node_id: str, name: str, ordinal: int) -> str:
    canonical = str(node_id)
    display = str(name or canonical)
    row: list[Any] = (
        [canonical, int(ordinal)]
        if display == canonical
        else [canonical, display, int(ordinal)]
    )
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def decode_node_row(payload: str | list[Any] | dict[str, Any]) -> tuple[str, str, int]:
    row = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(row, dict):
        node_id = str(row["id"])
        return node_id, str(row.get("name") or node_id), int(row.get("ordinal", 0))
    if not isinstance(row, list) or len(row) not in {2, 3}:
        raise ValueError("快照节点记录格式错误")
    node_id = str(row[0])
    if len(row) == 2:
        return node_id, node_id, int(row[1])
    return node_id, str(row[1] or node_id), int(row[2])
