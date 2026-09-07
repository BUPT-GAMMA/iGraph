"""Durable property graphs and explicit, transactional graph optimizations.

SQL, CSV and TXT intentionally have no built-in construction algorithm. Integrators
register a callable with ``register_algorithm(format, builder)``. The callable
receives the retained source Path and JSON options and returns the graph document
specified below. Its output passes the same validation as every optimization.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import graph_engine.paths as paths

FORMATS = ("sql", "csv", "txt")
OPERATIONS = ("collapse_node", "node_property_to_edge", "extract_property_node",
              "delete", "reverse_edges", "rename_type")
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


def validate_graph(graph: dict) -> dict:
    """Copy and validate a complete property graph; never coerce identifiers."""
    if not isinstance(graph, dict) or set(graph) != {"directed", "nodes", "edges"}:
        _fail("Graph requires exactly directed, nodes and edges.", "invalid_graph")
    if not isinstance(graph["directed"], bool):
        _fail("Graph directed must be a boolean.", "invalid_graph")
    if not isinstance(graph["nodes"], list) or not isinstance(graph["edges"], list):
        _fail("Graph nodes and edges must be arrays.", "invalid_graph")
    if len(graph["nodes"]) > 200_000 or len(graph["edges"]) > 1_000_000:
        _fail("Graph exceeds the construction document size limit.", "graph_too_large", 413)
    node_ids, edge_ids = set(), set()
    result = copy.deepcopy(graph)
    for target, identities, fields in (("nodes", node_ids, {"id", "type", "properties"}),
                                        ("edges", edge_ids, {"id", "type", "properties", "source", "target"})):
        for row in result[target]:
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
                _text(row["source"], "source")
                _text(row["target"], "target")
                if row["source"] not in node_ids or row["target"] not in node_ids:
                    _fail(f"Edge {identity} has a missing endpoint.", "invalid_graph")
    return result


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


def optimize_graph(graph: dict, operations: list[dict]) -> tuple[dict, dict]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        _fail("Provide between 1 and 100 operations.")
    original = validate_graph(graph)
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
                if prop not in node["properties"]:
                    _fail(f"Node {node['id']} has no property {prop}.")
                selected = [_get(edges, identity, "edge") for identity in _ids(operation.get("edge_ids"), "edge_ids")]
                for edge in selected:
                    if node["id"] not in (edge["source"], edge["target"]):
                        _fail("The selected edges must be incident to the selected node.")
                    if destination in edge["properties"] and _json(edge["properties"][destination]) != _json(node["properties"][prop]):
                        _fail(f"Edge {edge['id']} already has a different value for {destination}.", "property_conflict")
                    edge["properties"][destination] = copy.deepcopy(node["properties"][prop])
                if _boolean(operation, "remove_from_node", True):
                    del node["properties"][prop]
            elif kind == "extract_property_node":
                selected = [_get(nodes, identity, "node") for identity in _ids(operation.get("node_ids"), "node_ids")]
                prop = _text(operation.get("property"), "property")
                node_type, edge_type = _text(operation.get("node_type"), "node_type"), _text(operation.get("edge_type"), "edge_type")
                remove = _boolean(operation, "remove_from_node", True)
                for node in selected:
                    if prop not in node["properties"]:
                        _fail(f"Node {node['id']} has no property {prop}.")
                    value = node["properties"][prop]
                    new_id = _generated_id("property_", node_type, prop, value)
                    if new_id == node["id"]:
                        _fail("A property node cannot be extracted into itself.")
                    new_node = {"id": new_id, "type": node_type, "properties": {prop: copy.deepcopy(value)}}
                    if new_id in nodes and nodes[new_id] != new_node:
                        _fail("Generated property node conflicts with an existing node.", "property_conflict")
                    if new_id not in nodes:
                        candidate["nodes"].append(new_node)
                        nodes[new_id] = new_node
                    edge_id = _generated_id("property_edge_", node["id"], new_id, edge_type)
                    new_edge = {"id": edge_id, "source": node["id"], "target": new_id, "type": edge_type, "properties": {}}
                    if edge_id in edges and edges[edge_id] != new_edge:
                        _fail("Generated property edge conflicts with an existing edge.")
                    if edge_id not in edges:
                        candidate["edges"].append(new_edge)
                        edges[edge_id] = new_edge
                    if remove:
                        del node["properties"][prop]
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
                        if prop not in row["properties"]:
                            _fail(f"{target} {row['id']} has no property {prop}.")
                        del row["properties"][prop]
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
            validate_graph(candidate)
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
            stream.write(_json(value))
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


class ConstructionStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).resolve() if root else paths.data_root()
        self._algorithms: dict[str, Callable[[Path, dict], dict]] = {}

    def register_algorithm(self, source_type: str, algorithm: Callable[[Path, dict], dict]):
        if source_type not in FORMATS or not callable(algorithm):
            raise ValueError("Register a callable for sql, csv or txt.")
        self._algorithms[source_type] = algorithm

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
    def _lock(self, user_id, dataset_id):
        directory = self._directory(user_id, dataset_id)
        key = str(directory)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        with lock:
            lock_dir = self.root / ".construction_locks"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_path = lock_dir / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
            with lock_path.open("a+b") as handle:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    import msvcrt
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield directory
                finally:
                    if os.name == "posix":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    else:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read(self, directory, dataset_id):
        path = directory / "record.json"
        if not path.exists():
            return {"dataset_id": dataset_id, "status": "not_built", "revision": 0}
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("dataset_id") != dataset_id:
            _fail("Dataset construction ownership mismatch.", "invalid_identity", 403)
        return record

    def _status(self, record):
        graph = record.get("graph")
        return {key: copy.deepcopy(value) for key, value in {
            "dataset_id": record["dataset_id"], "status": record["status"], "revision": record["revision"],
            "node_count": len(graph["nodes"]) if graph else 0, "edge_count": len(graph["edges"]) if graph else 0,
            "directed": graph["directed"] if graph else True,
            "source_filename": record.get("source_filename", ""), "source_type": record.get("source_type", ""),
            "algorithm_available": record.get("source_type") in self._algorithms,
            "message": record.get("message", ""), "updated_at": record.get("updated_at"),
            "last_build_status": record.get("last_build_status"),
        }.items()}

    def status(self, user_id: str, dataset_id: str):
        with self._lock(user_id, dataset_id) as directory:
            return self._status(self._read(directory, dataset_id))

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
        with self._lock(user_id, dataset_id) as directory:
            record = self._register(directory, self._read(directory, dataset_id), source_path, source_filename)
            return self._status(record)

    def build(self, user_id, dataset_id, source_path=None, source_filename=None, options=None):
        options = {} if options is None else options
        if not isinstance(options, dict):
            _fail("options must be a JSON object.")
        _json_keys(options)
        _json(options)
        with self._lock(user_id, dataset_id) as directory:
            record = self._read(directory, dataset_id)
            if source_path is not None:
                record = self._register(directory, record, source_path, source_filename)
            if "retained_source" not in record:
                _fail("Register the dataset source before building.", "source_not_registered", 409)
            # Reusing a ready dataset is intentionally idempotent. Rebuild semantics
            # can be added with their own revision contract when algorithms exist.
            if record.get("graph") is not None:
                return {"success": True, **self._status(record), "reused": True}
            algorithm = self._algorithms.get(record["source_type"])
            if algorithm is None:
                record.update(status="algorithm_unavailable", last_build_status="algorithm_unavailable",
                              message="构图算法尚未接入。源文件已保留，接入算法后可再次构图。")
                _atomic_json(directory / "record.json", record)
                return {"success": False, **self._status(record)}
            try:
                graph = validate_graph(algorithm(directory / record["retained_source"], copy.deepcopy(options)))
            except Exception:
                record.update(status="failed", last_build_status="failed", message="构图失败；源文件已保留，请检查构图算法后重试。")
                _atomic_json(directory / "record.json", record)
                return {"success": False, **self._status(record)}
            record.update(graph=graph, revision=record["revision"] + 1, status="ready", last_build_status="ready",
                          message="构图完成，结果已保存到数据集。", updated_at=datetime.now(timezone.utc).isoformat())
            _atomic_json(directory / "record.json", record)
            return {"success": True, **self._status(record)}

    def graph(self, user_id, dataset_id):
        with self._lock(user_id, dataset_id) as directory:
            record = self._read(directory, dataset_id)
            if record.get("graph") is None:
                _fail("This dataset has no constructed graph.", "graph_not_ready", 409, status=record["status"])
            return {"dataset_id": dataset_id, "revision": record["revision"], "graph": copy.deepcopy(record["graph"])}

    def optimize(self, user_id, dataset_id, base_revision, operations, *, commit=False, before_commit=None, receipt=None):
        if type(base_revision) is not int or base_revision < 1:
            _fail("base_revision must be a positive integer.")
        with self._lock(user_id, dataset_id) as directory:
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
        with self._lock(user_id, dataset_id) as directory:
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
