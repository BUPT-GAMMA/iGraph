"""Constrained table/schema transformations for the private construction agent.

Transforms return a fresh graph. The worker, not the LLM, decides whether the
candidate passes source fidelity, the frozen workload and the cost objective.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from collections import defaultdict

from graph_data_service.construction import optimize_graph, validate_graph
from graph_engine.construction.sources import foreign_key_pairs, source_integrity_errors


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(prefix, *values):
    return prefix + hashlib.sha256(_key(values).encode()).hexdigest()[:32]


def graph_schema(graph):
    nodes, edges = {}, {}
    for node in graph["nodes"]:
        row = nodes.setdefault(node["type"], {"count": 0, "properties": set()})
        row["count"] += 1
        row["properties"].update(node["properties"].get("data", node["properties"]))
    by_id = {node["id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        row = edges.setdefault(edge["type"], {"count": 0, "endpoints": set()})
        row["count"] += 1
        row["endpoints"].add((by_id[edge["source"]]["type"], by_id[edge["target"]]["type"]))
    return {"node_types": {k: {**v, "properties": sorted(v["properties"])} for k, v in nodes.items()},
            "edge_types": {k: {**v, "endpoints": sorted(v["endpoints"])} for k, v in edges.items()}}


def _rows(graph, table):
    return [n for n in graph["nodes"] if n["properties"].get("table") == table and "data" in n["properties"]]


def fidelity_errors(source, graph):
    """Check every source record, including records not selected by the workload.

This does not supply graph query answers. It prevents optimizing away data just
because the finite acceptance workload happens not to read it.
"""
    return source_integrity_errors(source, graph)


def _check_fields(args, allowed, required=()):
    if not isinstance(args, dict) or set(args) - set(allowed) or set(required) - set(args):
        raise ValueError("Invalid tool parameters; required: " + ", ".join(required))


def _column(source, table, columns):
    if table not in source.tables or not isinstance(columns, list) or not columns:
        raise ValueError("Unknown table or empty columns")
    if len(set(columns)) != len(columns) or any(c not in source.tables[table].columns for c in columns):
        raise ValueError("Unknown or repeated source columns")


def _without_shortcuts(graph):
    result = copy.deepcopy(graph)
    result["edges"] = [e for e in result["edges"] if "materialized_join" not in e["properties"]]
    return result


def materialize_join(graph, source, args, *, deadline=None):
    _check_fields(args, ("path", "edge_type"), ("path",))
    path = args["path"]
    if not isinstance(path, list) or not 2 <= len(path) <= 3:
        raise ValueError("materialize_join requires a two- or three-step FK path")
    maps = []
    previous = None
    for step in path:
        _check_fields(step, ("table", "columns", "ref_table", "ref_columns", "direction"),
                      ("table", "columns", "ref_table", "ref_columns", "direction"))
        fk = {k: v for k, v in step.items() if k != "direction"}
        _column(source, step["table"], step["columns"])
        _column(source, step["ref_table"], step["ref_columns"])
        if step["direction"] not in ("forward", "reverse"):
            raise ValueError("Unknown path direction")
        reverse = step["direction"] == "reverse"
        start, end = (step["ref_table"], step["table"]) if reverse else (step["table"], step["ref_table"])
        if previous is not None and start != previous:
            raise ValueError("FK path is not contiguous")
        previous = end
        adjacency = defaultdict(list)
        for edge in graph["edges"]:
            if edge["properties"].get("foreign_key") == fk:
                a, b = (edge["target"], edge["source"]) if reverse else (edge["source"], edge["target"])
                adjacency[a].append(b)
        maps.append(adjacency)
    result = copy.deepcopy(graph)
    result["edges"] = [e for e in result["edges"] if e["properties"].get("materialized_join", {}).get("path") != path]
    edge_type = args.get("edge_type") or "materialized_" + hashlib.sha256(_key(path).encode()).hexdigest()[:12]
    for start in maps[0]:
        _budget(deadline)
        paths = [[start]]
        for adjacency in maps:
            expanded = []
            for nodes in paths:
                _budget(deadline)
                for target in adjacency.get(nodes[-1], ()):
                    if len(expanded) + len(result["edges"]) >= 200_000:
                        raise ValueError("Materialization exceeds intermediate path budget")
                    expanded.append((*nodes, target))
            paths = expanded
        for nodes in paths:
            result["edges"].append({"id": _id("join_", path, nodes), "source": start, "target": nodes[-1],
                                    "type": edge_type, "properties": {"materialized_join": {"path": copy.deepcopy(path), "node_ids": list(nodes)}}})
    return validate_graph(result)


def _budget(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ValueError("Construction transformation deadline exceeded")


def _business_operations(graph, source, operations, deadline=None):
    """Adapt GalaxyWeaver rewrites to current row/attribute graph entities.

    No provenance snapshot is used for answering. A collapsed row is represented
    by the resulting edge's own data and two actual endpoints; extracted values
    live in current attribute entities or incident edges.
    """
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ValueError("Provide 1..100 graph operations")
    result = graph
    for operation in operations:
        _budget(deadline)
        if not isinstance(operation, dict):
            raise ValueError("Operation must be an object")
        kind = operation.get("op")
        by_id = {n["id"]: n for n in result["nodes"]}
        node = by_id.get(operation.get("node_id", ""))
        prop = operation.get("property")
        if kind == "collapse_node" and node and node["properties"].get("table") in source.tables:
            _check_fields(operation, ("op", "node_id", "edge_type"), ("op", "node_id", "edge_type"))
            table = node["properties"]["table"]
            if any(f.ref_table == table for t in source.tables.values() for f in t.foreign_keys):
                raise ValueError("Cannot turn a referenced row into an edge: incoming FK identities must remain nodes")
            incident = [e for e in result["edges"] if node["id"] in (e["source"], e["target"])]
            if len(incident) != 2 or any(e["properties"].get("foreign_key", {}).get("table") != table for e in incident):
                raise ValueError("A bridge row must have exactly two current FK relationships and no other incident edges")
            endpoints = []
            for edge in incident:
                other = edge["target"] if edge["source"] == node["id"] else edge["source"]
                if other == node["id"]:
                    raise ValueError("A row cannot collapse onto itself")
                endpoints.append({"foreign_key": copy.deepcopy(edge["properties"]["foreign_key"]), "node_id": other})
            new_edge = {"id": _id("row_edge_", node["id"]), "source": endpoints[0]["node_id"], "target": endpoints[1]["node_id"],
                        "type": operation["edge_type"], "properties": {**copy.deepcopy(node["properties"]), "row_endpoints": endpoints}}
            removed = {e["id"] for e in incident}
            result["nodes"] = [n for n in result["nodes"] if n["id"] != node["id"]]
            result["edges"] = [e for e in result["edges"] if e["id"] not in removed] + [new_edge]
        elif kind == "extract_property_node" and prop not in {"data", "table", "row_index", "relational_schema"}:
            _check_fields(operation, ("op", "node_ids", "property", "node_type", "edge_type", "remove_from_node"),
                          ("op", "node_ids", "property", "node_type", "edge_type"))
            ids = operation["node_ids"]
            if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
                raise ValueError("node_ids must be a nonempty unique list")
            remove = operation.get("remove_from_node", True)
            if type(remove) is not bool:
                raise ValueError("remove_from_node must be boolean")
            for identity in ids:
                _budget(deadline)
                selected = by_id.get(identity)
                if not selected or prop not in selected["properties"].get("data", {}):
                    raise ValueError("Unknown business attribute on the selected row")
                table = selected["properties"]["table"]
                value = selected["properties"]["data"][prop]
                attribute_id = _id("attribute_", operation["node_type"], prop, value)
                attribute = {"id": attribute_id, "type": operation["node_type"], "properties": {"value": copy.deepcopy(value)}}
                if attribute_id in by_id and by_id[attribute_id] != attribute:
                    raise ValueError("Conflicting attribute entity")
                if attribute_id not in by_id:
                    if len(result["nodes"]) >= 200000:
                        raise ValueError("Attribute entity budget exceeded")
                    by_id[attribute_id] = attribute
                    result["nodes"].append(attribute)
                if len(result["edges"]) >= 1000000:
                    raise ValueError("Attribute edge budget exceeded")
                result["edges"].append({"id": _id("attribute_link_", identity, prop, attribute_id), "type": operation["edge_type"],
                    "source": identity, "target": attribute_id, "properties": {"extracted_attribute": {"table": table, "column": prop}}})
                if remove:
                    del selected["properties"]["data"][prop]
        elif kind == "node_property_to_edge" and node and prop in node["properties"].get("data", {}):
            _check_fields(operation, ("op", "node_id", "property", "edge_ids", "target_property", "remove_from_node"),
                          ("op", "node_id", "property", "edge_ids"))
            remove = operation.get("remove_from_node", True)
            if type(remove) is not bool or not isinstance(operation["edge_ids"], list) or not operation["edge_ids"]:
                raise ValueError("Invalid property relocation arguments")
            edges = {e["id"]: e for e in result["edges"]}
            value = node["properties"]["data"][prop]
            for edge_id in operation["edge_ids"]:
                edge = edges.get(edge_id)
                if not edge or node["id"] not in (edge["source"], edge["target"]):
                    raise ValueError("Property relocation requires a current incident edge")
                records = edge["properties"].setdefault("relocated_attributes", [])
                records.append({"row_id": node["id"], "column": prop, "value": copy.deepcopy(value),
                                "name": operation.get("target_property", prop)})
            if remove:
                del node["properties"]["data"][prop]
        else:
            result = optimize_graph(result, [operation])[0]
    return validate_graph(result)


def transform(graph, source, name, args, *, deadline=None):
    _budget(deadline)
    if name == "materialize_join":
        return materialize_join(graph, source, args, deadline=deadline)
    result = _without_shortcuts(graph)
    if name == "optimize_graph":
        _check_fields(args, ("operations",), ("operations",))
        return _business_operations(result, source, args["operations"], deadline)
    if name == "connect_columns":
        _check_fields(args, ("table", "columns", "ref_table", "ref_columns", "edge_type"),
                      ("table", "columns", "ref_table", "ref_columns"))
        _column(source, args["table"], args["columns"])
        _column(source, args["ref_table"], args["ref_columns"])
        if len(args["columns"]) != len(args["ref_columns"]):
            raise ValueError("Join key arities differ")
        lookup = defaultdict(list)
        for row in _rows(result, args["ref_table"]):
            value = [row["properties"]["data"][c] for c in args["ref_columns"]]
            if None not in value:
                lookup[_key(value)].append(row["id"])
        fk = {k: args[k] for k in ("table", "columns", "ref_table", "ref_columns")}
        declared = any(f.columns == args["columns"] and f.ref_table == args["ref_table"] and f.ref_columns == args["ref_columns"]
                       for f in source.tables[args["table"]].foreign_keys)
        existing = {(e["source"], e["target"], _key(e["properties"].get("foreign_key"))) for e in result["edges"]}
        matched_fk = next((f for f in source.tables[args["table"]].foreign_keys
                           if f.columns == args["columns"] and f.ref_table == args["ref_table"] and f.ref_columns == args["ref_columns"]), None)
        if matched_fk is not None:
            child_rows = {n["properties"]["row_index"]: n for n in _rows(result, args["table"])}
            parent_rows = {n["properties"]["row_index"]: n for n in _rows(result, args["ref_table"])}
            for child_index, parent_index in foreign_key_pairs(source, source.tables[args["table"]], matched_fk):
                _budget(deadline)
                if child_index not in child_rows or parent_index not in parent_rows:
                    continue
                child, parent = child_rows[child_index]["id"], parent_rows[parent_index]["id"]
                if (child, parent, _key(fk)) in existing:
                    continue
                result["edges"].append({"id": _id("column_", child, parent, fk), "source": child, "target": parent,
                    "type": args.get("edge_type") or "column_connection", "properties": {"foreign_key": fk}})
            return validate_graph(result)
        for row in _rows(result, args["table"]):
            _budget(deadline)
            values = [row["properties"]["data"][c] for c in args["columns"]]
            for target in lookup.get(_key(values), ()) if None not in values else ():
                if declared and (row["id"], target, _key(fk)) in existing:
                    continue
                result["edges"].append({"id": _id("column_", row["id"], target, fk), "source": row["id"], "target": target,
                    "type": args.get("edge_type") or "column_connection", "properties": {"foreign_key" if declared else "column_join": fk}})
                if len(result["edges"]) > 1_000_000:
                    raise ValueError("Column join exceeds graph edge budget")
    elif name in ("generate_entity_table", "unfold_multi_category"):
        _check_fields(args, ("table", "column", "node_type", "edge_type", "delimiter"), ("table", "column", "node_type"))
        _column(source, args["table"], [args["column"]])
        if args["node_type"] in source.tables:
            raise ValueError("Derived entity type must not shadow a source table")
        delimiter = args.get("delimiter", ",")
        if not isinstance(delimiter, str) or not delimiter:
            raise ValueError("delimiter must be nonempty")
        node_ids = {n["id"] for n in result["nodes"]}
        edge_ids = {e["id"] for e in result["edges"]}
        for row in _rows(result, args["table"]):
            _budget(deadline)
            value = row["properties"]["data"][args["column"]]
            if value is None:
                continue
            if name == "unfold_multi_category" and isinstance(value, str) and value.count(delimiter) > 10000:
                raise ValueError("A multi-category cell exceeds the 10000-item limit")
            values = value if name == "unfold_multi_category" and isinstance(value, list) else (
                [v.strip() for v in value.split(delimiter) if v.strip()] if name == "unfold_multi_category" and isinstance(value, str) else [value])
            for position, item in enumerate(values):
                _budget(deadline)
                node_id = _id("entity_", args["node_type"], args["column"], item)
                if node_id not in node_ids:
                    if len(result["nodes"]) >= 200000:
                        raise ValueError("Entity node budget exceeded")
                    result["nodes"].append({"id": node_id, "type": args["node_type"], "properties": {"value": item}})
                    node_ids.add(node_id)
                edge_id = _id("entity_edge_", row["id"], node_id, args["column"], position)
                if edge_id not in edge_ids:
                    if len(result["edges"]) >= 1000000:
                        raise ValueError("Entity edge budget exceeded")
                    result["edges"].append({"id": edge_id, "source": row["id"], "target": node_id,
                        "type": args.get("edge_type") or "has_" + args["column"], "properties": {"column": args["column"], "position": position}})
                    edge_ids.add(edge_id)
    elif name in ("add_primary_key", "remove_primary_key"):
        _check_fields(args, ("table", "columns"), ("table", "columns") if name == "add_primary_key" else ("table",))
        if args["table"] not in source.tables:
            raise ValueError("Unknown table")
        rows = _rows(result, args["table"])
        if name == "add_primary_key":
            _column(source, args["table"], args["columns"])
            values = [[r["properties"]["data"][c] for c in args["columns"]] for r in rows]
            if any(None in v for v in values) or len({_key(v) for v in values}) != len(values):
                raise ValueError("Proposed primary key is null or not unique")
            for row in rows:
                row["properties"]["identity_key"] = list(args["columns"])
        else:
            for row in rows:
                row["properties"].pop("identity_key", None)
    else:
        raise ValueError("Unknown construction transformation")
    return validate_graph(result)


TRANSFORM_NAMES = frozenset(("optimize_graph", "connect_columns", "generate_entity_table", "unfold_multi_category",
                             "add_primary_key", "remove_primary_key", "materialize_join"))


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}}


_STR = {"type": "string"}
_COLS = {"type": "array", "items": _STR, "minItems": 1}
_FK_PROPS = {"table": _STR, "columns": _COLS, "ref_table": _STR, "ref_columns": _COLS}


def tool_specs(stage="evolve"):
    specs = [
        _tool("inspect_schema", "Inspect current graph schema, real graph IDs and source table samples. Read only.", {}),
    ]
    if stage == "workload":
        return specs + [_tool("set_workload", "Freeze exactly query_count diverse production questions and executable query plans. Do not supply expected answers. Use the documented DSL and actual source columns/values.",
                             {"queries": {"type": "array", "items": {"type": "object"}}}, ("queries",))]
    from graph_engine.construction_catalog import _OPERATIONS
    specs.extend([
        _tool("evaluate_queries", "Run the frozen queries against the current graph and independent source SQL oracle; report failures and timings.", {}),
        _tool("optimize_graph", "GalaxyWeaver: collapse_node, node_property_to_edge, extract_property_node, delete, reverse_edges, rename_type. Business property names refer to fields inside data (e.g. status). A bridge row with exactly two FK relationships and no incoming references can become a current edge entity; its row values remain queryable on that edge. Extracted values are queried from current attribute entities; relocated values from current incident edges. Unsupported/lossy rewrites are rejected. Compose operations in one array.", {"operations": _OPERATIONS}, ("operations",)),
        _tool("connect_columns", "AutoG: connect PK-FK or FK-FK values by column equality. Restores missing declared FK edges, or adds a derived relation without changing source data.", {**_FK_PROPS, "edge_type": _STR}, tuple(_FK_PROPS)),
        _tool("generate_entity_table", "AutoG generate_new_table / GalaxyWeaver attribute extraction: make distinct property values into shared entity nodes, retaining source values.", {"table": _STR, "column": _STR, "node_type": _STR, "edge_type": _STR}, ("table", "column", "node_type")),
        _tool("unfold_multi_category", "AutoG: unfold delimited/list categories into entity nodes and position-preserving relationships without rewriting original cells.", {"table": _STR, "column": _STR, "node_type": _STR, "edge_type": _STR, "delimiter": _STR}, ("table", "column", "node_type")),
        _tool("add_primary_key", "Validate a non-null unique candidate key and record a graph identity constraint. Stable row IDs and source schema are preserved.", {"table": _STR, "columns": _COLS}, ("table", "columns")),
        _tool("remove_primary_key", "Remove only a proposed graph identity constraint; source PK and stable graph IDs remain intact.", {"table": _STR}, ("table",)),
        _tool("materialize_join", "Materialize a two/three-hop FK path as graph shortcut edges, keeping intermediate row IDs and multiplicity for real graph query execution. Candidate accepted only after query validation and measured cost improvement.",
              {"path": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "object", "properties": {**_FK_PROPS, "direction": {"type": "string", "enum": ["forward", "reverse"]}}, "required": [*_FK_PROPS, "direction"], "additionalProperties": False}}, "edge_type": _STR}, ("path",)),
        _tool("finish", "Request finalization. The worker independently reruns every frozen query and refuses publication unless all pass.", {"reason": _STR}, ("reason",)),
    ])
    return specs
