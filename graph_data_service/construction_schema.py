"""Expose only structural source metadata from the private construction report."""
from __future__ import annotations

import json
from pathlib import Path


def source_schema_tables(directory: Path, fallback: object = None) -> list[dict]:
    try:
        report = json.loads((directory / "evolution-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    source = report.get("source_schema") if isinstance(report, dict) else None
    tables = source.get("tables") if isinstance(source, dict) else None
    if not isinstance(tables, list) or not tables:
        tables = fallback
    if not isinstance(tables, list):
        return []
    result = []
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            continue
        columns = table.get("columns")
        if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
            continue
        keys = table.get("primary_key", [])
        types = table.get("column_types", {})
        foreign_keys = []
        for key in table.get("foreign_keys", []) if isinstance(table.get("foreign_keys", []), list) else []:
            if (not isinstance(key, dict) or not isinstance(key.get("ref_table"), str)
                    or not isinstance(key.get("columns"), list) or not isinstance(key.get("ref_columns"), list)
                    or not all(isinstance(value, str) for value in key["columns"] + key["ref_columns"])):
                continue
            foreign_keys.append({field: key[field] for field in ("columns", "ref_table", "ref_columns")})
        result.append({
            "name": table["name"], "columns": columns,
            "column_types": {column: types[column] for column in columns
                             if isinstance(types, dict) and isinstance(types.get(column), str)},
            "primary_key": [key for key in keys if isinstance(key, str) and key in columns] if isinstance(keys, list) else [],
            "foreign_keys": foreign_keys,
        })
    return result
