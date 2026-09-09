"""Resolve logical source rows from their *current* graph representation.

This module has no source-table argument and reads no historical snapshots.
Rows can live on graph nodes or on bridge edges. Extracted and relocated cells
are obtained from the live entity node or live incident edge respectively.
"""
from __future__ import annotations

import copy
import json


class RepresentationError(ValueError):
    """Current graph elements do not identify an unambiguous logical row."""


def _value_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def resolve_rows(graph: dict) -> list[dict]:
    """Return detached current row-node/row-edge objects with resolved data.

    Returned ``properties.data`` merges cells from live graph storage. Row edges
    retain ``source``/``target`` and ``properties.row_endpoints``. Non-row entities
    are omitted. Malformed, ambiguous, conflicting or detached storage fails
    explicitly instead of being repaired from provenance or source tables.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    if len(nodes) != len(graph["nodes"]):
        raise RepresentationError("Duplicate current node identifiers.")
    edge_ids = {edge["id"] for edge in graph["edges"]}
    if len(edge_ids) != len(graph["edges"]):
        raise RepresentationError("Duplicate current edge identifiers.")
    for edge in graph["edges"]:
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise RepresentationError("Current graph edge has a missing endpoint.")
    rows = {}
    row_node_ids = set()
    for is_edge, entities in ((False, graph["nodes"]), (True, graph["edges"])):
        for entity in entities:
            props = entity["properties"]
            if "table" not in props and "row_index" not in props:
                continue
            table, index, data = props.get("table"), props.get("row_index"), props.get("data")
            if not isinstance(table, str) or not table or type(index) is not int or index < 0 or not isinstance(data, dict):
                raise RepresentationError("Current source record requires table, row_index and data.")
            if entity["id"] in rows:
                raise RepresentationError("Logical row identifiers collide across current graph elements.")
            if not all(isinstance(column, str) for column in data):
                raise RepresentationError("Logical row column names must be strings.")
            if is_edge:
                endpoints = props.get("row_endpoints")
                if not isinstance(endpoints, list) or not endpoints:
                    raise RepresentationError("An edge carrying a source row requires current row_endpoints.")
                seen = set()
                for endpoint in endpoints:
                    if not isinstance(endpoint, dict) or set(endpoint) != {"foreign_key", "node_id"}:
                        raise RepresentationError("Each row endpoint requires foreign_key and node_id.")
                    if endpoint["node_id"] not in (entity["source"], entity["target"]):
                        raise RepresentationError("Row endpoint must be an actual endpoint of its current edge.")
                    fk = endpoint["foreign_key"]
                    if not isinstance(fk, dict) or set(fk) != {"table", "columns", "ref_table", "ref_columns"}:
                        raise RepresentationError("A row endpoint requires a complete foreign-key definition.")
                    if fk["table"] != table or not isinstance(fk["ref_table"], str):
                        raise RepresentationError("Row endpoint foreign key must belong to the edge's source table.")
                    if not isinstance(fk["columns"], list) or not fk["columns"] or not isinstance(fk["ref_columns"], list) or len(fk["columns"]) != len(fk["ref_columns"]) or not all(isinstance(c, str) for c in [*fk["columns"], *fk["ref_columns"]]):
                        raise RepresentationError("Row endpoint foreign-key columns are invalid.")
                    key = _value_key(endpoint)
                    if key in seen:
                        raise RepresentationError("Duplicate current row endpoint relationship.")
                    seen.add(key)
            else:
                if "row_endpoints" in props:
                    raise RepresentationError("Only a current edge row can declare row_endpoints.")
                row_node_ids.add(entity["id"])
            rows[entity["id"]] = copy.deepcopy(entity)

    recovered = {}

    def add_cell(row_id, column, value, location):
        if not isinstance(row_id, str) or row_id not in row_node_ids or not isinstance(column, str) or not column:
            raise RepresentationError("Attribute storage must reference a current row node and named column.")
        key = (row_id, column)
        value_key = _value_key(value)
        if key in recovered:
            previous_value, locations = recovered[key]
            if location in locations:
                raise RepresentationError("A row column has duplicate records at the same current storage location.")
            if previous_value != value_key:
                raise RepresentationError("Current attribute storage locations contain conflicting cell values.")
            locations.add(location)
        else:
            recovered[key] = (value_key, {location})
        data = rows[row_id]["properties"]["data"]
        if column in data and _value_key(data[column]) != _value_key(value):
            raise RepresentationError("Current row and attribute entity contain conflicting cell values.")
        data[column] = copy.deepcopy(value)

    for edge in graph["edges"]:
        properties = edge["properties"]
        if "extracted_attribute" in properties:
            attribute = properties["extracted_attribute"]
            if not isinstance(attribute, dict) or set(attribute) != {"table", "column"}:
                raise RepresentationError("extracted_attribute requires table and column.")
            row_id, target = edge["source"], nodes[edge["target"]]
            if row_id not in row_node_ids or rows[row_id]["properties"]["table"] != attribute["table"]:
                raise RepresentationError("Extracted attribute must originate at its current source row.")
            if "value" not in target["properties"]:
                raise RepresentationError("Current extracted entity has no value.")
            add_cell(row_id, attribute["column"], target["properties"]["value"], ("entity", edge["id"]))
        if "relocated_attributes" in properties:
            attributes = properties["relocated_attributes"]
            if not isinstance(attributes, list) or not attributes:
                raise RepresentationError("relocated_attributes must be a nonempty array.")
            for attribute in attributes:
                if not isinstance(attribute, dict) or set(attribute) - {"row_id", "column", "value", "name"} or {"row_id", "column", "value"} - set(attribute):
                    raise RepresentationError("Relocated attributes require row_id, column and value.")
                if "name" in attribute and (not isinstance(attribute["name"], str) or not attribute["name"]):
                    raise RepresentationError("Relocated attribute name must be a nonempty string.")
                if attribute["row_id"] not in (edge["source"], edge["target"]):
                    raise RepresentationError("Relocated attribute must belong to an incident current row node.")
                add_cell(attribute["row_id"], attribute["column"], attribute["value"], ("edge", edge["id"]))
    return list(rows.values())
