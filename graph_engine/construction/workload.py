"""Frozen business workloads, independent SQLite oracle, and native graph execution.

The JSON DSL deliberately contains no SQL, Python expressions, or model answers.
Column references are ``alias.column``; output aliases in ``order_by`` refer to
projection/aggregation names.  Joins explicitly name a declared foreign key and
its direction.  Queries use SQL bag (duplicate preserving) and NULL semantics.
The evaluator never obtains graph answers from source tables or the oracle.
"""
from __future__ import annotations

import copy
import functools
import json
import math
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from graph_engine.construction.representation import resolve_rows
from .budget import checked, check_deadline, deadline_scope, ConstructionBudgetExceeded

_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "is_null", "not_null"}
_FUNCTIONS = {"count", "sum", "min", "max", "avg"}
MAX_INTERMEDIATE_ROWS = 200_000
QUERY_TIMEOUT_SECONDS = 10.0
MAX_GENERATED_PLANS = 2_000
MAX_BENCHMARK_INNER_ITERATIONS = 256


class QueryLimitError(ValueError):
    """An exact query exceeded its resource budget; never a truncated success."""


def _check_overall_deadline(deadline: float | None) -> None:
    check_deadline(deadline)


def _query_deadline(deadline: float | None, query_timeout_seconds: float | None = None) -> float:
    _check_overall_deadline(deadline)
    timeout = QUERY_TIMEOUT_SECONDS if query_timeout_seconds is None else query_timeout_seconds
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    return time.perf_counter() + remaining


def _query_resource_limits(source: Any) -> tuple[int, float]:
    """Resolve validated numeric query limits without exposing source to the executor."""
    limits = _get(source, "resource_limits")
    max_rows = _get(limits, "max_intermediate_rows", MAX_INTERMEDIATE_ROWS)
    timeout = _get(limits, "query_timeout_seconds", QUERY_TIMEOUT_SECONDS)
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("max_intermediate_rows must be a positive integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise ValueError("query_timeout_seconds must be a finite number")
    return max_rows, float(timeout)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _fk(fk: Any, table: str | None = None) -> dict:
    return {"table": _get(fk, "table", table), "columns": list(_get(fk, "columns", [])),
            "ref_table": _get(fk, "ref_table"), "ref_columns": list(_get(fk, "ref_columns", []))}


def _fk_key(fk: dict) -> tuple:
    return (fk["table"], tuple(fk["columns"]), fk["ref_table"], tuple(fk["ref_columns"]))


def _path_key(path: list[dict]) -> tuple:
    return tuple((_fk_key(step), step["direction"]) for step in path)


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int)) or (
        isinstance(value, float) and math.isfinite(value))


def _fields(obj: Any, allowed: set[str], required: set[str], where: str) -> None:
    if not isinstance(obj, dict) or set(obj) - allowed or required - set(obj):
        raise ValueError(f"Invalid {where} fields; allowed={sorted(allowed)}, required={sorted(required)}")


def _name(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise ValueError(f"Invalid {where}: {value!r}")


def _bound(value: Any, low: int, high: int, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{where} must be an integer between {low} and {high}")


def _reference(source: Any, aliases: dict, reference: Any) -> tuple[str, str]:
    if not isinstance(reference, str) or "." not in reference:
        raise ValueError(f"Invalid column reference: {reference!r}")
    alias, column = reference.split(".", 1)
    if alias not in aliases or column not in _get(source.tables[aliases[alias]], "columns", []):
        raise ValueError(f"Unknown column reference: {reference!r}")
    return alias, column


def _semantic_fingerprint(q: dict) -> str:
    plan = copy.deepcopy({k: v for k, v in q.items() if k not in ("id", "question")})
    aliases = {"t0": "t0", **{j["alias"]: f"t{i + 1}" for i, j in enumerate(plan["joins"])}}

    def reference(value: str) -> str:
        if value == "*":
            return value
        alias, column = value.split(".", 1)
        return f"{aliases[alias]}.{column}"

    for join in plan["joins"]:
        join["from"], join["alias"] = aliases[join["from"]], aliases[join["alias"]]
    outputs = {}
    for i, item in enumerate(plan["projection"] + plan["aggregations"]):
        outputs[item["as"]] = f"output_{i}"
        item["as"] = f"output_{i}"
        item["column"] = reference(item["column"])
    for item in plan["filters"]:
        item["column"] = reference(item["column"])
    plan["filters"].sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    plan["group_by"] = sorted(reference(value) for value in plan["group_by"])
    for item in plan["order_by"]:
        item["column"] = outputs[item["column"]]
    return json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _validate_queries(source: Any, queries: Any, count: int | None = None) -> list[dict]:
    """Validate and freeze JSON query plans; no SQL strings or extra keys allowed.

    ``count`` requires that exact number of distinct plans.  Numeric SUM/AVG
    require numeric source values. LIMIT requires ordering all output columns,
    making the selected bag deterministic even when source/graph row order differs.
    """
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must be a nonempty list")
    if count is not None:
        _bound(count, 1, 100, "count")
        if len(queries) != count:
            raise ValueError(f"Expected {count} queries, received {len(queries)}")
    _bound(len(queries), 1, 100, "query count")
    normalized, seen_ids, seen_plans = [], set(), set()
    declared = {_fk_key(_fk(f, table)) for table, spec in source.tables.items()
                for f in _get(spec, "foreign_keys", [])}
    for original in queries:
        _fields(original, {"id", "question", "root", "joins", "filters", "projection", "group_by",
                           "aggregations", "order_by", "limit"}, {"id", "question", "root"}, "query")
        q = copy.deepcopy(original)
        _name(q["id"], "query id")
        if q["id"] in seen_ids:
            raise ValueError("Query IDs must be unique")
        seen_ids.add(q["id"])
        if not isinstance(q["question"], str) or not q["question"].strip() or len(q["question"]) > 4000:
            raise ValueError("question must be nonempty text of at most 4000 characters")
        _fields(q["root"], {"table", "alias"}, {"table", "alias"}, "root")
        if not isinstance(q["root"]["table"], str) or q["root"]["table"] not in source.tables or q["root"]["alias"] != "t0":
            raise ValueError("root must name a source table with alias t0")
        aliases = {"t0": q["root"]["table"]}
        for field in ("joins", "filters", "projection", "group_by", "aggregations", "order_by"):
            q.setdefault(field, [])
            if not isinstance(q[field], list) or len(q[field]) > (3 if field == "joins" else 100):
                raise ValueError(f"Invalid {field} list")
        q.setdefault("limit", None)
        for join in q["joins"]:
            _fields(join, {"from", "alias", "table", "direction", "kind", "foreign_key"},
                    {"from", "alias", "table", "direction", "kind", "foreign_key"}, "join")
            _name(join["alias"], "join alias")
            _name(join["from"], "join source alias")
            if join["alias"] in aliases or join["from"] not in aliases:
                raise ValueError("Join aliases must be new and reference a previously bound alias")
            if join["direction"] not in ("forward", "reverse") or join["kind"] not in ("inner", "left"):
                raise ValueError("Invalid join direction or kind")
            f = join["foreign_key"]
            _fields(f, {"table", "columns", "ref_table", "ref_columns"},
                    {"table", "columns", "ref_table", "ref_columns"}, "foreign_key")
            if not isinstance(f["table"], str) or not isinstance(f["ref_table"], str):
                raise ValueError("Foreign-key table identifiers must be strings")
            if any(not isinstance(f[k], list) or not f[k] or
                   any(not isinstance(c, str) for c in f[k]) for k in ("columns", "ref_columns")):
                raise ValueError("Foreign key columns must be nonempty string lists")
            if _fk_key(f) not in declared:
                raise ValueError("Joins must use an explicitly declared source foreign key")
            left, right = ((f["table"], f["ref_table"]) if join["direction"] == "forward"
                           else (f["ref_table"], f["table"]))
            if aliases[join["from"]] != left or join["table"] != right:
                raise ValueError("Join tables disagree with foreign-key direction")
            aliases[join["alias"]] = right
        for item in q["filters"]:
            _fields(item, {"column", "op", "value"}, {"column", "op"}, "filter")
            _reference(source, aliases, item["column"])
            if not isinstance(item["op"], str) or item["op"] not in _OPS:
                raise ValueError("Unsupported filter operator")
            if item["op"] in ("is_null", "not_null"):
                if "value" in item:
                    raise ValueError("NULL predicates do not take a value")
            elif "value" not in item:
                raise ValueError("Filter value is required")
            elif item["op"] == "in":
                if not isinstance(item["value"], list) or not item["value"] or len(item["value"]) > 1000 or not all(_scalar(v) for v in item["value"]):
                    raise ValueError("IN requires 1..1000 JSON scalar values")
            elif not _scalar(item["value"]):
                raise ValueError("Filter values must be finite JSON scalars")
            if item["op"] == "contains" and not isinstance(item["value"], str):
                raise ValueError("contains requires a string")
            if item["op"] == "contains":
                alias, column = _reference(source, aliases, item["column"])
                if any(row.get(column) is not None and not isinstance(row.get(column), str)
                       for row in _get(source.tables[aliases[alias]], "rows", [])):
                    raise ValueError("contains requires a text source column")
            alias, column = _reference(source, aliases, item["column"])
            spec = source.tables[aliases[alias]]
            if _get(spec, "create_sql") and _affinity(_get(spec, "column_types", {}).get(column, "")) == "TEXT":
                operands = item.get("value", []) if item["op"] == "in" else [item.get("value")]
                if any(isinstance(v, float) for v in operands):
                    raise ValueError("Float-to-TEXT comparisons are unsupported; supply an explicit text literal")
        names = set()
        for item in q["projection"]:
            _fields(item, {"column", "as"}, {"column", "as"}, "projection")
            _reference(source, aliases, item["column"])
            _name(item["as"], "output alias")
            if item["as"] in names:
                raise ValueError("Output aliases must be unique")
            names.add(item["as"])
        for reference in q["group_by"]:
            _reference(source, aliases, reference)
        if len(set(q["group_by"])) != len(q["group_by"]):
            raise ValueError("Duplicate group-by columns")
        for item in q["aggregations"]:
            _fields(item, {"function", "column", "as"}, {"function", "column", "as"}, "aggregation")
            if not isinstance(item["function"], str) or item["function"] not in _FUNCTIONS or (item["column"] == "*" and item["function"] != "count"):
                raise ValueError("Unsupported aggregation")
            if item["column"] != "*":
                alias, column = _reference(source, aliases, item["column"])
                if item["function"] in ("sum", "avg") and any(
                    row.get(column) is not None and not isinstance(row.get(column), (bool, int, float))
                    for row in _get(source.tables[aliases[alias]], "rows", [])):
                    raise ValueError("SUM/AVG require numeric source columns")
            _name(item["as"], "aggregation alias")
            if item["as"] in names:
                raise ValueError("Output aliases must be unique")
            names.add(item["as"])
        if not names:
            raise ValueError("A projection or aggregation is required")
        if q["aggregations"] or q["group_by"]:
            if any(p["column"] not in q["group_by"] for p in q["projection"]):
                raise ValueError("Every projected column must occur in group_by for grouped queries")
        for item in q["order_by"]:
            _fields(item, {"column", "direction"}, {"column", "direction"}, "order_by")
            if not isinstance(item["column"], str) or item["column"] not in names or item["direction"] not in ("asc", "desc"):
                raise ValueError("Order by must reference an output alias with asc/desc direction")
        if len({o["column"] for o in q["order_by"]}) != len(q["order_by"]):
            raise ValueError("Duplicate order-by columns")
        if q["limit"] is not None:
            _bound(q["limit"], 1, 100000, "limit")
            if {o["column"] for o in q["order_by"]} != names:
                raise ValueError("LIMIT requires ordering every output column for deterministic results")
        fingerprint = _semantic_fingerprint(q)
        if fingerprint in seen_plans:
            raise ValueError("Duplicate query plans do not count as independent coverage")
        seen_plans.add(fingerprint)
        normalized.append(q)
    return normalized


def validate_queries(source: Any, queries: Any, count: int | None = None) -> list[dict]:
    """Return a validated deep copy of the JSON DSL or raise ``ValueError``.

    Invalid JSON shapes (including lists in identifier positions) are validation
    failures, never executable expressions or low-level implementation errors.
    """
    try:
        return _validate_queries(source, queries, count)
    except (TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ValueError(f"Malformed query plan: {type(exc).__name__}") from exc


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sql_ref(reference: str) -> str:
    if reference == "*":
        return "*"
    alias, column = reference.split(".", 1)
    return f"{_quote(alias)}.{_quote(column)}"


def _compile_sql(q: dict, source: Any = None) -> tuple[str, list]:
    selected = [f"{_sql_ref(p['column'])} AS {_quote(p['as'])}" for p in q["projection"]]
    selected += [f"{a['function'].upper()}({_sql_ref(a['column'])}) AS {_quote(a['as'])}" for a in q["aggregations"]]
    sql = f"SELECT {', '.join(selected)} FROM {_quote(q['root']['table'])} AS t0"
    params = []
    for join in q["joins"]:
        f = join["foreign_key"]
        parent_alias, child_alias = ((join["alias"], join["from"]) if join["direction"] == "forward"
                                     else (join["from"], join["alias"]))
        # Parent on the left preserves its declared collation, matching the
        # relational source's FK construction (including NOCASE parent keys).
        on = " AND ".join(f"{_quote(parent_alias)}.{_quote(p)} = {_quote(child_alias)}.{_quote(c)}"
                          for c, p in zip(f["columns"], f["ref_columns"]))
        if source is not None and not _get(source.tables[f["table"]], "create_sql"):
            # CSV/TXT import defines an empty FK cell as a missing relationship,
            # while preserving the original empty string in row properties.
            on += " AND " + " AND ".join(f"{_quote(child_alias)}.{_quote(c)} != ''" for c in f["columns"])
        sql += f" {join['kind'].upper()} JOIN {_quote(join['table'])} AS {_quote(join['alias'])} ON {on}"
    predicates = []
    for item in q["filters"]:
        col, op = _sql_ref(item["column"]), item["op"]
        if op in ("is_null", "not_null"):
            predicates.append(f"{col} IS {'NOT ' if op == 'not_null' else ''}NULL")
        elif op == "in":
            predicates.append(f"{col} IN ({','.join('?' for _ in item['value'])})")
            params.extend(item["value"])
        elif op == "contains":
            predicates.append(f"instr({col}, ?) > 0")
            params.append(item["value"])
        else:
            operator = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
            predicates.append(f"{col} {operator} ?")
            params.append(item["value"])
    if predicates:
        sql += " WHERE " + " AND ".join(predicates)
    if q["group_by"]:
        sql += " GROUP BY " + ", ".join(_sql_ref(c) for c in q["group_by"])
    if q["order_by"]:
        sql += " ORDER BY " + ", ".join(f"{_quote(o['column'])} {o['direction'].upper()}" for o in q["order_by"])
        if q["limit"] is not None:
            # NOCASE/RTRIM can tie distinct strings even when all output columns
            # are ordered. Binary secondary keys make LIMIT unambiguous.
            sql += ", " + ", ".join(f"{_quote(o['column'])} COLLATE BINARY {o['direction'].upper()}" for o in q["order_by"])
    if q["limit"] is not None:
        sql += " LIMIT ?"
        params.append(q["limit"])
    return sql, params


def _oracle(source: Any, *, deadline: float | None = None) -> sqlite3.Connection:
    _check_overall_deadline(deadline)
    db = sqlite3.connect(":memory:")
    try:
        database_path = _get(source, "_database_path")
        if database_path is not None:
            path = Path(database_path)
            expected_identity = _get(source, "_database_identity")

            def identity(candidate):
                try:
                    stat = candidate.stat()
                except FileNotFoundError:
                    return None
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

            def current_identity():
                return (identity(path), identity(Path(str(path) + "-wal")),
                        identity(Path(str(path) + "-journal")))

            if expected_identity is None or current_identity() != expected_identity:
                raise ValueError("SQLite source file changed after loading")
            db.close()
            # Do not use immutable=1: it can silently ignore committed WAL rows.
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            db.enable_load_extension(False)
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("PRAGMA query_only=ON")
            if current_identity() != expected_identity:
                raise ValueError("SQLite source file changed while opening the oracle")
            return db
        original = _get(source, "_connection")
        if original is not None:
            original.backup(db, pages=256, progress=lambda *_: _check_overall_deadline(deadline))
            _check_overall_deadline(deadline)
            db.execute("PRAGMA query_only=ON")
            return db
        for name, table in source.tables.items():
            _check_overall_deadline(deadline)
            columns = list(_get(table, "columns", []))
            # No SQLite affinity: JSON scalar storage classes remain unchanged.
            db.execute(f"CREATE TABLE {_quote(name)} ({', '.join(_quote(c) for c in columns)})")
            values = []
            for index, row in enumerate(_get(table, "rows", [])):
                if index % 1024 == 0:
                    _check_overall_deadline(deadline)
                record = tuple(row.get(c) for c in columns)
                if not all(_scalar(v) for v in record):
                    raise ValueError("Source rows must contain finite JSON scalar values")
                values.append(record)
            db.executemany(f"INSERT INTO {_quote(name)} VALUES ({','.join('?' for _ in columns)})", values)
            _check_overall_deadline(deadline)
        return db
    except Exception:
        db.close()
        raise


def _compare(a: Any, b: Any) -> int:
    """SQLite NULL < numeric < text storage-class ordering (binary collation)."""
    rank = lambda v: 0 if v is None else 1 if isinstance(v, (int, float)) else 2
    ra, rb = rank(a), rank(b)
    if ra != rb:
        return -1 if ra < rb else 1
    if ra == 0:
        return 0
    return (a > b) - (a < b)


def _affinity(declared: str) -> str:
    declared = declared.upper()
    if "INT" in declared:
        return "NUMERIC"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "NUMERIC"
    return "NUMERIC"


def _numeric(value: Any) -> Any:
    if not isinstance(value, str) or not re.fullmatch(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", value, re.ASCII):
        return value
    try:
        integer = int(value, 10)
        if -(2 ** 63) <= integer < 2 ** 63:
            return integer
    except ValueError:
        pass
    number = float(value)
    return int(number) if math.isfinite(number) and number.is_integer() and -(2 ** 63) <= number < 2 ** 63 else number


def _apply_affinity(value: Any, affinity: str) -> Any:
    if affinity == "NUMERIC":
        return _numeric(value)
    if affinity == "TEXT" and isinstance(value, (int, float)):
        if isinstance(value, int):
            return str(int(value))
        # SQLite's floating point -> TEXT rendering is deliberately not guessed.
        # Validation rejects these operands; corrupted graph data fails here.
        raise ValueError("Float-to-TEXT affinity comparisons are unsupported; use an explicit text literal")
    return value


def _collation_key(value: Any, collation: str) -> Any:
    if not isinstance(value, str) or collation == "BINARY":
        return value
    if collation == "NOCASE":
        if "\x00" in value:
            raise ValueError("Embedded NUL with NOCASE collation is unsupported")
        return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    if collation == "RTRIM":
        return value.rstrip(" ")
    raise ValueError(f"Unsupported SQLite collation: {collation}")


def _compare_collated(a: Any, b: Any, collation: str) -> int:
    return _compare(_collation_key(a, collation), _collation_key(b, collation))


def _value(env: dict, reference: str) -> Any:
    alias, column = reference.split(".", 1)
    row = env.get(alias)
    return None if row is None else row["properties"]["data"].get(column)


def _matches(env: dict, item: dict, affinity: str = "BLOB", collation: str = "BINARY") -> bool:
    a, op = _value(env, item["column"]), item["op"]
    if op == "is_null":
        return a is None
    if op == "not_null":
        return a is not None
    b = item.get("value")
    if a is None or b is None:
        return False
    if op == "in":
        return any(v is not None and _compare_collated(a, _apply_affinity(v, affinity), collation) == 0 for v in b)
    if op == "contains":
        # Match SQLite's text conversion for bools and ordinary numeric values.
        text = str(int(a)) if isinstance(a, bool) else str(a)
        return b in text
    c = _compare_collated(a, _apply_affinity(b, affinity), collation)
    return {"eq": c == 0, "ne": c != 0, "lt": c < 0, "le": c <= 0, "gt": c > 0, "ge": c >= 0}[op]


class GraphExecutor:
    """Index and execute the current graph only; does not accept a source object."""

    def __init__(self, graph: dict, *, deadline=None, max_intermediate_rows=None,
                 query_timeout_seconds=None):
        # Schema is read from the current graph's row metadata. No source
        # object, connection or oracle answers enter this executor.
        max_intermediate_rows = (MAX_INTERMEDIATE_ROWS if max_intermediate_rows is None
                                 else max_intermediate_rows)
        query_timeout_seconds = (QUERY_TIMEOUT_SECONDS if query_timeout_seconds is None
                                 else query_timeout_seconds)
        if type(max_intermediate_rows) is not int or max_intermediate_rows < 1:
            raise ValueError("max_intermediate_rows must be a positive integer")
        if (isinstance(query_timeout_seconds, bool)
                or not isinstance(query_timeout_seconds, (int, float))
                or not math.isfinite(query_timeout_seconds)):
            raise ValueError("query_timeout_seconds must be a finite number")
        self.max_intermediate_rows = max_intermediate_rows
        self.query_timeout_seconds = float(query_timeout_seconds)
        self.schema = {}
        self.nodes, self.tables = {}, defaultdict(list)
        self.adjacency = defaultdict(lambda: defaultdict(set))
        self.shortcuts = {}
        self.attribute_indexes = {}
        self.last_execution_stats = {}
        self.materialized_hits = 0
        self.deadline = float("inf")
        self.overall_deadline = deadline
        _check_overall_deadline(deadline)
        if deadline is None:
            current_rows = resolve_rows(graph)
        else:
            with deadline_scope(deadline):
                current_rows = resolve_rows(graph)
        for node in checked(current_rows, deadline=deadline):
            props = node.get("properties", {})
            if isinstance(props.get("relational_schema"), dict) and isinstance(props.get("table"), str):
                metadata = dict(props["relational_schema"], name=props["table"])
                existing = self.schema.get(props["table"])
                if existing is not None and existing != metadata:
                    raise ValueError(f"Conflicting graph schema metadata for table {props['table']!r}")
                self.schema[props["table"]] = metadata
        for node in checked(current_rows, deadline=deadline):
            props = node.get("properties", {})
            if not isinstance(props.get("data"), dict) or not isinstance(props.get("table"), str):
                continue
            if node["id"] in self.nodes:
                raise ValueError(f"Duplicate row node id: {node['id']!r}")
            if not all(_scalar(v) for v in props["data"].values()):
                raise ValueError("Graph data rows must contain finite JSON scalars")
            self.nodes[node["id"]] = node
            self.tables[props["table"]].append(node)
        self._build_attribute_indexes(graph, deadline=deadline)
        materialized = []
        relationships = []
        for edge in checked(graph.get("edges", []), deadline=deadline):
            props = edge.get("properties", {})
            if "materialized_join" in props:
                materialized.append(edge)
                continue
            f = props.get("foreign_key")
            if not isinstance(f, dict):
                continue
            relationships.append((edge["source"], edge["target"], f))
        for row in checked(current_rows, deadline=deadline):
            if "source" not in row or "target" not in row:
                continue
            # A current bridge edge is itself the row entity. Its actual
            # endpoints express its FK roles; no historical node is recreated.
            for endpoint in row["properties"].get("row_endpoints", []):
                relationships.append((row["id"], endpoint["node_id"], endpoint["foreign_key"]))
        for source_id, target_id, f in checked(relationships, deadline=deadline):
            try:
                key = _fk_key(f)
                left, right = self.nodes.get(source_id), self.nodes.get(target_id)
                if left is None or right is None:
                    continue
                # Reversed storage retains relationship semantics only when the
                # endpoints' current table/data agree with the explicit FK.
                orientations = [(left, right), (right, left)]
                for child, parent in orientations:
                    if child["properties"]["table"] != f["table"] or parent["properties"]["table"] != f["ref_table"]:
                        continue
                    a = [child["properties"]["data"].get(c) for c in f["columns"]]
                    b = [parent["properties"]["data"].get(c) for c in f["ref_columns"]]
                    equal = True
                    for child_column, parent_column, child_value, parent_value in zip(f["columns"], f["ref_columns"], a, b):
                        ca, _ = self._column_semantics(f["table"], child_column)
                        pa, pc = self._column_semantics(f["ref_table"], parent_column)
                        if "NUMERIC" in (ca, pa):
                            child_value, parent_value = _numeric(child_value), _numeric(parent_value)
                        elif pa == "TEXT" and ca == "BLOB":
                            child_value = _apply_affinity(child_value, "TEXT")
                        elif ca == "TEXT" and pa == "BLOB":
                            parent_value = _apply_affinity(parent_value, "TEXT")
                        if _compare_collated(parent_value, child_value, pc):
                            equal = False
                    sql_affinity = self.schema.get(f["table"], {}).get("sqlite_affinity") or self.schema.get(f["table"], {}).get("create_sql")
                    if not a or len(a) != len(b) or any(v is None for v in a + b) or (not sql_affinity and any(v == "" for v in a)) or not equal:
                        continue
                    self.adjacency[(key, "forward")][child["id"]].add(parent["id"])
                    self.adjacency[(key, "reverse")][parent["id"]].add(child["id"])
                    break
            except (KeyError, TypeError, ValueError):
                continue
        candidates = defaultdict(lambda: defaultdict(set))
        for edge in checked(materialized, deadline=deadline):
            m = edge["properties"]["materialized_join"]
            try:
                path, ids = m["path"], m["node_ids"]
                if len(path) not in (2, 3) or len(ids) != len(path) + 1:
                    continue
                key = _path_key(path)
                if edge["source"] != ids[0] or edge["target"] != ids[-1] or any(n not in self.nodes for n in ids):
                    continue
                if any(ids[i + 1] not in self.adjacency[step].get(ids[i], ()) for i, step in enumerate(key)):
                    continue
                candidates[key][ids[0]].add(tuple(ids))
            except (KeyError, TypeError, ValueError):
                continue
        # Verify full path multiplicity using DP counts. A partially generated
        # shortcut set is never allowed to silently truncate a join's answers.
        for key, starts in candidates.items():
            memo = {}

            def path_count(node_id: Any, depth: int) -> int:
                _check_overall_deadline(deadline)
                if depth == len(key):
                    return 1
                token = (node_id, depth)
                if token not in memo:
                    memo[token] = sum(path_count(n, depth + 1) for n in self.adjacency[key[depth]].get(node_id, ()))
                return memo[token]

            complete = {start: paths for start, paths in starts.items() if len(paths) == path_count(start, 0)}
            if complete:
                self.shortcuts[key] = complete

    def _build_attribute_indexes(self, graph: dict, *, deadline=None) -> None:
        """Certify entity access paths against complete *current graph* rows.

        A derived entity is an index only when its edges cover every non-NULL
        row of a table/column and each entity value equals that row's live cell.
        Partial, stale or tokenized category structures cannot narrow a scan.
        Extracted cells were resolved from their actual storage by resolve_rows;
        no source table, provenance snapshot or oracle is consulted here.
        """
        entities = {node["id"]: node for node in checked(graph.get("nodes", []), deadline=deadline)}
        candidates = {}
        for edge in checked(graph.get("edges", []), deadline=deadline):
            props = edge.get("properties", {})
            extracted = props.get("extracted_attribute")
            if isinstance(extracted, dict):
                column, kind = extracted.get("column"), "extracted_attribute"
            elif "column" in props and "position" in props:
                column, kind = props["column"], "entity_column"
            else:
                continue
            row = self.nodes.get(edge.get("source"))
            if row is None or not isinstance(column, str):
                continue
            table = row["properties"]["table"]
            state = candidates.setdefault((table, column), {"rows": {}, "kinds": set(), "valid": True})
            state["kinds"].add(kind)
            target = entities.get(edge.get("target"))
            data = row["properties"]["data"]
            if (column not in data or target is None or "value" not in target.get("properties", {})
                    or "table" in target.get("properties", {})
                    or isinstance(extracted, dict) and extracted.get("table") != table):
                state["valid"] = False
                continue
            value = target["properties"]["value"]
            # Exact stored values must agree before SQLite comparison semantics
            # are applied. Collation equivalence must not bless stale data.
            if (not _scalar(value) or json.dumps(value, ensure_ascii=False, allow_nan=False)
                    != json.dumps(data[column], ensure_ascii=False, allow_nan=False)):
                state["valid"] = False
                continue
            state["rows"][row["id"]] = value
        for (table, column), state in checked(candidates.items(), deadline=deadline):
            if not state["valid"]:
                continue
            rows = self.tables[table]
            if any(column not in row["properties"]["data"] or (
                    row["properties"]["data"][column] is not None and row["id"] not in state["rows"])
                   for row in checked(rows, deadline=deadline)):
                continue
            _, collation = self._column_semantics(table, column)
            groups, raw_groups = {}, {}
            null_ids = []
            order = {}
            for index, row in enumerate(checked(rows, deadline=deadline)):
                identity = row["id"]
                order[identity] = index
                value = row["properties"]["data"][column]
                if value is None:
                    null_ids.append(identity)
                    continue
                key = self._attribute_value_key(value, collation)
                group = groups.setdefault(key, {"value": value, "ids": []})
                group["ids"].append(identity)
                raw_key = json.dumps(value, ensure_ascii=False, allow_nan=False)
                raw_groups.setdefault(raw_key, {"value": value, "ids": []})["ids"].append(identity)
            self.attribute_indexes[table, column] = {
                "groups": groups, "raw_groups": raw_groups, "null_ids": null_ids, "order": order,
                "storage_kinds": sorted(state["kinds"]), "row_count": len(rows),
            }

    @staticmethod
    def _attribute_value_key(value, collation):
        # Same storage-class ordering/equality as _compare_collated. Numeric
        # int/float/bool keys compare together; text remains distinct.
        rank = 0 if value is None else 1 if isinstance(value, (int, float)) else 2
        return rank, _collation_key(value, collation)

    def _root_access(self, q: dict):
        table = q["root"]["table"]
        scan = self.tables.get(table, ())
        choices = []
        values_examined = 0
        for item in q.get("filters", []):
            alias, column = item["column"].split(".", 1)
            index = self.attribute_indexes.get((table, column)) if alias == "t0" else None
            if index is None:
                continue
            affinity, collation = self._column_semantics(table, column)
            op = item["op"]
            identities = set()
            if op == "is_null":
                identities.update(index["null_ids"])
            elif op in ("eq", "in"):
                values = item["value"] if op == "in" else [item["value"]]
                for value in values:
                    if value is None:
                        continue
                    values_examined += 1
                    key = self._attribute_value_key(_apply_affinity(value, affinity), collation)
                    identities.update(index["groups"].get(key, {}).get("ids", ()))
            else:
                # Range, contains and non-NULL predicates inspect distinct
                # indexed values, then use their postings rather than rows.
                groups = index["raw_groups"] if op == "contains" else index["groups"]
                for group in checked(groups.values(), deadline=self.overall_deadline):
                    values_examined += 1
                    env = {"t0": {"properties": {"data": {column: group["value"]}}}}
                    if _matches(env, item, affinity, collation):
                        identities.update(group["ids"])
            self._check()
            choices.append((identities, column, op, index))
        if choices:
            identities = set.intersection(*(choice[0] for choice in choices))
            order = choices[0][3]["order"]
            roots = [self.nodes[identity] for identity in sorted(identities, key=order.__getitem__)]
        else:
            roots = scan
        self.last_execution_stats = {
            "physical_plan": {"root_access": "attribute_index" if choices else "table_scan", "root_table": table,
                "attribute_paths": [{"column": column, "operator": op, "storage_kinds": index["storage_kinds"]}
                                    for _, column, op, index in choices]},
            "source_rows_total": len(scan), "source_rows_examined": len(roots),
            "attribute_index_hits": len(choices), "attribute_values_examined": values_examined,
        }
        return roots

    def _column_semantics(self, table: str, column: str) -> tuple[str, str]:
        spec = self.schema.get(table, {})
        # CSV/TXT type labels are descriptive, not SQLite DDL. Raw text/inline
        # scalar storage classes must remain unchanged in graph and oracle.
        affinity = _affinity(spec.get("column_types", {}).get(column, "")) if spec.get("create_sql") or spec.get("sqlite_affinity") else "BLOB"
        return affinity, spec.get("column_collations", {}).get(column, "BINARY").upper()

    def _check(self, rows: list | None = None) -> None:
        _check_overall_deadline(self.overall_deadline)
        if rows is not None and len(rows) > self.max_intermediate_rows:
            raise QueryLimitError(f"Query exceeds {self.max_intermediate_rows} intermediate/result rows")
        if time.perf_counter() > self.deadline:
            raise QueryLimitError(f"Graph query exceeded {self.query_timeout_seconds:g} seconds")

    def _join(self, rows: list[dict], join: dict) -> list[dict]:
        out = []
        neighbors = self.adjacency[(_fk_key(join["foreign_key"]), join["direction"])]
        for env in rows:
            node = env.get(join["from"])
            ids = () if node is None else neighbors.get(node["id"], ())
            if ids:
                if len(out) + len(ids) > self.max_intermediate_rows:
                    raise QueryLimitError(f"Query exceeds {self.max_intermediate_rows} intermediate rows")
                out.extend(dict(env, **{join["alias"]: self.nodes[n]}) for n in ids)
            elif join["kind"] == "left":
                out.append(dict(env, **{join["alias"]: None}))
            if len(out) % 1024 == 0:
                self._check(out)
        self._check(out)
        return out

    def execute(self, q: dict, *, deadline: float | None = None) -> list[list]:
        self.overall_deadline = deadline
        self.deadline = _query_deadline(deadline, self.query_timeout_seconds)
        # Root-only filters commute with every supported inner/left join. Keep
        # the full filters below as a semantic recheck after the indexed access.
        rows = [{"t0": node} for node in self._root_access(q)]
        self._check(rows)
        aliases = {"t0": q["root"]["table"], **{j["alias"]: j["table"] for j in q["joins"]}}

        def semantics(reference: str) -> tuple[str, str]:
            alias, column = reference.split(".", 1)
            return self._column_semantics(aliases[alias], column)
        index = 0
        while index < len(q["joins"]):
            selected = None
            for length in (3, 2):
                chain = q["joins"][index:index + length]
                if len(chain) != length or any(j["kind"] != "inner" for j in chain):
                    continue
                if any(chain[i]["from"] != chain[i - 1]["alias"] for i in range(1, length)):
                    continue
                key = tuple((_fk_key(j["foreign_key"]), j["direction"]) for j in chain)
                if key in self.shortcuts:
                    selected = (chain, self.shortcuts[key])
                    break
            if selected is None:
                rows = self._join(rows, q["joins"][index])
                index += 1
                continue
            chain, shortcuts = selected
            out = []
            for env in rows:
                start = env.get(chain[0]["from"])
                paths = () if start is None else shortcuts.get(start["id"], ())
                if paths:
                    if len(out) + len(paths) > self.max_intermediate_rows:
                        raise QueryLimitError(f"Query exceeds {self.max_intermediate_rows} intermediate rows")
                    self.materialized_hits += 1
                    for ids in paths:
                        out.append(dict(env, **{join["alias"]: self.nodes[ids[i + 1]] for i, join in enumerate(chain)}))
                else:
                    expanded = [env]
                    for join in chain:
                        expanded = self._join(expanded, join)
                    out.extend(expanded)
                self._check(out)
            rows = out
            index += len(chain)
        if q["filters"]:
            rows = [r for r in rows if all(_matches(r, f, *semantics(f["column"])) for f in q["filters"])]
            self._check(rows)
        results = []
        if q["group_by"] or q["aggregations"]:
            groups = defaultdict(list)
            for env in rows:
                groups[tuple(_collation_key(_value(env, c), semantics(c)[1]) for c in q["group_by"])].append(env)
            if not q["group_by"] and not groups:
                groups[()] = []
            for grouped in groups.values():
                result = [_value(grouped[0], p["column"]) for p in q["projection"]]
                for agg in q["aggregations"]:
                    values = ([1] * len(grouped) if agg["column"] == "*" else
                              [v for r in grouped if (v := _value(r, agg["column"])) is not None])
                    function = agg["function"]
                    if function == "count":
                        value = len(values)
                    elif not values:
                        value = None
                    elif function == "sum":
                        value = sum(values)
                    elif function == "avg":
                        value = sum(values) / len(values)
                    else:
                        collation = semantics(agg["column"])[1]
                        ordered = sorted(values, key=functools.cmp_to_key(lambda a, b: _compare_collated(a, b, collation)))
                        value = ordered[0] if function == "min" else ordered[-1]
                    result.append(value)
                results.append(result)
        else:
            results = [[_value(r, p["column"]) for p in q["projection"]] for r in rows]
        names = [p["as"] for p in q["projection"]] + [a["as"] for a in q["aggregations"]]
        if q["limit"] is not None:
            for order in reversed(q["order_by"]):
                column = names.index(order["column"])
                results.sort(key=functools.cmp_to_key(lambda a, b: _compare(a[column], b[column])),
                             reverse=order["direction"] == "desc")
        for order in reversed(q["order_by"]):
            column = names.index(order["column"])
            collation = semantics(q["projection"][column]["column"])[1] if column < len(q["projection"]) else "BINARY"
            results.sort(key=functools.cmp_to_key(lambda a, b: _compare_collated(a[column], b[column], collation)),
                         reverse=order["direction"] == "desc")
        self._check(results)
        return results if q["limit"] is None else results[:q["limit"]]


def _equal_value(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return type(a) is type(b) and a == b


def _equal_bag(actual: list, expected: list) -> bool:
    if len(actual) != len(expected):
        return False
    def compare_rows(a: list, b: list) -> int:
        for x, y in zip(a, b):
            compared = _compare(x, y)
            if compared:
                return compared
        return 0
    actual = sorted(actual, key=functools.cmp_to_key(compare_rows))
    expected = sorted(expected, key=functools.cmp_to_key(compare_rows))
    return all(len(a) == len(e) and all(_equal_value(x, y) for x, y in zip(a, e)) for a, e in zip(actual, expected))


def _equal_results(actual: list, expected: list, q: dict, output_collations: dict | None = None) -> bool:
    if len(actual) != len(expected):
        return False
    if not q["order_by"]:
        return _equal_bag(actual, expected)
    names = [p["as"] for p in q["projection"]] + [a["as"] for a in q["aggregations"]]
    indices = [names.index(o["column"]) for o in q["order_by"]]
    collations = [(output_collations or {}).get(o["column"], "BINARY") for o in q["order_by"]]

    def tied(row: list, key: list) -> bool:
        return all(_compare_collated(row[i], value, collation) == 0
                   for i, value, collation in zip(indices, key, collations))
    # SQL leaves order among tied ORDER BY keys unspecified. Compare each tie
    # group as a multiset while preserving the ordering of distinct groups.
    position = 0
    while position < len(expected):
        key = [expected[position][i] for i in indices]
        stop = position + 1
        while stop < len(expected) and tied(expected[stop], key):
            stop += 1
        if not all(tied(row, key) for row in actual[position:stop]):
            return False
        if not _equal_bag(actual[position:stop], expected[position:stop]):
            return False
        position = stop
    return True


def evaluate_workload(source: Any, graph: dict, queries: list[dict], repeats: int = 3,
                      *, deadline: float | None = None, min_query_seconds: float = 0.003) -> dict:
    """Compare graph answers with source-only SQLite answers for frozen plans.

    ``accuracy`` is [0, 1]; latency_ms is the sum of per-query median graph
    execution times. Each measured sample averages a bounded, adaptive number
    of executions after one warmup; samples target min_query_seconds. Index
    preparation and SQLite oracle times are separate.
    Result previews are bounded; complete results are always compared. ``deadline``
    is an optional absolute ``time.monotonic()`` construction budget. Exceeding
    it raises TimeoutError, so an expired build cannot publish a partial success.
    """
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    _check_overall_deadline(deadline)
    _bound(repeats, 1, 20, "repeats")
    if (isinstance(min_query_seconds, bool) or not isinstance(min_query_seconds, (int, float))
            or not math.isfinite(min_query_seconds) or not 0 <= min_query_seconds <= 1):
        raise ValueError("min_query_seconds must be a finite number between 0 and 1")
    queries = validate_queries(source, queries)
    max_intermediate_rows, query_timeout_seconds = _query_resource_limits(source)
    _check_overall_deadline(deadline)
    started = time.perf_counter()
    executor_error = None
    try:
        executor = GraphExecutor(graph, deadline=deadline, max_intermediate_rows=max_intermediate_rows,
                                 query_timeout_seconds=query_timeout_seconds)
    except ConstructionBudgetExceeded:
        raise
    except Exception as exc:
        executor, executor_error = None, f"{type(exc).__name__}: {exc}"
    preparation_ms = (time.perf_counter() - started) * 1000
    _check_overall_deadline(deadline)
    db = _oracle(source, deadline=deadline)
    results = []
    try:
        _check_overall_deadline(deadline)
        for q in queries:
            _check_overall_deadline(deadline)
            sql, params = _compile_sql(q, source)
            began = time.perf_counter()
            oracle_error, expected = None, []
            query_deadline = _query_deadline(deadline, query_timeout_seconds)
            db.set_progress_handler(lambda: int(time.perf_counter() > query_deadline), 1000)
            try:
                cursor = db.execute(sql, params)
                expected = [list(row) for row in cursor.fetchmany(max_intermediate_rows + 1)]
                if len(expected) > max_intermediate_rows:
                    raise QueryLimitError(f"Oracle exceeds {max_intermediate_rows} result rows")
                _check_overall_deadline(deadline)
                if time.perf_counter() > query_deadline:
                    raise QueryLimitError(f"Oracle query exceeded {query_timeout_seconds:g} seconds")
            except (sqlite3.Error, QueryLimitError, OverflowError) as exc:
                _check_overall_deadline(deadline)
                expected = []
                oracle_error = f"Oracle {type(exc).__name__}: {exc}"
            finally:
                db.set_progress_handler(None, 0)
            oracle_ms = (time.perf_counter() - began) * 1000
            durations, iterations, actual, error = [], [], [], oracle_error or executor_error
            warmup_runs, physical_stats = 0, {}
            hits_before = 0 if executor is None else executor.materialized_hits
            if executor is not None and oracle_error is None:
                try:
                    actual = executor.execute(q, deadline=deadline)
                    warmup_runs = 1
                    # Keep the legacy materialized-path counter scoped to timed
                    # runs; the warmup is reported separately.
                    hits_before = executor.materialized_hits
                    for _ in range(repeats):
                        _check_overall_deadline(deadline)
                        began = time.perf_counter()
                        count, batch, elapsed = 0, 1, 0.0
                        while count < MAX_BENCHMARK_INNER_ITERATIONS:
                            for _ in range(min(batch, MAX_BENCHMARK_INNER_ITERATIONS - count)):
                                _check_overall_deadline(deadline)
                                actual = executor.execute(q, deadline=deadline)
                                count += 1
                            elapsed = time.perf_counter() - began
                            if elapsed >= min_query_seconds or count >= MAX_BENCHMARK_INNER_ITERATIONS:
                                break
                            # Grow the next batch from observed throughput, with
                            # a doubling limit and a hard cap even if the clock
                            # resolution is too coarse for the current query.
                            needed = math.ceil(count * (min_query_seconds - elapsed) / elapsed) if elapsed > 0 else count
                            batch = max(1, min(count * 2, needed))
                        durations.append(elapsed * 1000 / count)
                        iterations.append(count)
                    physical_stats = copy.deepcopy(executor.last_execution_stats)
                except TimeoutError:
                    raise
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
            _check_overall_deadline(deadline)
            aliases = {"t0": q["root"]["table"], **{j["alias"]: j["table"] for j in q["joins"]}}
            collations = {}
            for projection in q["projection"]:
                alias, column = projection["column"].split(".", 1)
                collations[projection["as"]] = _get(source.tables[aliases[alias]], "column_collations", {}).get(column, "BINARY")
            correct = error is None and _equal_results(actual, expected, q, collations)
            results.append({"id": q["id"], "question": q["question"], "correct": correct,
                            "latency_ms": statistics.median(durations) if durations else 0.0,
                            "latency_samples_ms": durations, "timing_iterations": iterations,
                            "measured_query_runs": sum(iterations), "warmup_runs": warmup_runs,
                            "min_query_seconds": min_query_seconds,
                            "oracle_latency_ms": oracle_ms, "expected_count": len(expected),
                            "actual_count": len(actual), "expected_preview": expected[:20],
                            "actual_preview": actual[:20], "error": error,
                            **physical_stats,
                            "materialized_path_hits": 0 if executor is None else executor.materialized_hits - hits_before})
    finally:
        db.close()
    _check_overall_deadline(deadline)
    correct = sum(r["correct"] for r in results)
    return {"accuracy": correct / len(results), "correct": correct, "total": len(results),
            "latency_ms": sum(r["latency_ms"] for r in results), "preparation_ms": preparation_ms,
            "oracle_latency_ms": sum(r["oracle_latency_ms"] for r in results), "repeats": repeats,
            "min_query_seconds": min_query_seconds,
            "max_inner_iterations": MAX_BENCHMARK_INNER_ITERATIONS,
            "results": results}


def generate_workload(source: Any, count: int = 15) -> list[dict]:
    """Create diverse frozen plans grounded in actual tables, FKs and row values.

    Returns exactly count queries or explicitly reports insufficient distinct
    plans. The workload is generated before evolution and remains unchanged.
    A finite generated workload reaching 100% is not a universal correctness proof.
    """
    _bound(count, 1, 100, "count")
    pools = defaultdict(list)
    fingerprints = set()
    root_plan_counts = defaultdict(int)
    base_plan_counts = defaultdict(int)
    per_root_budget = max(12, MAX_GENERATED_PLANS // max(1, len(source.tables)))
    per_root_base_budget = max(4, int(per_root_budget * 0.65))
    domain = str(_get(source, "domain", "业务"))[:160]
    relationship_categories = {"relationship", "multi_hop", "relationship_details", "multi_hop_details", "optional_relationship"}

    def identifier(table: str, column: str) -> bool:
        spec = source.tables[table]
        declared = set(_get(spec, "primary_key", []))
        declared.update(c for fk in _get(spec, "foreign_keys", []) for c in _get(fk, "columns", []))
        lower = column.lower()
        return (column in declared or lower in {"id", "uuid", "guid", "key", "code", "identifier"}
                or lower.endswith(("_id", "_key", "_uuid", "_code"))
                or bool(re.search(r"(?:[a-z]Id|ID)$", column))
                or bool(re.fullmatch(r"(?:user|customer|order|post|owner|account|parent|child|type|region|tenant|session|record|product|category)id", lower)))

    def domain_columns(table: str) -> list[str]:
        columns = list(_get(source.tables[table], "columns", []))
        categorical = ("status", "state", "category", "name", "title", "type", "role", "tag")
        measures = ("score", "amount", "price", "reputation", "rating", "quantity", "count", "cost", "total", "weight", "duration")

        def rank(column: str) -> tuple:
            lower = column.lower()
            business = 0 if any(word in lower for word in categorical) else 1 if any(word in lower for word in measures) else 2
            return identifier(table, column), business, columns.index(column)

        return sorted(columns, key=rank)

    def add(category: str, table: str, question: str, **parts: Any) -> None:
        if len(fingerprints) >= MAX_GENERATED_PLANS or root_plan_counts[table] >= per_root_budget:
            return
        if category not in relationship_categories and base_plan_counts[table] >= per_root_base_budget:
            return
        q = {"id": "pending", "question": f"{domain}场景：{question}", "root": {"table": table, "alias": "t0"},
             "joins": [], "filters": [], "projection": [], "group_by": [], "aggregations": [],
             "order_by": [], "limit": None}
        q.update(parts)
        fingerprint = _semantic_fingerprint(q)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            pools[category].append(q)
            root_plan_counts[table] += 1
            if category not in relationship_categories:
                base_plan_counts[table] += 1

    def project(table: str, alias: str = "t0", maximum: int = 3) -> list[dict]:
        spec = source.tables[table]
        columns = list(dict.fromkeys(list(_get(spec, "primary_key", []))[:1] + domain_columns(table)))[:maximum]
        return [{"column": f"{alias}.{c}", "as": f"{alias}_c{i}"} for i, c in enumerate(columns)]

    count_agg = [{"function": "count", "column": "*", "as": "row_count"}]
    steps = defaultdict(list)
    source_adjacency = {}
    for table, spec in source.tables.items():
        projection = project(table)
        add("inventory", table, f"盘点 {table} 当前所有业务记录的数量。", aggregations=count_agg)
        add("details", table, f"核对 {table} 业务记录的关键字段。", projection=projection)
        for direction in ("asc", "desc"):
            add("ranking", table, f"按关键字段{direction}排列检查 {table} 前10条记录。", projection=projection,
                order_by=[{"column": p["as"], "direction": direction} for p in projection], limit=10)
        for column in domain_columns(table):
            if base_plan_counts[table] >= per_root_base_budget:
                break
            ref = f"t0.{column}"
            values = []
            for row in _get(spec, "rows", []):
                value = row.get(column)
                if value is not None and _scalar(value) and value not in values:
                    values.append(value)
                if len(values) == 3:
                    break
            add("quality", table, f"检查 {table}.{column} 缺失的记录数量。",
                filters=[{"column": ref, "op": "is_null"}], aggregations=count_agg)
            add("aggregation", table, f"统计 {table}.{column} 的有效值数量。",
                aggregations=[{"function": "count", "column": ref, "as": "non_null_count"}])
            if not identifier(table, column):
                add("grouping", table, f"按 {column} 分组统计 {table} 的业务记录分布。",
                    projection=[{"column": ref, "as": "category"}], group_by=[ref], aggregations=count_agg)
            for value in values:
                label = repr(value)[:100]
                add("lookup", table, f"查询 {table} 中 {column}={label} 的具体业务记录。", projection=projection,
                    filters=[{"column": ref, "op": "eq", "value": value}])
                if not identifier(table, column):
                    add("lookup_summary", table, f"统计 {table} 中 {column}={label} 的业务记录数量。",
                        filters=[{"column": ref, "op": "eq", "value": value}], aggregations=count_agg)
            if values:
                for func in ("min", "max"):
                    add("extrema", table, f"核对 {table}.{column} 的{func}值。",
                        aggregations=[{"function": func, "column": ref, "as": f"{func}_value"}])
            all_values = [r.get(column) for r in _get(spec, "rows", []) if r.get(column) is not None]
            if not identifier(table, column) and all_values and all(isinstance(v, (int, float)) for v in all_values):
                for func in ("sum", "avg"):
                    add("numeric", table, f"核对 {table}.{column} 的{func}业务指标。",
                        aggregations=[{"function": func, "column": ref, "as": f"{func}_value"}])
                pivot = values[0]
                for op in ("gt", "lt"):
                    add("range", table, f"筛选 {table}.{column} {op} {pivot!r} 的业务记录。", projection=projection,
                        filters=[{"column": ref, "op": op, "value": pivot}])
        for fk in _get(spec, "foreign_keys", []):
            f = _fk(fk, table)
            steps[table].append((f, "forward", f["ref_table"]))
            steps[f["ref_table"]].append((f, "reverse", table))
            # Workload *selection* uses source FK row-index pairs, never a graph
            # execution or an oracle answer. SQL imports retain their original
            # affinity/collation comparisons in the source's immutable FK cache.
            if hasattr(source, "_fk_pairs") and not isinstance(fk, dict):
                from graph_engine.construction.sources import foreign_key_pairs
                pairs = foreign_key_pairs(source, spec, fk)
            else:
                parents = defaultdict(list)
                for index, row in enumerate(_get(source.tables[f["ref_table"]], "rows", [])):
                    values = tuple(row.get(c) for c in f["ref_columns"])
                    if all(v is not None for v in values):
                        parents[values].append(index)
                pairs = []
                for index, row in enumerate(_get(spec, "rows", [])):
                    values = tuple(row.get(c) for c in f["columns"])
                    if all(v is not None and v != "" for v in values):
                        pairs.extend((index, parent) for parent in parents.get(values, ()))
            forward, reverse = defaultdict(set), defaultdict(set)
            for child, parent in pairs:
                forward[child].add(parent)
                reverse[parent].add(child)
            source_adjacency[(_fk_key(f), "forward")] = forward
            source_adjacency[(_fk_key(f), "reverse")] = reverse
    for root in source.tables:
        def walk(table: str, joins: list[dict], seen: set[tuple], reachable: set[int]) -> None:
            if len(joins) == 3 or len(fingerprints) >= MAX_GENERATED_PLANS or root_plan_counts[root] >= per_root_budget:
                return
            for f, direction, target in steps[table]:
                token = (_fk_key(f), direction)
                # Avoid immediate backtracking and repeated edges in generated
                # business paths, while the validator accepts explicit cycles.
                if token in seen or (joins and _fk_key(joins[-1]["foreign_key"]) == _fk_key(f)):
                    continue
                alias = f"t{len(joins) + 1}"
                join = {"from": f"t{len(joins)}", "alias": alias, "table": target,
                        "direction": direction, "kind": "inner", "foreign_key": f}
                path = joins + [join]
                desc = " → ".join([root] + [j["table"] for j in path])
                adjacency = source_adjacency[token]
                reached = {neighbor for index in reachable for neighbor in adjacency.get(index, ())}
                if reached:
                    add("relationship" if len(path) == 1 else "multi_hop", root,
                        f"统计业务链路 {desc} 中有效关联组合的数量。", joins=path, aggregations=count_agg)
                    add("relationship_details" if len(path) == 1 else "multi_hop_details", root,
                        f"沿业务链路 {desc} 追溯起点和终点的关键记录。", joins=path,
                        projection=project(root, maximum=1) + project(target, alias, maximum=2))
                if len(path) == 1:
                    left = copy.deepcopy(path)
                    left[0]["kind"] = "left"
                    add("optional_relationship", root, f"检查 {root} 到 {target} 的关联，保留没有匹配的业务记录。",
                        joins=left, projection=project(root, maximum=1) + project(target, alias, maximum=2))
                if reached:
                    walk(target, path, seen | {token}, reached)
        walk(root, [], set(), set(range(len(_get(source.tables[root], "rows", [])))))
    # Root-table coverage takes precedence over category rotation. Once every
    # available root has appeared, balance the number of plans per root while
    # continuing the category rotation. Wide alphabetical first tables cannot
    # consume the entire workload or the bounded candidate-generation budget.
    priority = ["inventory", "relationship", "multi_hop", "lookup", "numeric", "grouping", "quality",
                "relationship_details", "multi_hop_details", "optional_relationship", "ranking", "range",
                "lookup_summary", "extrema", "aggregation", "details"]
    selected = []
    root_selected = defaultdict(int)
    cursor = 0
    while len(selected) < count:
        available_roots = {q["root"]["table"] for pool in pools.values() for q in pool}
        if not available_roots:
            raise ValueError(f"Requested {count} queries but only {len(selected)} distinct grounded plans are available")
        minimum = min(root_selected[table] for table in available_roots)
        for offset in range(len(priority)):
            position = (cursor + offset) % len(priority)
            category = priority[position]
            index = next((i for i, q in enumerate(pools[category]) if root_selected[q["root"]["table"]] == minimum), None)
            if index is not None:
                chosen = pools[category].pop(index)
                selected.append(chosen)
                root_selected[chosen["root"]["table"]] += 1
                cursor = (position + 1) % len(priority)
                break
    for i, q in enumerate(selected):
        q["id"] = f"q{i + 1:03d}"
    return validate_queries(source, selected, count)


__all__ = ["GraphExecutor", "generate_workload", "validate_queries", "evaluate_workload"]
