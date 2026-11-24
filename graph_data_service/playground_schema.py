"""Inspect and revise the saved instance graph of an account-scoped Playground project."""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from graph_data_service.construction import ConstructionError, _atomic_json, graph_schema, optimize_graph, validate_graph

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sentence = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class LocalizedText(BaseModel):
    zh: Sentence
    en: Sentence


class TypeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["node", "edge"]
    action: Literal["add", "update", "remove", "reverse"]
    name: Name
    new_name: Name | None = None
    source: Name | None = None
    target: Name | None = None
    new_source: Name | None = None
    new_target: Name | None = None
    description: LocalizedText | None = None
    examples: list[Name] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def supported(self):
        if self.kind == "node" and (self.action == "reverse" or any((self.source, self.target, self.new_source, self.new_target))):
            raise ValueError("Only an edge has endpoints or a direction")
        if self.examples and (self.kind, self.action) != ("node", "add"):
            raise ValueError("Examples are only for adding a node type")
        if self.action != "update" and any((self.new_name, self.new_source, self.new_target)):
            raise ValueError("Use update to change an existing type")
        if self.description and self.action not in {"add", "update"}:
            raise ValueError("Descriptions belong to add or update")
        if self.action == "update" and not any((self.new_name, self.new_source, self.new_target, self.description)):
            raise ValueError("Specify what changes")
        if (self.kind, self.action) == ("edge", "add") and not (self.source and self.target):
            raise ValueError("A new edge needs both endpoint types")
        return self


class EditBatch(BaseModel):
    changes: list[TypeChange] = Field(min_length=1, max_length=32)
    questions: list[LocalizedText] | None = Field(default=None, min_length=1, max_length=6)


class SchemaDecision(BaseModel):
    action: Literal["answer", "propose", "cancel"]
    reply: str = Field(min_length=1, max_length=1600)
    edits: EditBatch | None = None

    @model_validator(mode="after")
    def check_edits(self):
        if (self.action == "propose") != (self.edits is not None):
            raise ValueError("Only a proposal has edits")
        return self


class PendingEdits(EditBatch):
    base_revision: int = Field(ge=1)
    proposal_id: uuid.UUID


class SchemaTurn(BaseModel):
    connection_revision: int = Field(ge=1)
    language: Literal["zh", "en"]
    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(max_length=12)
    scenario: str = Field(max_length=500)
    notes: list[dict] = Field(default_factory=list, max_length=32)
    pending: PendingEdits | None = None


def key(name, source=None, target=None):
    return json.dumps([source, name, target], ensure_ascii=False)


def plan_meanings(plan):
    result = {key(e.type): e.description.model_dump() for e in plan.entities if e.description}
    result.update({key(r.type, r.source, r.target): r.description.model_dump() for r in plan.relations if r.description})
    return result


def snapshot(graph, meanings, revision):
    schema = graph_schema(graph)
    examples = {}
    for node in graph["nodes"]:
        group = examples.setdefault(node["type"], [])
        props = node["properties"]
        data = props.get("data")
        name = props.get("name") or (data.get("name") if isinstance(data, dict) else None)
        if name and len(group) < 3 and str(name) not in group:
            group.append(str(name))
    nodes, edges = [], []
    roles = {}
    for row in schema["edge_types"]:
        source, name, target = row["source_type"], row["type"], row["target_type"]
        roles.setdefault(source, []).append((f"通过「{name}」连接到「{target}」", f"connects to {target} through {name}"))
        roles.setdefault(target, []).append((f"接收来自「{source}」的「{name}」关系", f"is linked from {source} through {name}"))
        description = meanings.get(key(name, source, target)) or {
            "zh": f"表示从「{source}」到「{target}」的「{name}」关系。",
            "en": f"A {name} relationship from {source} to {target}."}
        edges.append({"type": name, "source": source, "target": target, "count": row["relationship_count"], "description": description})
    for row in schema["node_types"]:
        name = row["type"]
        role = roles.get(name, [])
        description = meanings.get(key(name)) or {
            "zh": f"这类节点{role[0][0]}。" if role else f"图中表示「{name}」的实体。",
            "en": f"Each {name} {role[0][1]}." if role else f"An entity of type {name} in this graph."}
        nodes.append({"type": name, "count": row["instance_count"], "examples": examples[name], "description": description})
    return {"revision": revision, "node_count": schema["node_count"], "edge_count": schema["edge_count"],
            "nodes": nodes, "edges": edges}


def read_record(store, uid, did):
    record = store._read(store._directory(uid, did), did)
    if not record.get("playground") or record.get("graph") is None:
        raise ConstructionError("PLAYGROUND_GRAPH_UNAVAILABLE", "The saved scenario graph is unavailable.", 404)
    return record


def saved_schema(store, uid, did):
    record = read_record(store, uid, did)
    return snapshot(record["graph"], record.get("type_meanings", {}), record["revision"])


def _move_meaning(meanings, old, new, description):
    current = meanings.pop(old, None)
    if description:
        meanings[new] = description.model_dump()
    elif current and new not in meanings:
        meanings[new] = current


def _replace_names(text, names):
    # Match whole identifiers in Latin text while allowing labels within a Chinese sentence.
    patterns = [rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])" if name.isascii() else re.escape(name)
                for name in sorted(names, key=len, reverse=True)]
    return re.sub("|".join(patterns), lambda match: names[match[0]], text) if patterns else text


def _rename_in_meaning(meanings, entry, old, new):
    if entry in meanings:
        meanings[entry] = {lang: _replace_names(text, {old: new}) for lang, text in meanings[entry].items()}


def _node_edit(graph, meanings, edit):
    name = edit.name
    ids = [r["id"] for r in graph["nodes"] if r["type"] == name]
    if edit.action == "add":
        if ids:
            raise ValueError(f"Node type {name} already exists")
        operations = [{"op": "add_node", "node_type": name, "node_name": example, "properties": {"name": example}}
                      for example in dict.fromkeys(edit.examples or [f"{name} 01"])]
        graph, _ = optimize_graph(graph, operations)
    elif not ids:
        raise ValueError(f"Unknown node type {name}")
    elif edit.action == "remove":
        graph, _ = optimize_graph(graph, [{"op": "delete", "target": "node", "ids": ids, "cascade": True}])
        for entry in list(meanings):
            source, label, target = json.loads(entry)
            if name in (source, target) or source is None and label == name:
                del meanings[entry]
        return graph
    elif edit.new_name and edit.new_name != name:
        by_id = {n["id"]: n["type"] for n in graph["nodes"]}
        neighbors = {name}
        for edge in graph["edges"]:
            endpoints = (by_id[edge["source"]], by_id[edge["target"]])
            if name in endpoints:
                neighbors.update(endpoints)
        graph, _ = optimize_graph(graph, [{"op": "rename_type", "target": "node", "old_type": name, "new_type": edit.new_name}])
        for entry in list(meanings):
            source, label, target = json.loads(entry)
            if source is None and label in neighbors:
                _rename_in_meaning(meanings, entry, name, edit.new_name)
            if name in (source, target):
                _rename_in_meaning(meanings, entry, name, edit.new_name)
                _move_meaning(meanings, entry, key(label, edit.new_name if source == name else source,
                                                    edit.new_name if target == name else target), None)
    _move_meaning(meanings, key(name), key(edit.new_name or name), edit.description)
    return graph


def _edge_edit(graph, meanings, edit):
    by_id = {n["id"]: n["type"] for n in graph["nodes"]}
    groups = {}
    for node_id, name in by_id.items():
        groups.setdefault(name, []).append(node_id)
    signatures = {}
    for edge in graph["edges"]:
        if edge["type"] == edit.name:
            pair = (by_id[edge["source"]], by_id[edge["target"]])
            if (not edit.source or edit.source == pair[0]) and (not edit.target or edit.target == pair[1]):
                signatures.setdefault(pair, []).append(edge)
    if edit.action == "add":
        source, target = edit.source, edit.target
        if signatures or source not in groups or target not in groups:
            raise ValueError("Choose a new relationship between two existing node types")
        targets = groups[target]
        operations = [{"op": "add_edge", "edge_type": edit.name, "source": identity,
                       "target": targets[(i + (source == target)) % len(targets)]} for i, identity in enumerate(groups[source])]
        # The construction engine accepts at most 100 operations per call.
        for i in range(0, len(operations), 100):
            graph, _ = optimize_graph(graph, operations[i:i + 100])
        _move_meaning(meanings, key(edit.name, source, target), key(edit.name, source, target), edit.description)
        return graph
    if len(signatures) != 1:
        raise ValueError(f"Select one existing signature for edge type {edit.name}, including source and target")
    (source, target), rows = next(iter(signatures.items()))
    old = key(edit.name, source, target)
    if edit.action == "remove":
        graph, _ = optimize_graph(graph, [{"op": "delete", "target": "edge", "ids": [r["id"] for r in rows]}])
        meanings.pop(old, None)
        return graph
    if edit.action == "reverse":
        graph, _ = optimize_graph(graph, [{"op": "reverse_edges", "edge_ids": [r["id"] for r in rows]}])
        # An old explanation may assert the opposite direction.
        meanings.pop(old, None)
        return graph
    new_source, new_target = edit.new_source or source, edit.new_target or target
    if new_source not in groups or new_target not in groups:
        raise ValueError("The new endpoint types must exist")
    for field, before, after in (("source", source, new_source), ("target", target, new_target)):
        if before != after:
            replacements = {old_id: groups[after][i % len(groups[after])] for i, old_id in enumerate(dict.fromkeys(r[field] for r in rows))}
            for row in rows:
                row[field] = replacements[row[field]]
    for row in rows:
        row["type"] = edit.new_name or edit.name
    if (new_source, new_target) != (source, target):
        meanings.pop(old, None)
    elif edit.new_name and edit.new_name != edit.name:
        _rename_in_meaning(meanings, old, edit.name, edit.new_name)
    _move_meaning(meanings, old, key(edit.new_name or edit.name, new_source, new_target), edit.description)
    return graph


def _resolve_change(graph, change, renamed_nodes, renamed_edges):
    # Follow the same instances through a batch. A name that still exists takes precedence.
    by_node = {n["id"]: n["type"] for n in graph["nodes"]}
    names = set(by_node.values())

    def node(name):
        return name if name in names else by_node.get(renamed_nodes.get(name), name)

    if change.kind == "node":
        if change.action == "add":
            return change
        name = node(change.name)
        if change.new_name and name in names:
            renamed_nodes[name] = next(i for i, label in by_node.items() if label == name)
        return change.model_copy(update={"name": name})
    change = change.model_copy(update={field: node(getattr(change, field))
                                       for field in ("source", "target", "new_source", "new_target")})
    if change.action == "add":
        return change

    by_edge = {e["id"]: (by_node[e["source"]], e["type"], by_node[e["target"]]) for e in graph["edges"]}

    def matches(signature):
        source, name, target = signature
        return name == change.name and change.source in (None, node(source)) and change.target in (None, node(target))

    choices = {sig for sig in by_edge.values() if matches(sig)}
    if not choices:
        choices = {by_edge[i] for sig, i in renamed_edges.items() if i in by_edge and matches(sig)}
    if len(choices) != 1:
        actual = sorted({sig for sig in by_edge.values() if sig[1] == change.name})
        raise ValueError(f"Choose one signature for {change.name}. Current matching types: {actual}")
    signature = choices.pop()
    if change.action in {"update", "reverse"}:
        renamed_edges[signature] = next(i for i, sig in by_edge.items() if sig == signature)
    source, name, target = signature
    return change.model_copy(update={"name": name, "source": source, "target": target})


def candidate(record, edits):
    if record.get("draft_graph") is not None or record["status"] != "ready":
        raise ConstructionError("PLAYGROUND_GRAPH_BUSY", "Finish the pending graph construction changes first.", 409)
    graph = copy.deepcopy(record["graph"])
    meanings = dict(record.get("type_meanings", {}))
    renamed_nodes, renamed_edges, resolved = {}, {}, []
    for requested in edits.changes:
        change = _resolve_change(graph, requested, renamed_nodes, renamed_edges)
        graph = (_node_edit if change.kind == "node" else _edge_edit)(graph, meanings, change)
        resolved.append(change)
    validate_graph(graph, copy_graph=False)
    if graph == record["graph"] and meanings == record.get("type_meanings", {}):
        raise ValueError("The proposed edits do not change the graph or its type definitions")
    return graph, meanings, resolved


def check_revision(record, revision):
    if record["revision"] != revision:
        raise ConstructionError("PLAYGROUND_SCHEMA_CHANGED", "The graph has changed. Review its current types and propose the edit again.", 409)


def _refresh_question_names(questions, previous, graph):
    # Rewrite a literal type only when its instances now share a single, unambiguous new label.
    current = {r["type"] for side in ("nodes", "edges") for r in graph[side]}
    moves = {}
    for side in ("nodes", "edges"):
        new_types = {r["id"]: r["type"] for r in graph[side]}
        for row in previous[side]:
            if row["type"] not in current and row["id"] in new_types:
                moves.setdefault(row["type"], set()).add(new_types[row["id"]])
    names = {old: next(iter(labels)) for old, labels in moves.items() if len(labels) == 1}
    if names:
        for localized in [questions, *questions.get("translations", {}).values()]:
            if "questions" in localized:
                localized["questions"] = [_replace_names(q, names) for q in localized["questions"]]


def apply_edits(store, uid, did, proposal):
    with store._write_lock(uid, did) as directory:
        record = read_record(store, uid, did)
        receipt = str(proposal.proposal_id)
        if record.get("schema_edit_receipt") != receipt:
            check_revision(record, proposal.base_revision)
            graph, meanings, _ = candidate(record, proposal)
            if proposal.questions is not None:
                previous = record.get("generated_questions", {})
                record["generated_questions"] = {**previous, "questions": [q.zh for q in proposal.questions],
                    "translations": {"en": {"questions": [q.en for q in proposal.questions]}}}
            elif record.get("generated_questions"):
                _refresh_question_names(record["generated_questions"], record["graph"], graph)
            record.update(graph=graph, type_meanings=meanings, revision=record["revision"] + 1,
                          schema_edit_receipt=receipt, updated_at=datetime.now(timezone.utc).isoformat())
            if record.get("generated_questions"):
                record["question_generation"] = {"status": "ready"}
            elif record.get("question_generation", {}).get("status") == "generating":
                record.pop("question_generation", None)
            _atomic_json(directory / "record.json", record)
        return {**store._status(record, directory), "schema": snapshot(record["graph"], record.get("type_meanings", {}), record["revision"])}
