#!/usr/bin/env python3
"""Export a bounded, FK-closed sample of a GalaxyWeaver parquet dataset.

Requires PyArrow and PyYAML. Reads metadata.yaml and parquet files only; never
connects to or modifies a source database. The resulting SQL is SQLite-compatible.
See --help for limits. Missing *tables* may become explicitly reported key-only
virtual entities; missing rows of real tables are never fabricated.
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any


class SampleError(ValueError):
    pass


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sql_value(value: Any) -> Any:
    """Use lossless scalar encodings; document date/decimal/nested encodings."""
    if value is None or isinstance(value, (str, bytes, int)):
        if isinstance(value, str) and "\x00" in value:
            raise SampleError("A sampled string contains NUL; cannot emit it as a SQL text literal.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SampleError("A sampled value is non-finite; it was not silently replaced with NULL.")
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=sql_value, allow_nan=False)
    raise SampleError(f"Unsupported sampled scalar type: {type(value).__name__}")


def literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, (bool, int)):
        return str(int(value))
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def affinity(arrow_type: Any, pa: Any) -> str:
    if pa.types.is_integer(arrow_type) or pa.types.is_boolean(arrow_type):
        return "INTEGER"
    if pa.types.is_floating(arrow_type):
        return "REAL"
    if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return "BLOB"
    return "TEXT"


class ParquetRows:
    """Read only key columns while locating rows, then stream matching groups.

    Parquet cannot promise random row access: matching row groups may require
    decoding preceding batches. Both key scans and property decoding have hard
    row budgets, and their actual counts are recorded in the manifest.
    """
    def __init__(self, path: Path, batch_size: int, key_budget: int, property_budget: int):
        import pyarrow.parquet as pq
        self.file = pq.ParquetFile(path)
        self.path = path
        self.batch_size = batch_size
        self.key_budget = key_budget
        self.property_budget = property_budget
        self.key_rows_scanned = 0
        self.property_rows_decoded = 0
        self.physical_rows = self.file.metadata.num_rows

    def seeds(self, limit: int):
        emitted = 0
        for batch in self.file.iter_batches(batch_size=min(limit, self.batch_size)):
            self._charge_properties(batch.num_rows)
            for row in batch.to_pylist():
                if emitted >= limit:
                    return
                yield emitted, row
                emitted += 1
            if emitted >= limit:
                return

    def _charge_properties(self, count):
        self.property_rows_decoded += count
        if self.property_rows_decoded > self.property_budget:
            raise SampleError(f"Property decoding row budget exhausted for {self.path.name}; "
                              "reduce seed rows or explicitly increase --max-property-rows.")

    def lookup(self, key_columns: tuple[str, ...], wanted: set[tuple]):
        matches: dict[int, list[int]] = {}
        found_keys: set[tuple] = set()
        group_starts = []
        absolute = 0
        # A complete key-only scan also detects duplicate referenced primary keys.
        for group in range(self.file.num_row_groups):
            group_starts.append(absolute)
            offset = 0
            for batch in self.file.iter_batches(row_groups=[group], columns=list(key_columns),
                                                batch_size=self.batch_size):
                self.key_rows_scanned += batch.num_rows
                if self.key_rows_scanned > self.key_budget:
                    raise SampleError(f"Key scan row budget exhausted for {self.path.name}; "
                                      "explicitly increase --max-key-scan-rows if appropriate.")
                for index, row in enumerate(batch.to_pylist()):
                    key = tuple(sql_value(row[column]) for column in key_columns)
                    if key in wanted:
                        if key in found_keys:
                            raise SampleError(f"Ambiguous duplicate referenced key in {self.path.name}: {key!r}")
                        found_keys.add(key)
                        matches.setdefault(group, []).append(offset + index)
                offset += batch.num_rows
            absolute += self.file.metadata.row_group(group).num_rows
        for group, offsets in matches.items():
            desired = set(offsets)
            offset = 0
            last = max(offsets)
            for batch in self.file.iter_batches(row_groups=[group], batch_size=self.batch_size):
                self._charge_properties(batch.num_rows)
                selected = [index for index in range(batch.num_rows) if offset + index in desired]
                if selected:
                    import pyarrow as pa
                    for index, row in zip(selected, batch.take(pa.array(selected)).to_pylist()):
                        yield group_starts[group] + offset + index, row
                offset += batch.num_rows
                if offset > last:
                    break


def prepare(source_dir: Path, *, seed_rows: int = 20, max_rows: int = 5000,
            max_rounds: int = 20, batch_size: int = 4096,
            max_key_scan_rows: int = 50_000_000, max_property_rows: int = 5_000_000,
            missing_reference_policy: str = "prune", allow_virtual: bool = True):
    import pyarrow as pa
    import yaml

    if any(type(value) is not int or value < 1 for value in (
        seed_rows, max_rows, max_rounds, batch_size, max_key_scan_rows, max_property_rows
    )):
        raise SampleError("All sampling and scanning limits must be positive integers.")
    if missing_reference_policy not in {"prune", "error"}:
        raise SampleError("missing_reference_policy must be prune or error.")
    source_dir = source_dir.resolve()
    metadata_path = source_dir / "metadata.yaml"
    metadata_bytes = metadata_path.read_bytes()
    metadata = yaml.safe_load(metadata_bytes)
    specs = metadata.get("tables") if isinstance(metadata, dict) else None
    if not isinstance(specs, list) or not specs:
        raise SampleError("metadata.yaml must contain a nonempty tables list.")
    tables = {}
    for spec in specs:
        name = spec["name"]
        if not isinstance(name, str) or not name or name in tables:
            raise SampleError("Table names must be nonempty and unique.")
        columns = spec.get("columns", [])
        pk = tuple(column["name"] for column in columns if column.get("dtype") == "primary_key")
        fks = []
        for column in columns:
            if column.get("dtype") == "foreign_key":
                link = column.get("link_to")
                if not isinstance(link, str) or "." not in link:
                    raise SampleError(f"Missing explicit foreign-key target for {name}.{column['name']}.")
                target, target_column = link.rsplit(".", 1)
                fks.append((column["name"], target, target_column))
        source = spec.get("source")
        reader = None
        if source:
            path = (source_dir / source).resolve()
            if not path.is_relative_to(source_dir) or not path.is_file():
                raise SampleError(f"Missing or escaping source path for real table {name}: {source}")
            if spec.get("format", "parquet") != "parquet":
                raise SampleError(f"Only parquet table sources are supported: {name}")
            reader = ParquetRows(path, batch_size, max_key_scan_rows, max_property_rows)
            sql_types = {field.name: affinity(field.type, pa) for field in reader.file.schema_arrow}
            if set(column["name"] for column in columns) - sql_types.keys():
                raise SampleError(f"Metadata columns are absent from parquet table {name}.")
        else:
            if not allow_virtual:
                raise SampleError(f"Table {name} has no physical source; virtual entities disabled.")
            if len(pk) != 1 or fks or {column["name"] for column in columns} != set(pk):
                raise SampleError(f"Source-less {name} must declare only one primary-key column.")
            sql_types = {pk[0]: "TEXT"}
        tables[name] = {"name": name, "pk": pk, "fks": fks, "types": sql_types,
                        "reader": reader, "rows": {}, "virtual": reader is None,
                        "seed_count": 0, "closure_count": 0, "dropped": [], "unavailable": set()}

    # A missing referenced entity type is represented solely by distinct observed
    # FK values. It receives no invented business attributes.
    for table in list(tables.values()):
        for column, target, target_column in table["fks"]:
            if target not in tables:
                if not allow_virtual:
                    raise SampleError(f"Referenced table {target} is absent from metadata.")
                tables[target] = {"name": target, "pk": (target_column,), "fks": [],
                                  "types": {target_column: table["types"][column]}, "reader": None,
                                  "rows": {}, "virtual": True, "seed_count": 0, "closure_count": 0,
                                  "dropped": [], "unavailable": set()}
            if tables[target]["pk"] != (target_column,):
                raise SampleError(f"FK {table['name']}.{column} must reference a single declared PK; "
                                  "metadata's per-column links cannot express a composite FK.")
            if tables[target]["virtual"]:
                previous = tables[target].get("fk_affinity")
                current = table["types"][column]
                if previous is not None and previous != current:
                    raise SampleError(f"Conflicting source affinities for virtual entity {target}.")
                tables[target]["fk_affinity"] = current
                tables[target]["types"][target_column] = current

    def add_row(table, physical_index, row, *, seed=False):
        row = {column: sql_value(row[column]) for column in table["types"]}
        key = tuple(row[column] for column in table["pk"]) if table["pk"] else (physical_index,)
        if table["pk"] and any(value is None for value in key):
            raise SampleError(f"NULL primary key in real row of {table['name']}.")
        existing = table["rows"].get(key)
        if existing is not None:
            if existing[0] != physical_index:
                raise SampleError(f"Duplicate sampled primary key in {table['name']}: {key!r}")
            return
        if sum(len(item["rows"]) for item in tables.values()) >= max_rows:
            raise SampleError("FK closure exceeds --max-rows; reduce --seed-rows or increase the bound.")
        table["rows"][key] = (physical_index, row)
        table["seed_count" if seed else "closure_count"] += 1

    for table in tables.values():
        if table["reader"]:
            for index, row in table["reader"].seeds(seed_rows):
                add_row(table, index, row, seed=True)

    def missing_keys():
        pending = {name: set() for name in tables}
        for table in tables.values():
            for _, row in table["rows"].values():
                for column, target, _ in table["fks"]:
                    key = (row[column],)
                    if row[column] is not None and key not in tables[target]["rows"]:
                        if key not in tables[target]["unavailable"]:
                            pending[target].add(key)
        return {name: keys for name, keys in pending.items() if keys}

    rounds = 0
    while pending := missing_keys():
        rounds += 1
        if rounds > max_rounds:
            raise SampleError("Recursive FK closure exceeds --max-rounds; no incomplete sample exported.")
        for name, keys in pending.items():
            table = tables[name]
            if table["virtual"]:
                for key in sorted(keys, key=repr):
                    add_row(table, None, {table["pk"][0]: key[0]})
            else:
                for index, row in table["reader"].lookup(table["pk"], keys):
                    add_row(table, index, row)
                unavailable = keys - table["rows"].keys()
                if unavailable and missing_reference_policy == "error":
                    raise SampleError(f"{len(unavailable)} referenced keys are absent from real table {name}.")
                table["unavailable"].update(unavailable)

    # Prune entire original rows rather than clearing/changing invalid FK values.
    # Repeat because removing a referenced row can invalidate another sampled row.
    changed = True
    while changed:
        changed = False
        for table in tables.values():
            for key, (index, row) in list(table["rows"].items()):
                broken = [f"{column}->{target}.{target_column}" for column, target, target_column in table["fks"]
                          if row[column] is not None and (row[column],) not in tables[target]["rows"]]
                if broken:
                    table["dropped"].append({"physical_row_index": index, "key": list(key),
                                              "reason": "missing_referenced_real_row", "foreign_keys": broken})
                    del table["rows"][key]
                    changed = True
    # A virtual entity exists only to close a surviving sampled FK, never to
    # imply an extra source record after the originating real row was pruned.
    referenced_virtual = {name: set() for name, table in tables.items() if table["virtual"]}
    for table in tables.values():
        for _, row in table["rows"].values():
            for column, target, _ in table["fks"]:
                if target in referenced_virtual and row[column] is not None:
                    referenced_virtual[target].add((row[column],))
    for name, referenced in referenced_virtual.items():
        table = tables[name]
        for key in list(table["rows"]):
            if key not in referenced:
                del table["rows"][key]
                table["dropped"].append({"physical_row_index": None, "key": list(key),
                                          "reason": "no_surviving_sampled_foreign_key", "foreign_keys": []})
    if not any(table["rows"] for table in tables.values()):
        raise SampleError("No rows remain after referential-integrity pruning.")

    lines = ["-- BOUNDED SAMPLE, NOT THE FULL DATASET. See the companion manifest for provenance."]
    conn = sqlite3.connect(":memory:")
    try:
        for table in tables.values():
            definitions = [f"{quote(column)} {kind}" for column, kind in table["types"].items()]
            if table["pk"]:
                definitions.append("PRIMARY KEY (" + ", ".join(map(quote, table["pk"])) + ")")
            definitions.extend(f"FOREIGN KEY ({quote(column)}) REFERENCES {quote(target)} ({quote(target_column)})"
                               for column, target, target_column in table["fks"])
            statement = f"CREATE TABLE {quote(table['name'])} ({', '.join(definitions)});"
            conn.execute(statement)
            lines.append(statement)
        for table in tables.values():
            columns = list(table["types"])
            prefix = f"INSERT INTO {quote(table['name'])} ({', '.join(map(quote, columns))}) VALUES "
            for _, row in table["rows"].values():
                values = [row[column] for column in columns]
                conn.execute(prefix + "(" + ",".join("?" for _ in columns) + ")", values)
                lines.append(prefix + "(" + ", ".join(map(literal, values)) + ");")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise SampleError(f"SQLite referential-integrity verification failed: {problems[:3]!r}")
    finally:
        conn.close()
    sql = "\n".join(lines) + "\n"
    manifest = {
        "format": "igraph.construction_benchmark_sample.v1", "dataset_name": metadata.get("dataset_name"),
        "scope": "bounded_fk_closed_sample_not_full_dataset", "sample_method": "deterministic_first_rows_per_table_then_fk_closure",
        "source_directory": str(source_dir), "metadata_path": str(metadata_path),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "limits": {"seed_rows_per_real_table": seed_rows, "max_total_rows": max_rows,
                   "max_closure_rounds": max_rounds, "batch_size": batch_size,
                   "max_key_scan_rows_per_table": max_key_scan_rows,
                   "max_property_rows_decoded_per_table": max_property_rows},
        "closure_rounds": rounds, "missing_reference_policy": missing_reference_policy,
        "sqlite_foreign_key_check": "passed", "source_task_labels_included": False,
        "encodings": {"date_time": "ISO 8601 text", "decimal": "exact decimal text",
                      "nested_values": "JSON text", "binary": "SQLite BLOB", "source_attributes_truncated": False},
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "total_sample_rows": sum(len(table["rows"]) for table in tables.values()), "tables": [],
    }
    for table in tables.values():
        reader = table["reader"]
        stat = reader.path.stat() if reader else None
        manifest["tables"].append({
            "table": table["name"], "virtual": table["virtual"],
            "virtual_reason": "key_only_entity_from_distinct_sampled_foreign_key_values" if table["virtual"] else None,
            "source_path": str(reader.path) if reader else None, "source_bytes": stat.st_size if stat else None,
            "source_mtime_ns": stat.st_mtime_ns if stat else None,
            "source_total_rows": reader.physical_rows if reader else None,
            "primary_key": list(table["pk"]), "columns": table["types"],
            "foreign_keys": [{"column": column, "target_table": target, "target_column": target_column}
                             for column, target, target_column in table["fks"]],
            "seed_rows_selected": table["seed_count"], "rows_added_by_fk_closure": table["closure_count"],
            "sample_rows": len(table["rows"]), "dropped_rows": table["dropped"],
            "source_physical_row_indices": [index for index, _ in table["rows"].values()] if reader else [],
            "missing_reference_keys": [list(key) for key in sorted(table["unavailable"], key=repr)],
            "key_rows_scanned": reader.key_rows_scanned if reader else 0,
            "property_rows_decoded": reader.property_rows_decoded if reader else 0,
        })
    return sql, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path, help="Dataset directory containing metadata.yaml")
    parser.add_argument("--output", required=True, type=Path, help="Destination .sql file")
    parser.add_argument("--manifest", type=Path, help="Default: OUTPUT.manifest.json")
    parser.add_argument("--seed-rows", type=int, default=20, help="Initial rows per real table (default: 20)")
    parser.add_argument("--max-rows", type=int, default=5000, help="Total output row limit including closure")
    parser.add_argument("--max-rounds", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=4096)
    parser.add_argument("--max-key-scan-rows", type=int, default=50_000_000, help="Cumulative per-table key-only scan limit")
    parser.add_argument("--max-property-rows", type=int, default=5_000_000, help="Cumulative per-table decoded full-row limit")
    parser.add_argument("--max-sql-bytes", type=int, default=32_000_000)
    parser.add_argument("--missing-reference-policy", choices=["prune", "error"], default="prune",
                        help="Prune complete rows with absent real FK targets, or fail; never fabricate attributes")
    parser.add_argument("--no-virtual-entities", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    source_dir = args.source_dir.resolve()
    destinations = [args.output.resolve(), manifest_path.resolve()]
    if len(set(destinations)) != 2 or any(path.is_relative_to(source_dir) for path in destinations):
        parser.error("Output SQL and manifest must be distinct files outside the source dataset directory.")
    if not args.overwrite and any(path.exists() for path in destinations):
        parser.error("Output already exists; use --overwrite to replace generated artifacts.")
    try:
        sql, manifest = prepare(args.source_dir, seed_rows=args.seed_rows, max_rows=args.max_rows,
            max_rounds=args.max_rounds, batch_size=args.batch_size, max_key_scan_rows=args.max_key_scan_rows,
            max_property_rows=args.max_property_rows, missing_reference_policy=args.missing_reference_policy,
            allow_virtual=not args.no_virtual_entities)
        if args.max_sql_bytes < 1 or len(sql.encode("utf-8")) > args.max_sql_bytes:
            raise SampleError("SQL byte limit exceeded; no truncated output was exported.")
        manifest["limits"]["max_sql_bytes"] = args.max_sql_bytes
        for path in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(sql, encoding="utf-8", newline="\n")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (SampleError, ImportError, OSError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f"Benchmark export failed: {exc}\n")
    print(json.dumps({"sql": str(args.output.resolve()), "manifest": str(manifest_path.resolve()),
                      "scope": manifest["scope"], "sample_rows": manifest["total_sample_rows"],
                      "virtual_tables": [table["table"] for table in manifest["tables"] if table["virtual"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
