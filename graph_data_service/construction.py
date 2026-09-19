"""Durable property graphs and explicit, transactional graph optimizations.

This store remains algorithm-agnostic. Graph Engine installs private SQL/CSV/TXT
construction workers through ``register_algorithm(format, builder)``. The callable
receives the retained source Path and JSON options and returns the graph document
specified below. Its output passes the same validation as every optimization.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import graph_engine.paths as paths
from graph_engine.analysis_policy import numeric_weight
from graph_data_service.construction_schema import source_schema_tables

FORMATS = ("sql", "csv", "txt")
OPERATIONS = ("collapse_node", "node_property_to_edge", "extract_property_node",
              "delete", "reverse_edges", "rename_type")
PUBLIC_PROGRESS_STAGES = ("starting", "source_profiled", "extracting_entities", "materializing_graph",
                          "generating_workload", "evaluating_baseline", "evolving",
                          "validating_final", "ready", "failed")
PUBLIC_PROGRESS_KINDS = ("stage", "candidate", "terminal")
PUBLIC_PROGRESS_OUTCOMES = ("accepted", "rejected", "ready", "failed")
PUBLIC_PROGRESS_METRICS = ("query_count", "max_iterations", "iterations", "tool_calls",
                           "evaluated_candidates", "accepted_candidates", "initial_nodes", "initial_edges",
                           "chunks_completed", "chunks_total", "extracted_nodes", "extracted_edges")
SCHEMA_PROPERTY_TYPES = ("null", "boolean", "integer", "number", "string", "object", "array")
_TEXT_EXTRACTION_FAILURE_MESSAGES = {
    "text_extraction_requires_llm": "TXT 实体关系抽取需要智能构图模式；源文件已保留，请选择 LLM 抽取后重试。",
    "text_extraction_llm_unavailable": "TXT 实体关系抽取需要可用的构图模型配置；源文件已保留，请配置模型或改用表格/逐行记录模式。",
    "text_extraction_empty": "未从 TXT 中识别到明确的实体关系；源文件已保留，可补充更明确的文本或抽取目标后重试。",
    "text_extraction_input_too_large": "TXT 超过本次语义抽取的字符上限；源文件已保留，请拆分文件后重试。",
    "text_extraction_chunk_limit_exceeded": "TXT 超过本次语义抽取的分块上限；源文件已保留，请拆分文件后重试。",
    "text_extraction_call_limit_exceeded": "TXT 抽取达到模型调用上限且尚未完整处理；未发布半成品图，请拆分文件后重试。",
    "text_extraction_graph_too_large": "抽取到的实体关系超过图规模上限；源文件已保留，请拆分文件后重试。",
}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class ConstructionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422, **details):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"code": code, "message": message, **details}


def _fail(message: str, code: str = "invalid_operation", status_code: int = 422, **details):
    raise ConstructionError(code, message, status_code, **details)


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        _fail(f"{field} must be a nonempty string (maximum 1024 characters).")
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}", value):
        _fail("Invalid user or dataset identifier.", "invalid_identity")
    return value


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _fail("Properties and options must contain finite JSON values.", "invalid_graph")


def _json_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("Property names must be strings.", "invalid_graph")
            _json_keys(item)
    elif isinstance(value, list):
        for item in value:
            _json_keys(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        _fail("Properties must contain JSON values.", "invalid_graph")


def validate_graph(graph: dict, *, max_nodes: int = 200_000, max_edges: int = 1_000_000,
                   copy_graph: bool = True) -> dict:
    """Copy and validate a complete property graph; never coerce identifiers."""
    from graph_engine.construction.budget import checked, check_deadline
    check_deadline()
    if not isinstance(graph, dict) or set(graph) != {"directed", "nodes", "edges"}:
        _fail("Graph requires exactly directed, nodes and edges.", "invalid_graph")
    if not isinstance(graph["directed"], bool):
        _fail("Graph directed must be a boolean.", "invalid_graph")
    if type(copy_graph) is not bool:
        _fail("copy_graph must be a boolean.", "invalid_graph")
    if (type(max_nodes) is not int or type(max_edges) is not int
            or not 1 <= max_nodes <= 20_000_000 or not 1 <= max_edges <= 50_000_000):
        _fail("Graph limits are invalid.", "invalid_graph")
    if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
        _fail("Graph nodes and edges must be arrays.", "invalid_graph")
    if len(graph["nodes"]) > max_nodes or len(graph["edges"]) > max_edges:
        _fail("Graph exceeds the construction document size limit.", "graph_too_large", 413)
    node_ids, edge_ids = set(), set()
    result = copy.deepcopy(graph) if copy_graph else graph
    for target, identities, fields in (("nodes", node_ids, {"id", "type", "properties"}),
                                        ("edges", edge_ids, {"id", "type", "properties", "source", "target"})):
        for row in checked(result[target]):
            if not isinstance(row, dict) or set(row) != fields:
                _fail(f"Each {target} entry requires exactly {', '.join(sorted(fields))}.", "invalid_graph")
            identity = _text(row["id"], "id")
            _text(row["type"], "type")
            if identity in identities:
                _fail(f"Duplicate {target} id: {identity}", "invalid_graph")
            identities.add(identity)
            if not isinstance(row["properties"], dict):
                _fail("properties must be an object.", "invalid_graph")
            _json_keys(row["properties"])
            _json(row["properties"])
            if target == "edges":
                if "weight" in row["properties"]:
                    try:
                        numeric_weight(row["properties"]["weight"])
                    except ValueError:
                        _fail(f"Edge {identity} weight must be a finite number.", "invalid_graph")
                _text(row["source"], "source")
                _text(row["target"], "target")
                if row["source"] not in node_ids or row["target"] not in node_ids:
                    _fail(f"Edge {identity} has a missing endpoint.", "invalid_graph")
    return result


def _schema_value_type(value) -> str:
    """Return an unambiguous JSON type name (``bool`` is not an integer)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    return "array"


def _schema_property_path(parent: str, key: str) -> str:
    """Use readable dotted paths while keeping unusual JSON keys unambiguous."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}" if parent else key
    quoted = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return f"{parent}[{quoted}]" if parent else f"[{quoted}]"


def _collect_schema_observations(value, path: str, observations: dict[str, Counter]):
    observations.setdefault(path, Counter())[_schema_value_type(value)] += 1
    if isinstance(value, dict):
        for key in sorted(value):
            _collect_schema_observations(value[key], _schema_property_path(path, key), observations)
    elif isinstance(value, list):
        for item in value:
            _collect_schema_observations(item, path + "[]", observations)


def _schema_property_stats(rows: list[dict]) -> list[dict]:
    """Aggregate recursive paths without retaining or returning any source value."""
    aggregate: dict[str, dict] = {}
    for row in rows:
        observations: dict[str, Counter] = {}
        for key in sorted(row["properties"]):
            _collect_schema_observations(row["properties"][key], _schema_property_path("", key), observations)
        for path, counts in observations.items():
            current = aggregate.setdefault(path, {"present_count": 0, "type_counts": Counter()})
            current["present_count"] += 1
            current["type_counts"].update(counts)
    result = []
    for path in sorted(aggregate):
        counts = aggregate[path]["type_counts"]
        type_counts = {kind: counts[kind] for kind in SCHEMA_PROPERTY_TYPES if counts[kind]}
        result.append({
            "path": path,
            "types": list(type_counts),
            "type_counts": type_counts,
            "present_count": aggregate[path]["present_count"],
            "value_count": sum(type_counts.values()),
        })
    return result


def graph_schema(graph: dict) -> dict:
    """Build a deterministic, value-free meta-graph from a property graph.

    An undirected graph canonicalizes the endpoint type pair, while a directed
    graph keeps opposite relationships as distinct schema edges.
    """
    graph = validate_graph(graph, copy_graph=False)
    nodes_by_type: dict[str, list[dict]] = {}
    node_type_by_id: dict[str, str] = {}
    for node in graph["nodes"]:
        nodes_by_type.setdefault(node["type"], []).append(node)
        node_type_by_id[node["id"]] = node["type"]

    edges_by_signature: dict[tuple[str, str, str], list[dict]] = {}
    for edge in graph["edges"]:
        source_type = node_type_by_id[edge["source"]]
        target_type = node_type_by_id[edge["target"]]
        if not graph["directed"] and target_type < source_type:
            source_type, target_type = target_type, source_type
        signature = (source_type, edge["type"], target_type)
        edges_by_signature.setdefault(signature, []).append(edge)

    node_types = []
    schema_nodes = []
    for type_name in sorted(nodes_by_type):
        rows = nodes_by_type[type_name]
        property_paths = _schema_property_stats(rows)
        item = {"id": type_name, "type": type_name, "instance_count": len(rows),
                "property_paths": property_paths}
        node_types.append(item)
        schema_nodes.append({
            "id": type_name,
            "label": type_name,
            "type": "schema_node",
            "properties": {"schema_kind": "node_type", "type_name": type_name,
                           "instance_count": len(rows), "property_paths": property_paths},
        })

    edge_types = []
    schema_edges = []
    for (source_type, type_name, target_type), rows in sorted(edges_by_signature.items()):
        edge_id = _generated_id("schema_edge_", graph["directed"], source_type, type_name, target_type)
        property_paths = _schema_property_stats(rows)
        item = {"id": edge_id, "type": type_name, "source_type": source_type,
                "target_type": target_type, "relationship_count": len(rows),
                "property_paths": property_paths}
        edge_types.append(item)
        schema_edges.append({
            "id": edge_id,
            "source": source_type,
            "target": target_type,
            "type": type_name,
            "label": type_name,
            "properties": {"schema_kind": "edge_type", "type_name": type_name,
                           "source_type": source_type, "target_type": target_type,
                           "relationship_count": len(rows), "property_paths": property_paths},
        })

    return {
        "directed": graph["directed"],
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "node_type_count": len(node_types),
        "edge_type_count": len(edge_types),
        "node_types": node_types,
        "edge_types": edge_types,
        "graph": {
            "visualization_kind": "schema",
            "directed": graph["directed"],
            "nodes": schema_nodes,
            "edges": schema_edges,
            "num_nodes": len(schema_nodes),
            "num_edges": len(schema_edges),
        },
    }


def _ids(value, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(f"{field} must be a nonempty array of identifiers.")
    result = [_text(item, field) for item in value]
    if len(result) != len(set(result)):
        _fail(f"{field} must not contain duplicate identifiers.")
    return result


def _boolean(operation: dict, key: str, default: bool) -> bool:
    value = operation.get(key, default)
    if not isinstance(value, bool):
        _fail(f"{key} must be a boolean.")
    return value


def _generated_id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()[:32]


def _get(rows: dict, identity: str, target: str) -> dict:
    _text(identity, f"{target} id")
    if identity not in rows:
        _fail(f"Unknown {target} id: {identity}")
    return rows[identity]


def _row_data(row: dict) -> dict | None:
    """Recognize source rows; a legacy property merely named data stays literal."""
    props = row["properties"]
    if (isinstance(props.get("table"), str) and props["table"] and
            type(props.get("row_index")) is int and props["row_index"] >= 0 and
            isinstance(props.get("data"), dict)):
        return props["data"]
    return None


def _relocated_attributes(row: dict) -> list:
    records = row["properties"].get("relocated_attributes")
    if ("source" in row and isinstance(records, list) and records and all(
            isinstance(record, dict) and {"row_id", "column", "value"} <= record.keys() and
            not record.keys() - {"row_id", "column", "value", "name"} and
            all(isinstance(record.get(key), str) and record[key] for key in ("row_id", "column")) and
            ("name" not in record or isinstance(record["name"], str) and record["name"])
            for record in records)):
        return records
    return []


def _property_locations(row: dict, name: str) -> list[tuple[dict, str]]:
    data = _row_data(row)
    container = row["properties"] if data is None else data
    relocated = _relocated_attributes(row)
    locations = [(container, name)] if name in container and not (data is None and relocated and name == "relocated_attributes") else []
    locations.extend((record, "value") for record in relocated if record.get("name", record["column"]) == name)
    return locations


def _property_value(row: dict, name: str):
    locations = _property_locations(row, name)
    if not locations:
        _fail(f"Element {row['id']} has no business property {name}.")
    value = locations[0][0][locations[0][1]]
    if any(_json(container[key]) != _json(value) for container, key in locations[1:]):
        _fail(f"Element {row['id']} has conflicting values for {name}.", "property_conflict")
    return value


def _remove_property(row: dict, name: str):
    _property_value(row, name)
    records = _relocated_attributes(row)
    had_records = bool(records)
    for container, key in _property_locations(row, name):
        if any(container is record for record in records):
            records[:] = [record for record in records if record is not container]
        else:
            del container[key]
    if had_records and not records:
        del row["properties"]["relocated_attributes"]


def _relocate_property(node: dict, edge: dict, prop: str, destination: str, value):
    records = _relocated_attributes(edge)
    if "relocated_attributes" in edge["properties"] and not records:
        _fail("The edge contains an invalid or conflicting relocated_attributes property.", "property_conflict")
    matching = [record for record in records if record["row_id"] == node["id"] and record["column"] == prop]
    expected = {"row_id": node["id"], "column": prop, "name": destination, "value": copy.deepcopy(value)}
    if matching:
        if len(matching) != 1 or matching[0] != expected:
            _fail(f"Edge {edge['id']} already has a different value or name for {destination}.", "property_conflict")
        return
    if _property_locations(edge, destination):
        _fail(f"Edge {edge['id']} already has a property named {destination}.", "property_conflict")
    edge["properties"].setdefault("relocated_attributes", []).append(expected)


def optimize_graph(graph: dict, operations: list[dict], *, max_nodes: int = 200_000, max_edges: int = 1_000_000) -> tuple[dict, dict]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        _fail("Provide between 1 and 100 operations.")
    original = validate_graph(graph, max_nodes=max_nodes, max_edges=max_edges, copy_graph=False)
    candidate = copy.deepcopy(original)
    fields = {
        "collapse_node": {"op", "node_id", "edge_type"},
        "node_property_to_edge": {"op", "node_id", "property", "edge_ids", "target_property", "remove_from_node"},
        "extract_property_node": {"op", "node_ids", "property", "node_type", "edge_type", "remove_from_node"},
        "delete": {"op", "target", "ids", "property", "cascade"},
        "reverse_edges": {"op", "edge_ids"},
        "rename_type": {"op", "target", "old_type", "new_type"},
    }
    for index, operation in enumerate(operations):
        try:
            if not isinstance(operation, dict) or not isinstance(operation.get("op"), str) or operation.get("op") not in fields:
                _fail("Unsupported graph optimization operation.")
            kind = operation["op"]
            if set(operation) - fields[kind]:
                _fail("Unknown operation parameters: " + ", ".join(sorted(set(operation) - fields[kind])))
            nodes = {row["id"]: row for row in candidate["nodes"]}
            edges = {row["id"]: row for row in candidate["edges"]}
            if kind == "collapse_node":
                node = _get(nodes, operation.get("node_id"), "node")
                incident = [e for e in edges.values() if node["id"] in (e["source"], e["target"])]
                if len(incident) != 2 or any(e["source"] == e["target"] for e in incident):
                    _fail("Collapse requires exactly two incident edges and no self-loop.")
                if candidate["directed"]:
                    incoming = [e for e in incident if e["target"] == node["id"]]
                    outgoing = [e for e in incident if e["source"] == node["id"]]
                    if len(incoming) != 1 or len(outgoing) != 1:
                        _fail("A directed intermediate node must have one incoming and one outgoing edge.")
                    source, target = incoming[0]["source"], outgoing[0]["target"]
                else:
                    source, target = [e["target"] if e["source"] == node["id"] else e["source"] for e in incident]
                if source == target:
                    _fail("Collapse would create a self-loop instead of connecting distinct neighbors.")
                edge_type = _text(operation.get("edge_type"), "edge_type")
                new_id = _generated_id("collapsed_", node["id"], sorted(e["id"] for e in incident), edge_type)
                if new_id in edges:
                    _fail("Generated edge identifier already exists.")
                # Preserve complete records, including conflicting property names and
                # relationship types, without overwriting any user property.
                edge = {"id": new_id, "source": source, "target": target, "type": edge_type,
                        "properties": {"_collapsed_node": copy.deepcopy(node), "_collapsed_edges": copy.deepcopy(incident)}}
                candidate["nodes"] = [n for n in candidate["nodes"] if n["id"] != node["id"]]
                candidate["edges"] = [e for e in candidate["edges"] if e not in incident] + [edge]
            elif kind == "node_property_to_edge":
                node = _get(nodes, operation.get("node_id"), "node")
                prop = _text(operation.get("property"), "property")
                destination = _text(operation.get("target_property", prop), "target_property")
                value = _property_value(node, prop)
                selected = [_get(edges, identity, "edge") for identity in _ids(operation.get("edge_ids"), "edge_ids")]
                for edge in selected:
                    if node["id"] not in (edge["source"], edge["target"]):
                        _fail("The selected edges must be incident to the selected node.")
                    if _row_data(node) is not None:
                        _relocate_property(node, edge, prop, destination, value)
                    else:
                        if destination == "relocated_attributes" and _relocated_attributes(edge) and _row_data(edge) is None:
                            _fail("Cannot overwrite current attribute storage.", "property_conflict")
                        if _property_locations(edge, destination) and _json(_property_value(edge, destination)) != _json(value):
                            _fail(f"Edge {edge['id']} already has a different value for {destination}.", "property_conflict")
                        data = _row_data(edge)
                        (edge["properties"] if data is None else data)[destination] = copy.deepcopy(value)
                if _boolean(operation, "remove_from_node", True):
                    _remove_property(node, prop)
            elif kind == "extract_property_node":
                selected = [_get(nodes, identity, "node") for identity in _ids(operation.get("node_ids"), "node_ids")]
                prop = _text(operation.get("property"), "property")
                node_type, edge_type = _text(operation.get("node_type"), "node_type"), _text(operation.get("edge_type"), "edge_type")
                remove = _boolean(operation, "remove_from_node", True)
                for node in selected:
                    value = _property_value(node, prop)
                    source_row = _row_data(node) is not None
                    new_id = _generated_id("attribute_" if source_row else "property_", node_type, prop, value)
                    if new_id == node["id"]:
                        _fail("A property node cannot be extracted into itself.")
                    new_node = {"id": new_id, "type": node_type, "properties": {"value" if source_row else prop: copy.deepcopy(value)}}
                    if new_id in nodes and nodes[new_id] != new_node:
                        _fail("Generated property node conflicts with an existing node.", "property_conflict")
                    if new_id not in nodes:
                        candidate["nodes"].append(new_node)
                        nodes[new_id] = new_node
                    edge_id = (_generated_id("attribute_link_", node["id"], prop, new_id) if source_row else
                               _generated_id("property_edge_", node["id"], new_id, edge_type))
                    metadata = {"extracted_attribute": {"table": node["properties"]["table"], "column": prop}} if source_row else {}
                    new_edge = {"id": edge_id, "source": node["id"], "target": new_id, "type": edge_type, "properties": metadata}
                    if edge_id in edges and edges[edge_id] != new_edge:
                        _fail("Generated property edge conflicts with an existing edge.")
                    if edge_id not in edges:
                        candidate["edges"].append(new_edge)
                        edges[edge_id] = new_edge
                    if remove:
                        _remove_property(node, prop)
            elif kind == "delete":
                target = operation.get("target")
                if not isinstance(target, str) or target not in {"node", "edge"}:
                    _fail("delete target must be node or edge.")
                identities = set(_ids(operation.get("ids"), "ids"))
                selected = [_get(nodes if target == "node" else edges, identity, target) for identity in identities]
                cascade = _boolean(operation, "cascade", False)
                if "property" in operation:
                    prop = _text(operation["property"], "property")
                    if cascade:
                        _fail("cascade does not apply to property deletion.")
                    for row in selected:
                        _remove_property(row, prop)
                elif target == "edge":
                    if cascade:
                        _fail("cascade applies only to node deletion.")
                    candidate["edges"] = [row for row in candidate["edges"] if row["id"] not in identities]
                else:
                    incident = [e for e in candidate["edges"] if e["source"] in identities or e["target"] in identities]
                    if incident and not cascade:
                        _fail("Node deletion requires cascade=true when incident edges exist.")
                    candidate["nodes"] = [n for n in candidate["nodes"] if n["id"] not in identities]
                    candidate["edges"] = [e for e in candidate["edges"] if e not in incident]
            elif kind == "reverse_edges":
                if not candidate["directed"]:
                    _fail("An undirected graph has no edge direction to reverse.")
                for identity in _ids(operation.get("edge_ids"), "edge_ids"):
                    edge = _get(edges, identity, "edge")
                    edge["source"], edge["target"] = edge["target"], edge["source"]
            elif kind == "rename_type":
                target = operation.get("target")
                if not isinstance(target, str) or target not in {"node", "edge"}:
                    _fail("rename_type target must be node or edge.")
                old_type, new_type = _text(operation.get("old_type"), "old_type"), _text(operation.get("new_type"), "new_type")
                selected = [row for row in (nodes if target == "node" else edges).values() if row["type"] == old_type]
                if not selected or old_type == new_type:
                    _fail("Choose an existing type and a different replacement name.")
                for row in selected:
                    row["type"] = new_type
            validate_graph(candidate, max_nodes=max_nodes, max_edges=max_edges, copy_graph=False)
        except ConstructionError as exc:
            exc.detail["operation_index"] = index
            raise
    if candidate == original:
        _fail("The operations produce no graph changes.")
    def counts(doc):
        return {"node_count": len(doc["nodes"]), "edge_count": len(doc["edges"])}
    def changed(key):
        before, after = ({row["id"]: row for row in doc[key]} for doc in (original, candidate))
        return sum(before.get(identity) != after.get(identity) for identity in before.keys() | after.keys())
    return candidate, {"before": counts(original), "after": counts(candidate),
                       "changed_nodes": changed("nodes"), "changed_edges": changed("edges")}


def _atomic_json(path: Path, value, *, before_replace=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".construction-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for chunk in encoder.iterencode(value):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace:
            before_replace()
        os.replace(temporary, path)
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_public_progress(directory: Path, record: dict):
    """Return a bounded, whitelisted worker projection; never expose its report."""
    try:
        value = json.loads((directory / "construction-progress.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    if (not isinstance(value, dict) or value.get("version") != "construction_progress.v1"
            or value.get("build_id") != record.get("build_id")):
        return None
    stage = value.get("stage")
    if stage not in PUBLIC_PROGRESS_STAGES:
        return None
    result = {"version": "construction_progress.v1", "stage": stage}
    if value.get("status") in {"running", "ready", "failed"}:
        result["status"] = value["status"]
    for key in ("current_message", "updated_at"):
        item = value.get(key)
        if isinstance(item, str) and len(item) <= (320 if key == "current_message" else 80):
            result[key] = item
    elapsed = value.get("elapsed_seconds")
    if type(elapsed) in (int, float) and 0 <= elapsed <= 43_200:
        result["elapsed_seconds"] = elapsed
    metrics = value.get("metrics")
    if isinstance(metrics, dict):
        result["metrics"] = {key: metrics[key] for key in PUBLIC_PROGRESS_METRICS
                             if type(metrics.get(key)) is int and 0 <= metrics[key] <= 50_000_000}
    events = []
    if isinstance(value.get("events"), list):
        for item in value["events"][-24:]:
            if not isinstance(item, dict) or item.get("kind") not in PUBLIC_PROGRESS_KINDS:
                continue
            event_stage, identity, message = item.get("stage"), item.get("id"), item.get("message")
            if (event_stage not in PUBLIC_PROGRESS_STAGES or not isinstance(identity, str) or len(identity) > 80
                    or not isinstance(message, str) or not message or len(message) > 320):
                continue
            event = {"id": identity, "kind": item["kind"], "stage": event_stage, "message": message}
            outcome, timestamp = item.get("outcome"), item.get("timestamp")
            if outcome in PUBLIC_PROGRESS_OUTCOMES:
                event["outcome"] = outcome
            if isinstance(timestamp, str) and len(timestamp) <= 80:
                event["timestamp"] = timestamp
            events.append(event)
    result["events"] = events
    return result


class ConstructionStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).resolve() if root else paths.data_root()
        self._algorithms: dict[str, Callable[[Path, dict], dict]] = {}
        self._process_algorithms: set[str] = set()

    def register_algorithm(self, source_type: str, algorithm: Callable[[Path, dict], dict]):
        if source_type not in FORMATS or not callable(algorithm):
            raise ValueError("Register a callable for sql, csv or txt.")
        self._algorithms[source_type] = algorithm
        self._process_algorithms.discard(source_type)

    def capabilities(self):
        return {"formats": [{"source_type": kind, "algorithm_available": kind in self._algorithms,
                              "status": "available" if kind in self._algorithms else "algorithm_unavailable"} for kind in FORMATS],
                "operations": list(OPERATIONS), "schema": "construction_graph.v1"}

    def _directory(self, user_id, dataset_id):
        result = self.root / "users" / _identity(user_id) / "datasets" / _identity(dataset_id) / "construction"
        if not result.resolve().is_relative_to(self.root):
            _fail("Construction path escapes the storage root.", "invalid_identity")
        return result

    @contextmanager
    def _lock(self, user_id, dataset_id, *, blocking=True, lease_fds=None):
        directory = self._directory(user_id, dataset_id)
        key = str(directory)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        if not lock.acquire(blocking=blocking):
            yield None
            return
        try:
            lock_dir = self.root / ".construction_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
            with lock_path.open("a+b") as handle:
                try:
                    if os.name == "posix":
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    else:
                        import msvcrt
                        # Reading byte zero can itself fail when another process
                        # holds the Windows byte-range lock. fstat does not read it.
                        if os.fstat(handle.fileno()).st_size == 0:
                            handle.write(b"0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        yield None
                        return
                    raise
                try:
                    if lease_fds is not None:
                        lease_fds.append(handle.fileno())
                    yield directory
                finally:
                    if os.name == "posix":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock.release()

    @contextmanager
    def _write_lock(self, user_id, dataset_id, *, recovering=False):
        while True:
            with self._lock(user_id, dataset_id, blocking=False) as directory:
                record = self._read(directory or self._directory(user_id, dataset_id), dataset_id)
                if record["status"] == "building" and (directory is None or not recovering and record.get("executor_instance")):
                    _fail("构图任务仍在运行或确认退出，请稍后重试。", "construction_busy", 409, retryable=True)
                if directory is not None:
                    yield directory
                    return
            # Keep ordinary optimistic edits serialized (one winner, then a
            # revision conflict), but never wait behind a construction worker.
            time.sleep(.005)

    def _read(self, directory, dataset_id):
        path = directory / "record.json"
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"dataset_id": dataset_id, "status": "not_built", "revision": 0}
        record = json.loads(contents)
        if record.get("dataset_id") != dataset_id:
            _fail("Dataset construction ownership mismatch.", "invalid_identity", 403)
        return record

    def _status(self, record, directory=None):
        graph = record.get("graph")
        algorithm_available = record.get("source_type") in self._algorithms
        status = record["status"]
        message = record.get("message", "")
        # An unavailable result records the registry state at the time of that
        # attempt. Once a builder is installed, expose the retained source as
        # buildable instead of presenting contradictory current capabilities.
        if graph is None and status == "algorithm_unavailable" and algorithm_available:
            status = "not_built"
            message = "构图算法已就绪，源文件已保留，可开始构图。"
        result = {key: copy.deepcopy(value) for key, value in {
            "dataset_id": record["dataset_id"], "status": status, "revision": record["revision"],
            "node_count": len(graph["nodes"]) if graph else 0, "edge_count": len(graph["edges"]) if graph else 0,
            "directed": graph["directed"] if graph else True,
            "source_filename": record.get("source_filename", ""), "source_type": record.get("source_type", ""),
            "algorithm_available": algorithm_available,
            "message": message, "updated_at": record.get("updated_at"),
            "last_build_status": record.get("last_build_status"),
            "build_id": record.get("build_id"), "failure_code": record.get("failure_code"),
        }.items()}
        if directory is not None:
            progress = _read_public_progress(Path(directory), record)
            if progress is not None:
                result["progress"] = progress
        return result

    def status(self, user_id: str, dataset_id: str):
        # Writers publish complete JSON snapshots with os.replace. Readers must
        # not wait on a minutes-long private construction worker: listing datasets
        # is also a main-agent operation and must remain responsive.
        directory = self._directory(user_id, dataset_id)
        record = self._read(directory, dataset_id)
        if record["status"] == "building" and not record.get("executor_instance"):
            # Every live builder holds this complete thread + process lock. Only
            # acquiring it, never elapsed time or a PID guess, proves the prior
            # worker is gone. A contended lock leaves the snapshot read immediate.
            with self._lock(user_id, dataset_id, blocking=False) as claimed:
                if claimed is not None:
                    record = self._read(claimed, dataset_id)
                    if record["status"] == "building":
                        previous_graph = record.get("graph") is not None
                        record.update(status="ready" if previous_graph else "failed", last_build_status="interrupted",
                                      message="上次构图已中断；已有图数据仍可使用。" if previous_graph else "上次构图已中断；源文件已保留，可重新开始构图。",
                                      updated_at=datetime.now(timezone.utc).isoformat())
                        _atomic_json(claimed / "record.json", record)
        return self._status(record, directory)

    def list_ready(self, user_id: str):
        directory = self.root / "users" / _identity(user_id) / "datasets"
        if not directory.exists():
            return []
        result = []
        for child in directory.iterdir():
            if child.is_dir() and (child / "construction" / "record.json").is_file():
                state = self.status(user_id, child.name)
                if state["status"] == "ready":
                    result.append(state)
        return sorted(result, key=lambda state: state.get("updated_at") or "", reverse=True)

    def _register(self, directory, record, source_path, source_filename):
        source = Path(source_path).resolve()
        if not source.is_relative_to(self.root) or not source.is_file():
            _fail("Source must be an existing file inside IGRAPH_DATA_ROOT.", "invalid_source", 403)
        filename = _text(source_filename, "source_filename")
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            _fail("source_filename must be a filename, not a path.", "invalid_source")
        kind = Path(filename).suffix.lower().lstrip(".")
        if kind not in FORMATS:
            _fail("Only SQL, CSV and TXT source files support construction.", "unsupported_source")
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".source-", dir=directory)
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as dst, source.open("rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(block)
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
            checksum = digest.hexdigest()
            if record.get("graph") and (record.get("source_sha256") != checksum or record.get("source_type") != kind):
                _fail("An already constructed dataset cannot be rebound to a different source.", "source_conflict", 409)
            retained = directory / f"source-{checksum}.{kind}"
            os.replace(temporary, retained)
            record.update(source_filename=filename, source_type=kind, source_sha256=checksum,
                          retained_source=retained.name, updated_at=datetime.now(timezone.utc).isoformat())
            _atomic_json(directory / "record.json", record)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return record

    def register_source(self, user_id, dataset_id, source_path, source_filename):
        with self._write_lock(user_id, dataset_id) as directory:
            record = self._register(directory, self._read(directory, dataset_id), source_path, source_filename)
            return self._status(record, directory)

    def reuse_ready(self, user_id, dataset_id, source_path=None, source_filename=None):
        """Fast reuse cannot claim compute capacity or change a running source."""
        if self._read(self._directory(user_id, dataset_id), dataset_id).get("graph") is None:
            return None
        with self._write_lock(user_id, dataset_id) as directory:
            record = self._read(directory, dataset_id)
            if record.get("graph") is None:
                return None
            if source_path is not None:
                record = self._register(directory, record, source_path, source_filename)
            return {"success": True, **self._status(record, directory), "reused": True}

    def finish_interrupted(self, user_id, dataset_id, build_id):
        """Only the coordinator calls this after proving the old worker exited."""
        with self._write_lock(user_id, dataset_id, recovering=True) as directory:
            record = self._read(directory, dataset_id)
            if record.get("build_id") == build_id and record["status"] == "building":
                record.update(status="ready" if record.get("graph") is not None else "failed",
                              last_build_status="interrupted", failure_code="construction_interrupted",
                              message="上次构图已中断；源文件和已有图数据已保留，可重新开始构图。",
                              updated_at=datetime.now(timezone.utc).isoformat())
                _atomic_json(directory / "record.json", record)

    def build(self, user_id, dataset_id, source_path=None, source_filename=None, options=None, *, _execution=None):
        options = {} if options is None else options
        if not isinstance(options, dict):
            _fail("options must be a JSON object.")
        _json_keys(options)
        _json(options)
        lease_fds = []
        with self._lock(user_id, dataset_id, blocking=_execution is None, lease_fds=lease_fds) as directory:
            if directory is None:
                _fail("数据集正在构图或写入，请稍后重试。", "construction_busy", 409, retryable=True)
            record = self._read(directory, dataset_id)
            if _execution is not None and record["status"] == "building" and record.get("executor_instance"):
                _fail("上次构图仍在确认退出状态，请稍后重试。", "construction_busy", 409, retryable=True)
            if source_path is not None:
                record = self._register(directory, record, source_path, source_filename)
            if "retained_source" not in record:
                _fail("Register the dataset source before building.", "source_not_registered", 409)
            # Reusing a ready dataset is intentionally idempotent. Rebuild semantics
            # can be added with their own revision contract when algorithms exist.
            if record.get("graph") is not None:
                return {"success": True, **self._status(record, directory), "reused": True}
            algorithm = self._algorithms.get(record["source_type"])
            if algorithm is None:
                record.update(status="algorithm_unavailable", last_build_status="algorithm_unavailable",
                              message="构图算法尚未接入。源文件已保留，接入算法后可再次构图。")
                _atomic_json(directory / "record.json", record)
                return {"success": False, **self._status(record, directory)}
            record.update(status="building", last_build_status="building",
                          failure_code=None,
                          message="构图子代理正在生成并验证图，请稍后查询状态。",
                          updated_at=datetime.now(timezone.utc).isoformat())
            if _execution is not None:
                record.update(build_id=_execution.build_id, executor_instance=_execution.instance_id,
                              request_fingerprint=_execution.fingerprint)
            _atomic_json(directory / "record.json", record)
            try:
                (directory / "construction-progress.json").unlink(missing_ok=True)
            except OSError:
                # A stale observational file must never block construction.
                pass
            try:
                source = directory / record["retained_source"]
                result = (algorithm(source, copy.deepcopy(options)) if _execution is None else
                          _execution.invoke(algorithm, source, copy.deepcopy(options), lease_fds,
                                            isolated=record["source_type"] in self._process_algorithms))
                graph = validate_graph(result)
            except Exception as exc:
                code = getattr(exc, "code", "construction_failed")
                if code in _TEXT_EXTRACTION_FAILURE_MESSAGES:
                    message = _TEXT_EXTRACTION_FAILURE_MESSAGES[code]
                elif isinstance(code, str) and code.startswith("text_extraction_"):
                    message = "TXT 实体关系抽取未完整成功；未发布半成品图，源文件已保留，请检查文本或模型配置后重试。"
                elif code == "construction_time_budget_exhausted":
                    message = "构图超过时间预算；源文件已保留，可调整预算后重试。"
                else:
                    message = "构图失败；源文件已保留，请检查构图算法后重试。"
                record.update(status="failed", last_build_status="failed", failure_code=code,
                              message=message,
                              updated_at=datetime.now(timezone.utc).isoformat())
                _atomic_json(directory / "record.json", record)
                return {"success": False, **self._status(record, directory)}
            record.update(graph=graph, revision=record["revision"] + 1, status="ready", last_build_status="ready",
                          message="构图完成，结果已保存到数据集。", updated_at=datetime.now(timezone.utc).isoformat())
            _atomic_json(directory / "record.json", record)
            return {"success": True, **self._status(record, directory)}

    def graph(self, user_id, dataset_id):
        directory = self._directory(user_id, dataset_id)
        record = self._read(directory, dataset_id)
        if record.get("graph") is None:
            _fail("This dataset has no constructed graph.", "graph_not_ready", 409, status=record["status"])
        return {"dataset_id": dataset_id, "revision": record["revision"], "graph": copy.deepcopy(record["graph"])}

    def schema(self, user_id, dataset_id):
        """Return a freshly aggregated schema; the persisted instance graph is untouched."""
        directory = self._directory(user_id, dataset_id)
        record = self._read(directory, dataset_id)
        if record.get("graph") is None:
            _fail("This dataset has no constructed graph.", "graph_not_ready", 409, status=record["status"])
        return {"dataset_id": dataset_id, "revision": record["revision"],
                "schema": {**graph_schema(record["graph"]),
                           "source_tables": source_schema_tables(directory, record.get("source_tables"))}}

    def optimize(self, user_id, dataset_id, base_revision, operations, *, commit=False, before_commit=None, receipt=None):
        if type(base_revision) is not int or base_revision < 1:
            _fail("base_revision must be a positive integer.")
        with self._write_lock(user_id, dataset_id) as directory:
            record = self._read(directory, dataset_id)
            if record["revision"] != base_revision:
                _fail("The dataset has changed. Reload it and preview your edits again.", "revision_conflict", 409,
                      revision=record["revision"], current_revision=record["revision"])
            if record.get("graph") is None:
                _fail("This dataset has no constructed graph.", "graph_not_ready", 409)
            graph, summary = optimize_graph(record["graph"], operations)
            result = {"success": True, "dataset_id": dataset_id, "status": "ready" if commit else "preview",
                      "base_revision": base_revision, "revision": base_revision + 1, "graph": graph, "summary": summary}
            if commit:
                record.update(graph=graph, revision=base_revision + 1, updated_at=datetime.now(timezone.utc).isoformat())
                if receipt is not None:
                    record["commit_receipt"] = {**receipt, "result": result}
                _atomic_json(directory / "record.json", record, before_replace=before_commit)
            return result

    def delete(self, user_id, dataset_id):
        with self._write_lock(user_id, dataset_id) as directory:
            existed = directory.exists()
            if existed:
                shutil.rmtree(directory)
            return {"success": True, "dataset_id": dataset_id, "deleted": existed}


def to_graph_data(document: dict):
    """Retain the property document in durable GraphData metadata and stable names.

The numeric topology is available to existing compute/ML tools, while metadata
survives NPZ/JSON snapshots and exposes exact node/edge properties and edge ids.
"""
    import numpy as np
    from graph_data_service.graph_data import GraphData

    graph = validate_graph(document["graph"])
    names = {index: node["id"] for index, node in enumerate(graph["nodes"])}
    indices = {name: index for index, name in names.items()}
    edges = graph["edges"]
    edge_index = np.array([[indices[edge["source"]] for edge in edges],
                           [indices[edge["target"]] for edge in edges]], dtype=np.int64)
    return GraphData(num_nodes=len(names), edge_index=edge_index, id2node=names, node2id=indices,
                     edge_names=[edge["id"] for edge in edges], directed=graph["directed"],
                     semantic_type="directed" if graph["directed"] else "undirected",
                     node_type=[node["type"] for node in graph["nodes"]], edge_type=[edge["type"] for edge in edges],
                     name=document["dataset_id"], graph_metadata={
                         "construction_dataset_id": document["dataset_id"], "construction_revision": document["revision"],
                         "construction_graph": graph})
