"""Read a saved single graph without creating a construction draft or an analysis session."""
from __future__ import annotations

import json
import math
import os
import pickle
import pickletools
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

import numpy as np

from graph_data_service.construction import ConstructionError, graph_schema

_MAX_BYTES = 192 * 1024 * 1024
_MAX_NODES, _MAX_EDGES = 200_000, 500_000
_SAMPLE_NODES, _SAMPLE_EDGES = 64, 96
_preview_lock = threading.Lock()


def _too_large():
    raise ConstructionError("saved_graph_too_large", "This graph exceeds the interactive preview limit.", 413)


def _array(archive, field, *, metadata=False):
    """Read only bounded primitive arrays. Older iGraph metadata contains one JSON string."""
    with archive.open(field + ".npy") as stream:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Unsupported NPY version")
        if dtype.hasobject:
            if not metadata:
                raise ValueError("Object arrays are not graph topology")
            if shape != () or dtype != np.dtype(object):
                raise ValueError("Invalid graph metadata")
            # Older releases wrote the scalar with dtype=object. Inspect literal
            # strings only. No pickle instruction is ever executed.
            for op, value, _ in pickletools.genops(stream):
                if op.name in {"BINUNICODE", "SHORT_BINUNICODE", "BINUNICODE8", "UNICODE"} and value.startswith("{"):
                    return json.loads(value)
            raise ValueError("Missing graph metadata JSON")
        size = math.prod(shape) * dtype.itemsize
        if size > _MAX_BYTES:
            _too_large()
        if size != archive.getinfo(field + ".npy").file_size - stream.tell():
            raise ValueError("Inconsistent array shape")
        stream.seek(0)
        value = np.lib.format.read_array(stream, allow_pickle=False)
        return json.loads(str(value.item())) if metadata else value


def _check_size(nodes, edges):
    if nodes < 0:
        raise ValueError("Negative node count")
    if nodes > _MAX_NODES or edges > _MAX_EDGES:
        _too_large()


def _topology(nodes, index, directed, node_types, edge_types, names, weights):
    if index.ndim != 2 or index.shape[0] != 2 or index.dtype.kind not in "iu":
        raise ValueError("Invalid edge index")
    edges = index.shape[1]
    _check_size(nodes, edges)
    if index.size and (int(index.min()) < 0 or int(index.max()) >= nodes):
        raise ValueError("Invalid edge endpoint")
    for field, count in ((node_types, nodes), (edge_types, edges), (weights, edges)):
        if field is not None and np.shape(field) != (count,):
            raise ValueError("Field length does not match graph")
    return {"directed": bool(directed), "nodes": [
        {"id": str(i), "type": str(node_types[i]) if node_types is not None else "Node",
         "properties": {"name": str(names[i])} if i in names else {}} for i in range(nodes)
    ], "edges": [
        {"id": str(i), "type": str(edge_types[i]) if edge_types is not None else "Edge",
         "source": str(int(index[0, i])), "target": str(int(index[1, i])),
         "properties": {"weight": float(weights[i])} if weights is not None else {}} for i in range(edges)
    ]}


def _npz_document(archive):
    files = set(archive.namelist())

    def optional(field):
        return _array(archive, field) if field + ".npy" in files else None

    count = int(_array(archive, "_num_nodes").item())
    index = _array(archive, "edge_index")
    if index.ndim != 2 or index.shape[0] != 2:
        raise ValueError("Invalid edge index")
    _check_size(count, index.shape[1])
    if "_graph_metadata_json.npy" in files:
        metadata = _array(archive, "_graph_metadata_json", metadata=True)
        if isinstance(metadata, dict) and "construction_graph" in metadata:
            document = metadata["construction_graph"]
            if len(document["nodes"]) != count or len(document["edges"]) != index.shape[1]:
                raise ValueError("Stored construction counts disagree with topology")
            return document
    names = {}
    if {"map_ids.npy", "map_names.npy"} <= files:
        ids, labels = _array(archive, "map_ids"), _array(archive, "map_names")
        if ids.ndim != 1 or labels.shape != ids.shape:
            raise ValueError("Invalid node names")
        names = dict(zip(ids.tolist(), labels.tolist()))
    return _topology(count, index, _array(archive, "_directed").item(),
                     optional("node_type"), optional("edge_type"), names, optional("edge_weight"))


def _sample(graph):
    nodes, edges = graph["nodes"], graph["edges"]
    types = {node["id"]: node["type"] for node in nodes}
    chosen_nodes, chosen_edges = set(), {}

    def add_edge(edge):
        endpoints = {edge["source"], edge["target"]}
        if len(chosen_edges) < _SAMPLE_EDGES and len(chosen_nodes) + len(endpoints - chosen_nodes) <= _SAMPLE_NODES:
            chosen_nodes.update(endpoints)
            chosen_edges[edge["id"]] = edge

    # Cover different relationships before the first dense part fills the canvas.
    signatures = set()
    for edge in edges:
        signature = (types[edge["source"]], edge["type"], types[edge["target"]])
        if signature not in signatures:
            signatures.add(signature)
            add_edge(edge)
    seen_types = {types[key] for key in chosen_nodes}
    for node in nodes:
        if node["type"] not in seen_types and len(chosen_nodes) < _SAMPLE_NODES:
            chosen_nodes.add(node["id"])
            seen_types.add(node["type"])
    for edge in edges:
        if len(chosen_edges) == _SAMPLE_EDGES:
            break
        add_edge(edge)
    for node in nodes:
        if len(chosen_nodes) == _SAMPLE_NODES:
            break
        chosen_nodes.add(node["id"])
    return {"visualization_kind": "instances", "directed": graph["directed"],
            "nodes": [node for node in nodes if node["id"] in chosen_nodes],
            "edges": list(chosen_edges.values()), "num_nodes": len(chosen_nodes), "num_edges": len(chosen_edges),
            "total_nodes": len(nodes), "total_edges": len(edges),
            "truncated": len(chosen_nodes) < len(nodes) or len(chosen_edges) < len(edges)}


def _fingerprint(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


@lru_cache(maxsize=4)
def _read(path, fingerprint):
    if fingerprint[2] > _MAX_BYTES:
        _too_large()
    with path.open("rb") as source:
        if _fingerprint(os.fstat(source.fileno())) != fingerprint:
            raise ConstructionError("saved_graph_changed", "The graph file changed. Please reload.", 409)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > 4096 or sum(member.file_size for member in members) > _MAX_BYTES:
                _too_large()
            if path.suffix.lower() == ".npz":
                document = _npz_document(archive)
            elif path.suffix.lower() == ".pt":
                from graph_data_service.graph_data import GraphData
                from graph_data_service.io import load_graph_data_pt
                source.seek(0)
                graph = load_graph_data_pt(source, GraphData, preview_limits=(_MAX_NODES, _MAX_EDGES))
                _check_size(graph.num_nodes, graph.edge_index.shape[1])
                document = (graph.graph_metadata or {}).get("construction_graph")
                if document is None:
                    document = _topology(graph.num_nodes, graph.edge_index, graph.directed, graph.node_type,
                                         graph.edge_type, graph.id2node or {}, graph.edge_weight)
            else:
                raise ValueError("Unsupported graph format")
    schema = graph_schema(document)
    sample = _sample(document)
    for node in sample["nodes"]:
        name = node["properties"].get("name")
        if isinstance(name, str) and name:
            node["label"] = name
    return {"schema": schema, "graph": sample}


def saved_graph_preview(path: Path):
    try:
        # Concurrent clicks share the small, read-only result, not a whole loaded graph.
        with _preview_lock:
            return _read(path, _fingerprint(path.stat()))
    except ConstructionError:
        raise
    except FileNotFoundError:
        raise ConstructionError("saved_graph_missing", "The saved graph file is missing.", 404) from None
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, EOFError, OSError,
            OverflowError, RuntimeError, ImportError, zipfile.BadZipFile, pickle.UnpicklingError):
        raise ConstructionError("saved_graph_unreadable", "This file cannot be previewed as a single graph.") from None
