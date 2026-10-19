"""Bounded relational input and lossless PK/FK graph construction.

SQL input is a SQLite database image or a conservative SQLite DDL/INSERT dump.
Bounded images and SQL text are opened in memory; explicit full-scale images are
opened as locked read-only files. CSV and TXT values remain strings; identifiers
are never case-folded. TXT accepts strict delimited tables or lossless line records.
``options.tables`` may append inline tables to an uploaded CSV or TXT source;
external table paths are deliberately unsupported.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .budget import checked, check_deadline, current_deadline


class SourceError(ValueError):
    """The uploaded source cannot be imported completely and safely."""


class CsvConfigurationError(SourceError):
    """CSV node/edge roles or endpoint references require user configuration."""


@dataclass(frozen=True)
class ResourceLimits:
    """Explicit resource envelope carried with a relational source."""

    max_source_bytes: int
    max_source_rows: int
    source_timeout_seconds: int
    max_graph_nodes: int
    max_graph_edges: int
    max_intermediate_rows: int
    query_timeout_seconds: int


_BOUNDED_LIMITS = ResourceLimits(
    max_source_bytes=32 * 1024 * 1024,
    max_source_rows=200_000,
    source_timeout_seconds=10,
    max_graph_nodes=200_000,
    max_graph_edges=1_000_000,
    max_intermediate_rows=200_000,
    query_timeout_seconds=10,
)

_BOUNDED_CEILINGS = ResourceLimits(
    max_source_bytes=64 * 1024 * 1024,
    max_source_rows=200_000,
    source_timeout_seconds=60,
    max_graph_nodes=200_000,
    max_graph_edges=1_000_000,
    max_intermediate_rows=200_000,
    query_timeout_seconds=10,
)

_FULL_SCALE_LIMITS = ResourceLimits(
    max_source_bytes=8 * 1024 * 1024 * 1024,
    max_source_rows=10_000_000,
    source_timeout_seconds=7_200,
    max_graph_nodes=15_000_000,
    max_graph_edges=30_000_000,
    max_intermediate_rows=10_000_000,
    query_timeout_seconds=600,
)


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[dict]
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    column_types: dict[str, str] = field(default_factory=dict)
    column_collations: dict[str, str] = field(default_factory=dict)
    create_sql: str = ""


@dataclass
class RelationalSource:
    tables: dict[str, Table]
    domain: str = ""
    warnings: list[str] = field(default_factory=list)
    resource_limits: ResourceLimits = field(default_factory=lambda: _BOUNDED_LIMITS)
    _connection: sqlite3.Connection | None = field(default=None, repr=False)
    _fk_pairs: dict[str, list[tuple[int, int]]] = field(default_factory=dict, repr=False)
    _sql_rowids: dict[str, dict[int, int]] = field(default_factory=dict, repr=False)
    _sql_rowid_aliases: dict[str, str] = field(default_factory=dict, repr=False)
    _database_path: Path | None = field(default=None, repr=False)
    _database_identity: tuple | None = field(default=None, repr=False)
    _txt_mode: str | None = field(default=None, repr=False)
    _csv_mode: bool = field(default=False, repr=False)

    def schema(self) -> dict:
        return {"domain": self.domain, "tables": [
            {"name": table.name, "columns": table.columns.copy(),
             "column_types": table.column_types.copy(),
             "column_collations": table.column_collations.copy(),
             "create_sql": table.create_sql,
             "primary_key": table.primary_key.copy(),
             "foreign_keys": [asdict(fk) for fk in table.foreign_keys],
             "row_count": len(table.rows)} for table in self.tables.values()]}

    def samples(self, limit: int = 3) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
            raise SourceError("Sample limit must be an integer between 0 and 100.")
        return {name: copy.deepcopy(table.rows[:limit]) for name, table in self.tables.items()}

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._database_path = None
        self._database_identity = None


def _database_file_identity(path: Path) -> tuple:
    """Identify the database plus committed rollback/WAL state without reading rows."""
    def identity(candidate: Path):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            return None
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    return identity(path), identity(Path(str(path) + "-wal")), identity(Path(str(path) + "-journal"))


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _hash(prefix: str, *parts: Any) -> str:
    return prefix + hashlib.sha256(_encode(parts).encode("utf-8")).hexdigest()[:40]


def row_id(table: Table, row: dict, index: int) -> str:
    """PK identity survives row reordering; keyless rows use their source position."""
    return _hash("row_", table.name, "pk" if table.primary_key else "position",
                 [row[column] for column in table.primary_key] if table.primary_key else index)


def foreign_key_type(table: Table, fk: ForeignKey) -> str:
    return _hash("fk_", table.name, fk.columns, fk.ref_table, fk.ref_columns)


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value:
        raise SourceError(f"{label} must be a nonempty identifier of at most 1024 characters.")
    return value


def _names(value: Any, label: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty):
        raise SourceError(f"{label} must be an array of column names.")
    result = [_name(item, label) for item in value]
    if len(result) != len(set(result)):
        raise SourceError(f"{label} contains duplicate column names.")
    return result


def _foreign_keys(value: Any) -> list[ForeignKey]:
    if not isinstance(value, list) or len(value) > 128:
        raise SourceError("foreign_keys must be an array of at most 128 definitions.")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"columns", "ref_table", "ref_columns"}:
            raise SourceError("A foreign key requires exactly columns, ref_table and ref_columns.")
        fk = ForeignKey(_names(item["columns"], "Foreign key columns"),
                        _name(item["ref_table"], "Referenced table"),
                        _names(item["ref_columns"], "Referenced columns"))
        if len(fk.columns) != len(fk.ref_columns):
            raise SourceError("Foreign key and referenced column counts differ.")
        if fk in result:
            raise SourceError("Duplicate foreign key definition.")
        result.append(fk)
    return result


def _limit(options: dict, name: str, default: int, maximum: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise SourceError(f"{name} must be an integer between 1 and {maximum}.")
    return value


def _resource_limits(options: dict) -> ResourceLimits:
    profile = options.get("resource_profile", "bounded")
    if type(profile) is not str or profile not in {"bounded", "full_scale"}:
        raise SourceError("resource_profile must be bounded or full_scale.")
    defaults = _FULL_SCALE_LIMITS if profile == "full_scale" else _BOUNDED_LIMITS
    ceiling = _FULL_SCALE_LIMITS if profile == "full_scale" else _BOUNDED_CEILINGS
    values = {
        name: _limit(options, name, getattr(defaults, name), getattr(ceiling, name))
        for name in ResourceLimits.__dataclass_fields__
    }
    return ResourceLimits(**values)


def _valid_json_cell(value: Any) -> bool:
    return (value is None or isinstance(value, (str, bool, int)) or
            isinstance(value, float) and math.isfinite(value))


def _validate_source(source: RelationalSource, max_rows: int, *, csv_mode: bool = False,
                     warn_missing_primary_key: bool = True) -> None:
    if not source.tables:
        raise SourceError("The source contains no tables.")
    if len(source.tables) > 128:
        raise SourceError("The source exceeds the 128-table limit.")
    if sum(len(table.rows) for table in source.tables.values()) > max_rows:
        raise SourceError(f"The source exceeds the {max_rows}-row limit.")
    for table in source.tables.values():
        _name(table.name, "Table name")
        _names(table.columns, "Table columns")
        if len(table.columns) > 256:
            raise SourceError(f"Table {table.name!r} exceeds the 256-column limit.")
        for key in table.primary_key:
            if key not in table.columns:
                raise SourceError(f"Unknown primary key column {table.name}.{key}.")
        keys = set()
        for row in checked(table.rows):
            if not isinstance(row, dict) or set(row) != set(table.columns):
                raise SourceError(f"Rows in {table.name!r} must have exactly its declared columns.")
            if not all(_valid_json_cell(value) for value in row.values()):
                raise SourceError("Only finite scalar JSON cells are supported; BLOB/nested values need explicit preprocessing.")
            if table.primary_key:
                values = [row[column] for column in table.primary_key]
                if any(value is None or csv_mode and value == "" for value in values):
                    raise SourceError(f"Primary key of {table.name!r} contains a missing value.")
                key = _encode(values)
                if key in keys:
                    raise SourceError(f"Primary key of {table.name!r} is not unique.")
                keys.add(key)
        for fk in table.foreign_keys:
            if fk.ref_table not in source.tables:
                raise SourceError(f"Unknown referenced table {fk.ref_table!r}.")
            parent = source.tables[fk.ref_table]
            if not fk.columns or len(fk.columns) != len(fk.ref_columns):
                raise SourceError(f"Invalid foreign key in {table.name!r}.")
            if any(column not in table.columns for column in fk.columns):
                raise SourceError(f"Unknown foreign key column in {table.name!r}.")
            if any(column not in parent.columns for column in fk.ref_columns):
                raise SourceError(f"Unknown referenced column in {fk.ref_table!r}.")
        if warn_missing_primary_key and not table.primary_key and not source._csv_mode:
            source.warnings.append(f"Table {table.name!r} has no reliable primary key; row-position identities are used.")


def _infer_primary_key(table: Table) -> list[str]:
    # Conservative, exact-name rule: uniqueness alone is not evidence of identity.
    singular = table.name[:-1] if table.name.endswith("s") else table.name
    for column in ("id", table.name + "_id", singular + "_id"):
        if column not in table.columns or not table.rows:
            continue
        if any(column not in row for row in table.rows):
            return []  # Full row-shape validation below reports the missing cell.
        values = [row[column] for row in checked(table.rows)]
        if not all(_valid_json_cell(value) for value in values):
            return []
        if all(value is not None and value != "" for value in values) and len({_encode(v) for v in values}) == len(values):
            return [column]
    return []


def _decode_text(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError(f"{label} must use UTF-8 encoding.") from exc


def _delimiter(value: Any, label: str) -> str:
    delimiter = value
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\x00":
        raise SourceError(f"{label} delimiter must be one character.")
    return delimiter


def _delimited_rows(content: str, delimiter: str, label: str, max_rows: int) -> tuple[list[str], list[dict]]:
    try:
        reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
        columns = next(reader, None)
        if columns is None:
            raise SourceError(f"{label} is empty and has no header.")
        _names(columns, f"{label} headers")
        rows = []
        for line, cells in enumerate(checked(reader), 2):
            if len(cells) != len(columns):
                raise SourceError(f"{label} row {line} has {len(cells)} cells; expected {len(columns)}.")
            rows.append(dict(zip(columns, cells)))
            if len(rows) > max_rows:
                raise SourceError(f"The source exceeds the {max_rows}-row limit.")
    except csv.Error as exc:
        raise SourceError(f"Invalid {label}: {exc}") from exc
    return columns, rows


def _string_table_source(path: Path, options: dict, limits: ResourceLimits,
                         columns: list[str], rows: list[dict], warning: str = "",
                         *, warn_missing_primary_key: bool = True) -> RelationalSource:
    max_rows = limits.max_source_rows
    csv_mode = path.suffix.lower() == ".csv"
    name = _name(options.get("table_name", path.stem), "Table name")
    table = Table(name, columns, rows, column_types={column: "TEXT" for column in columns},
                  column_collations={column: "BINARY" for column in columns})
    table.primary_key = (_names(options["primary_key"], "primary_key", empty=True)
                         if "primary_key" in options else [] if csv_mode else _infer_primary_key(table))
    table.foreign_keys = _foreign_keys(options.get("foreign_keys", []))
    source = RelationalSource({name: table}, options.get("domain", ""), resource_limits=limits,
                              _csv_mode=csv_mode)
    if warning:
        source.warnings.append(warning)
    inline = options.get("tables", [])
    if not isinstance(inline, list) or len(inline) > 127:
        raise SourceError("tables must be an array of at most 127 inline table definitions.")
    for definition in inline:
        allowed = {"name", "columns", "rows", "primary_key", "foreign_keys", "column_types"}
        if not isinstance(definition, dict) or set(definition) - allowed:
            raise SourceError("Inline tables support name, columns, rows, primary_key, foreign_keys and column_types only; paths are unsupported.")
        name = _name(definition.get("name"), "Inline table name")
        if name in source.tables:
            raise SourceError(f"Duplicate table name {name!r}.")
        rows = definition.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise SourceError("Inline table rows must be an array of row objects.")
        if sum(len(table.rows) for table in source.tables.values()) + len(rows) > max_rows:
            raise SourceError(f"The source exceeds the {max_rows}-row limit.")
        columns = _names(definition.get("columns", list(rows[0]) if rows else []), "Inline table columns")
        types = definition.get("column_types", {column: "TEXT" for column in columns})
        if not isinstance(types, dict) or set(types) - set(columns) or not all(isinstance(v, str) for v in types.values()):
            raise SourceError("column_types must map existing column names to type strings.")
        table = Table(name, columns, copy.deepcopy(rows), column_types=types.copy(),
                      column_collations={column: "BINARY" for column in columns})
        table.primary_key = (_names(definition["primary_key"], "primary_key", empty=True)
                             if "primary_key" in definition else [] if csv_mode else _infer_primary_key(table))
        table.foreign_keys = _foreign_keys(definition.get("foreign_keys", []))
        source.tables[name] = table
    _validate_source(source, max_rows, csv_mode=True,
                     warn_missing_primary_key=warn_missing_primary_key)
    return source


def _load_csv(data: bytes, path: Path, options: dict, limits: ResourceLimits) -> RelationalSource:
    content = _decode_text(data, "CSV")
    delimiter = _delimiter(options.get("delimiter", ","), "CSV")
    columns, rows = _delimited_rows(content, delimiter, "CSV", limits.max_source_rows)
    return _string_table_source(path, options, limits, columns, rows)


def _load_csv_bundle(path: Path, options: dict, limits: ResourceLimits) -> RelationalSource:
    """Load a server-authored collection of CSV tables and retained PK/FK metadata."""
    if any(key in options for key in ("tables", "primary_key", "foreign_keys")):
        raise SourceError("CSV bundle keys and relationships are defined by its retained manifest.")
    delimiter = _delimiter(options.get("delimiter", ","), "CSV")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos if not info.is_dir()]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise SourceError("CSV bundle contains duplicate entries or has no manifest.")
            if any(info.flag_bits & 0x1 for info in infos):
                raise SourceError("Encrypted CSV bundle entries are unsupported.")
            if any(info.file_size > limits.max_source_bytes for info in infos):
                raise SourceError(f"CSV bundle entry exceeds the {limits.max_source_bytes}-byte limit.")
            if sum(info.file_size for info in infos) > limits.max_source_bytes:
                raise SourceError(f"CSV bundle exceeds the {limits.max_source_bytes}-byte uncompressed limit.")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 1024 * 1024:
                raise SourceError("CSV bundle manifest is too large.")
            try:
                manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceError("CSV bundle manifest must be valid UTF-8 JSON.") from exc
            definitions = manifest.get("tables") if isinstance(manifest, dict) else None
            if (not isinstance(manifest, dict) or manifest.get("version") != "csv_bundle.v1"
                    or not isinstance(definitions, list) or not 1 <= len(definitions) <= 128):
                raise SourceError("CSV bundle manifest version or table list is invalid.")
            expected_entries = {"manifest.json"}
            source = RelationalSource({}, options.get("domain", ""), resource_limits=limits, _csv_mode=True)
            remaining = limits.max_source_rows
            for definition in definitions:
                allowed = {"file_name", "entry", "name", "columns", "primary_key", "foreign_keys"}
                if not isinstance(definition, dict) or set(definition) != allowed:
                    raise SourceError("CSV bundle table definitions are invalid.")
                entry = definition.get("entry")
                if (not isinstance(entry, str) or not entry.startswith("tables/") or "\\" in entry
                        or any(part in {"", ".", ".."} for part in entry.split("/")) or entry not in names):
                    raise SourceError("CSV bundle table entry is invalid.")
                expected_entries.add(entry)
                name = _name(definition.get("name"), "Table name")
                if name in source.tables:
                    raise SourceError(f"Duplicate table name {name!r}.")
                with archive.open(entry, "r") as binary:
                    text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                    try:
                        reader = csv.reader(text, delimiter=delimiter, strict=True)
                        columns = next(reader, None)
                        if columns is None:
                            raise SourceError(f"CSV table {name!r} is empty and has no header.")
                        _names(columns, f"CSV table {name!r} headers")
                        declared_columns = definition.get("columns")
                        if declared_columns != columns:
                            raise SourceError(f"CSV table {name!r} headers changed after schema configuration.")
                        rows = []
                        for line, cells in enumerate(checked(reader), 2):
                            if len(cells) != len(columns):
                                raise SourceError(f"CSV table {name!r} row {line} has {len(cells)} cells; expected {len(columns)}.")
                            rows.append(dict(zip(columns, cells)))
                            remaining -= 1
                            if remaining < 0:
                                raise SourceError(f"The source exceeds the {limits.max_source_rows}-row limit.")
                    except UnicodeDecodeError as exc:
                        raise SourceError(f"CSV table {name!r} must use UTF-8 encoding.") from exc
                    except csv.Error as exc:
                        raise SourceError(f"Invalid CSV table {name!r}: {exc}") from exc
                table = Table(name, columns, rows, column_types={column: "TEXT" for column in columns},
                              column_collations={column: "BINARY" for column in columns})
                table.primary_key = _names(definition["primary_key"], "primary_key", empty=True)
                table.foreign_keys = _foreign_keys(definition["foreign_keys"])
                source.tables[name] = table
            if set(names) != expected_entries:
                raise SourceError("CSV bundle contains unlisted files.")
            _validate_source(source, limits.max_source_rows, csv_mode=True)
            return source
    except zipfile.BadZipFile as exc:
        raise SourceError("CSV bundle archive is invalid.") from exc


def _infer_txt_delimiter(content: str, max_rows: int) -> str | None:
    """Conservatively recognize TSV; punctuation-delimited TXT must be explicit."""
    check_deadline()
    try:
        reader = csv.reader(io.StringIO(content, newline=""), delimiter="\t", strict=True)
        header = next(reader, None)
        if header is None or len(header) <= 1:
            return None
        seen_rows = 0
        for line, cells in enumerate(checked(reader), 2):
            if len(cells) != len(header):
                return None
            seen_rows += 1
            if seen_rows > max_rows:
                raise SourceError(f"The source exceeds the {max_rows}-row limit.")
    except csv.Error:
        return None
    finally:
        check_deadline()
    if not seen_rows:
        return None
    return "\t"


def _load_txt(data: bytes, path: Path, options: dict, limits: ResourceLimits) -> RelationalSource:
    from graph_data_service.text_encoding import decode_txt
    try:
        content = decode_txt(data)
    except ValueError as exc:
        raise SourceError(str(exc)) from exc
    if not content.strip():
        raise SourceError("TXT is empty and has no records.")
    txt_mode = options.get("txt_mode", "auto")
    if type(txt_mode) is not str or txt_mode not in {"auto", "lines", "entities"}:
        raise SourceError("TXT txt_mode must be auto, lines or entities.")
    if txt_mode in {"lines", "entities"} and "delimiter" in options:
        raise SourceError(f"TXT delimiter cannot be combined with txt_mode={txt_mode}.")
    if txt_mode == "entities" and any(key in options for key in ("primary_key", "foreign_keys", "tables")):
        raise SourceError("TXT txt_mode=entities cannot be combined with relational PK/FK or inline-table options.")
    if "delimiter" in options:
        delimiter = _delimiter(options["delimiter"], "TXT")
    elif txt_mode in {"lines", "entities"}:
        delimiter = None
    else:
        delimiter = _infer_txt_delimiter(content, limits.max_source_rows)
    if delimiter is not None:
        columns, rows = _delimited_rows(content, delimiter, "TXT", limits.max_source_rows)
        source = _string_table_source(path, options, limits, columns, rows)
        source._txt_mode = "table"
        return source
    lines = content.splitlines()
    if len(lines) > limits.max_source_rows:
        raise SourceError(f"The source exceeds the {limits.max_source_rows}-row limit.")
    columns = ["text"]
    rows = [{"text": line} for line in checked(lines)]
    source = _string_table_source(
        path, options, limits, columns, rows,
        ("TXT entity mode retained each line for LLM semantic extraction." if txt_mode == "entities" else
         "TXT line mode imported each line as one text record." if txt_mode == "lines" else
         "TXT was not recognized as a tab-delimited table; each line was imported as one text record."),
        warn_missing_primary_key=txt_mode != "entities",
    )
    source._txt_mode = txt_mode if txt_mode in {"lines", "entities"} else "lines"
    return source


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _statements(script: str):
    """Use SQLite's statement completeness parser, including quoted semicolons."""
    start = 0
    for index, char in enumerate(checked(script)):
        if char == ";" and sqlite3.complete_statement(script[start:index + 1]):
            yield script[start:index + 1]
            start = index + 1
    tail = script[start:]
    if _strip_comments(tail).strip():
        if not sqlite3.complete_statement(tail + ";"):
            raise SourceError("SQL dump contains an incomplete statement.")
        yield tail + ";"


def _strip_comments(statement: str) -> str:
    # Only strip leading comments, so literal contents are never rewritten.
    return re.sub(r"\A(?:\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)*", "", statement)


def _sql_keywords(statement: str) -> set[str]:
    # Ignore literals, quoted identifiers and comments before checking grammar.
    # Multirow VALUES emits SQLITE_SELECT internally, so that authorizer opcode
    # cannot distinguish ordinary dump inserts from a user-supplied SELECT.
    tokens = re.finditer(r"'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|`(?:(?:``)|[^`])*`|\[[^\]]*\]|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|([A-Za-z_][A-Za-z_0-9]*)", statement)
    return {match.group(1).upper() for match in checked(tokens) if match.group(1)}


def _import_sql(connection: sqlite3.Connection, data: bytes, deadline: float) -> None:
    try:
        script = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("SQL dump must be UTF-8 SQLite SQL or a SQLite database image.") from exc
    denied = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_TRIGGER,
              sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
              sqlite3.SQLITE_CREATE_TEMP_VIEW, sqlite3.SQLITE_CREATE_VTABLE,
              sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
              sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE,
              sqlite3.SQLITE_FUNCTION}

    def authorize(action, arg1, arg2, database, trigger):
        if action in denied or trigger is not None or database not in (None, "main"):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (arg1.lower() != "foreign_keys" or str(arg2).lower() not in ("off", "0")):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        for number, statement in enumerate(_statements(script), 1):
            if time.monotonic() > deadline:
                raise SourceError("SQLite import exceeded source_timeout_seconds.")
            prefix = _strip_comments(statement)
            if _sql_keywords(statement) & {"SELECT", "WITH", "RETURNING", "REPLACE", "UPDATE"}:
                raise SourceError(f"Unsupported SQL expression in statement {number}; dump INSERT values must be literal data.")
            if not re.match(r"(?is)\A(?:CREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)\b|INSERT\s+INTO\b|BEGIN(?:\s+TRANSACTION)?\s*;|COMMIT(?:\s+TRANSACTION)?\s*;|END(?:\s+TRANSACTION)?\s*;|PRAGMA\s+foreign_keys\s*=\s*(?:OFF|0)\s*;|DELETE\s+FROM\s+[\"`\[]?sqlite_sequence[\"`\]]?\s*;)", prefix):
                raise SourceError(f"Unsupported or unsafe SQL statement {number}; only SQLite table/index DDL, INSERT and dump transaction markers are accepted.")
            try:
                cursor = connection.execute(statement)
                if cursor.description is not None:
                    raise SourceError(f"Unsupported result-producing SQL statement {number}.")
            except sqlite3.Error as exc:
                raise SourceError(f"SQLite import failed at statement {number}: {exc}. Unsupported SQL dialects must be converted explicitly.") from exc
        connection.commit()
    finally:
        # Python < 3.11 does not support disabling this callback with None.
        connection.set_authorizer(lambda *_: sqlite3.SQLITE_OK)


def _canonical_name(name: str, candidates: list[str], label: str) -> str:
    if name in candidates:
        return name
    # SQLite identifiers are ASCII case insensitive; data values are never folded.
    def fold(value):
        return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
    matches = [candidate for candidate in candidates if fold(candidate) == fold(name)]
    if len(matches) != 1:
        raise SourceError(f"Unknown or ambiguous {label} {name!r}.")
    return matches[0]


def _column_collations(ddl: str, columns: list[str]) -> dict[str, str]:
    """Read only top-level column COLLATE clauses, never index/table constraints.

    SQLite's table_xinfo does not expose collation. Keep the original DDL as
    well, so an independent oracle can retain all declared schema semantics.
    """
    token_pattern = r"\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:(?:'')|[^'])*'|\"(?:(?:\"\")|[^\"])*\"|`(?:(?:``)|[^`])*`|\[[^\]]*\]|[A-Za-z_\u0080-\uffff][A-Za-z_0-9\u0080-\uffff]*|[^\s]"
    tokens = []
    for match in re.finditer(token_pattern, ddl):
        text = match.group()
        if text.isspace() or text.startswith(("--", "/*")):
            continue
        tokens.append(text)
    result = {column: "BINARY" for column in columns}
    if "(" not in tokens:
        raise SourceError("SQLite table definition has no column list.")
    depth, groups, current = 1, [], []
    for token in tokens[tokens.index("(") + 1:]:
        if token == "(" and depth >= 1:
            depth += 1
        elif token == ")":
            depth -= 1
            if depth == 0:
                if current:
                    groups.append(current)
                break
        if token == "," and depth == 1:
            groups.append(current)
            current = []
        else:
            current.append((depth, token))

    def identifier(token):
        if token[:1] in ('"', "`", "'"):
            return token[1:-1].replace(token[0] * 2, token[0])
        if token.startswith("["):
            return token[1:-1]
        return token

    for group in groups:
        if not group or group[0][1].upper() in {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}:
            continue
        name = _canonical_name(identifier(group[0][1]), columns, "column in CREATE TABLE")
        for index, (nesting, token) in enumerate(group[1:], 1):
            if nesting == 1 and token.upper() == "COLLATE":
                if index + 1 >= len(group):
                    raise SourceError("Incomplete column COLLATE declaration.")
                collation = identifier(group[index + 1][1]).upper()
                if collation not in {"BINARY", "NOCASE", "RTRIM"}:
                    raise SourceError(f"Unsupported SQLite collation {collation!r}; materialize supported data before import.")
                result[name] = collation
    return result


def _sqlite_rowid_alias(connection: sqlite3.Connection, table: str, columns: list[str]) -> str | None:
    """Return an unshadowed SQLite rowid alias, or None for WITHOUT ROWID tables."""
    folded = {column.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
              for column in columns}
    for alias in ("_rowid_", "rowid", "oid"):
        if alias in folded:
            continue
        try:
            connection.execute(f"SELECT t.{alias} FROM {_quote(table)} AS t LIMIT 0")
            return alias
        except sqlite3.OperationalError:
            continue
    return None


def _sql_fk_pairs(source: RelationalSource, deadline: float, limits: ResourceLimits) -> None:
    connection = source._connection
    assert connection is not None
    fallback_maps = {}

    def fallback_map(table: Table) -> dict[str, list[int]]:
        if table.name in fallback_maps:
            return fallback_maps[table.name]
        mapping: dict[str, list[int]] = {}
        for index, row in enumerate(table.rows):
            if index % 1024 == 0 and time.monotonic() > deadline:
                raise SourceError("SQLite import exceeded source_timeout_seconds.")
            mapping.setdefault(_encode(row), []).append(index)
        fallback_maps[table.name] = mapping
        return mapping

    total_edges = 0
    for table in source.tables.values():
        for fk in table.foreign_keys:
            parent = source.tables[fk.ref_table]
            names = ",".join(_quote(c) for c in fk.ref_columns)
            present = " AND ".join(f"{_quote(c)} IS NOT NULL" for c in fk.ref_columns)
            duplicate = connection.execute(f"SELECT 1 FROM {_quote(parent.name)} WHERE {present} GROUP BY {names} HAVING count(*) > 1 LIMIT 1").fetchone()
            if duplicate:
                raise SourceError(f"Foreign key target {parent.name!r} is not unique.")
            # The parent is on the left: SQLite uses its affinity and collation,
            # which is also SQLite's documented foreign-key comparison rule.
            condition = " AND ".join(f"p.{_quote(pc)} = c.{_quote(cc)}" for cc, pc in zip(fk.columns, fk.ref_columns))
            pairs = set()
            child_rowids = source._sql_rowids.get(table.name)
            parent_rowids = source._sql_rowids.get(parent.name)
            child_alias = source._sql_rowid_aliases.get(table.name)
            parent_alias = source._sql_rowid_aliases.get(parent.name)
            if child_rowids is not None and parent_rowids is not None and child_alias and parent_alias:
                selection = f"c.{child_alias},p.{parent_alias}"
                cursor = connection.execute(
                    f"SELECT {selection} FROM {_quote(table.name)} AS c "
                    f"JOIN {_quote(parent.name)} AS p ON {condition}"
                )
                for number, (child_rowid, parent_rowid) in enumerate(cursor, 1):
                    if number % 1024 == 0 and time.monotonic() > deadline:
                        raise SourceError("SQLite import exceeded source_timeout_seconds.")
                    if child_rowid not in child_rowids or parent_rowid not in parent_rowids:
                        raise SourceError("SQLite row identity changed while resolving foreign keys.")
                    pairs.add((child_rowids[child_rowid], parent_rowids[parent_rowid]))
                    if len(pairs) + total_edges > limits.max_graph_edges:
                        raise SourceError(f"Foreign keys exceed the {limits.max_graph_edges}-edge construction limit.")
            else:
                selection = (",".join(f"c.{_quote(c)}" for c in table.columns) + "," +
                             ",".join(f"p.{_quote(c)}" for c in parent.columns))
                cursor = connection.execute(
                    f"SELECT {selection} FROM {_quote(table.name)} AS c "
                    f"JOIN {_quote(parent.name)} AS p ON {condition}"
                )
                row_maps = (fallback_map(table), fallback_map(parent))
                seen_rows = set()
                for number, values in enumerate(cursor, 1):
                    if number % 1024 == 0 and time.monotonic() > deadline:
                        raise SourceError("SQLite import exceeded source_timeout_seconds.")
                    child_key = _encode(dict(zip(table.columns, values[:len(table.columns)])))
                    parent_key = _encode(dict(zip(parent.columns, values[len(table.columns):])))
                    if (child_key, parent_key) in seen_rows:
                        continue
                    seen_rows.add((child_key, parent_key))
                    for child_index in row_maps[0][child_key]:
                        for parent_index in row_maps[1][parent_key]:
                            pairs.add((child_index, parent_index))
                            if len(pairs) + total_edges > limits.max_graph_edges:
                                raise SourceError(f"Foreign keys exceed the {limits.max_graph_edges}-edge construction limit.")
            source._fk_pairs[foreign_key_type(table, fk)] = sorted(pairs)
            total_edges += len(pairs)


def _load_sql(data: bytes | None, options: dict, limits: ResourceLimits,
              *, database_path: Path | None = None) -> RelationalSource:
    database_identity = _database_file_identity(database_path) if database_path is not None else None
    if database_path is None:
        connection = sqlite3.connect(":memory:")
    else:
        # A file-backed read avoids sqlite3.deserialize's >2 GiB boundary.
        # Keep normal read-only locking so committed WAL content is visible.
        connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    source = RelationalSource({}, options.get("domain", ""), resource_limits=limits,
                              _connection=connection, _database_path=database_path,
                              _database_identity=database_identity)
    max_rows = limits.max_source_rows
    timeout = limits.source_timeout_seconds
    deadline = time.monotonic() + timeout
    if current_deadline() is not None:
        deadline = min(deadline, current_deadline())
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.enable_load_extension(False)
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-8192")
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 256)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 16 * 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        if database_path is not None:
            pass
        elif data is not None and data.startswith(b"SQLite format 3\x00"):
            if not hasattr(connection, "deserialize"):
                raise SourceError("This Python SQLite runtime cannot import database images in memory.")
            connection.deserialize(data)
        else:
            assert data is not None
            _import_sql(connection, data, deadline)
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY name").fetchall()
        for kind, name, ddl in schema:
            if kind in ("view", "trigger") or kind == "table" and re.match(r"(?is)\s*CREATE\s+VIRTUAL\s+TABLE", ddl or ""):
                raise SourceError("SQLite views, triggers and virtual tables are unsupported; provide ordinary materialized tables.")
        table_names = [name for kind, name, _ in schema if kind == "table"]
        create_sql = {name: ddl for kind, name, ddl in schema if kind == "table"}
        if len(table_names) > 128:
            raise SourceError("The source exceeds the 128-table limit.")
        remaining = max_rows
        for name in table_names:
            info = connection.execute(f"PRAGMA table_xinfo({_quote(name)})").fetchall()
            if any(row[6] != 0 for row in info):
                raise SourceError("Generated/hidden SQLite columns are unsupported; materialize them before import.")
            columns = [row[1] for row in info]
            rowid_alias = _sqlite_rowid_alias(connection, name, columns)
            column_selection = ",".join(f"t.{_quote(c)}" for c in columns)
            selection = f"t.{rowid_alias},{column_selection}" if rowid_alias else column_selection
            records = connection.execute(
                f"SELECT {selection} FROM {_quote(name)} AS t LIMIT ?", (remaining + 1,)
            ).fetchall()
            if rowid_alias:
                rowids = [record[0] for record in records]
                if len(rowids) != len(set(rowids)):
                    raise SourceError(f"SQLite row identity is not unique in table {name!r}.")
                source._sql_rowids[name] = {rowid: index for index, rowid in enumerate(rowids)}
                source._sql_rowid_aliases[name] = rowid_alias
                rows = [record[1:] for record in records]
            else:
                rows = records
            if len(rows) > remaining:
                raise SourceError(f"The source exceeds the {max_rows}-row limit.")
            remaining -= len(rows)
            primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            source.tables[name] = Table(name, columns, [dict(zip(columns, row)) for row in checked(rows)], primary_key,
                                        column_types={row[1]: row[2] for row in info},
                                        column_collations=_column_collations(create_sql[name], columns),
                                        create_sql=create_sql[name])
        for table in source.tables.values():
            groups = {}
            for entry in connection.execute(f"PRAGMA foreign_key_list({_quote(table.name)})"):
                groups.setdefault(entry[0], []).append(entry)
            for group in groups.values():
                ordered = sorted(group, key=lambda entry: entry[1])
                parent_name = _canonical_name(ordered[0][2], table_names, "referenced table")
                parent = source.tables[parent_name]
                child_columns = [_canonical_name(entry[3], table.columns, "foreign key column") for entry in ordered]
                parent_columns = (parent.primary_key.copy() if all(entry[4] is None for entry in ordered)
                                  else [_canonical_name(entry[4], parent.columns, "referenced column") for entry in ordered])
                fk = ForeignKey(child_columns, parent_name, parent_columns)
                if fk not in table.foreign_keys:
                    table.foreign_keys.append(fk)
        _validate_source(source, max_rows)
        try:
            _sql_fk_pairs(source, deadline, limits)
        finally:
            source._sql_rowids.clear()
            source._sql_rowid_aliases.clear()
        connection.set_progress_handler(None, 0)
        connection.execute("PRAGMA query_only=ON")
        if database_path is not None and _database_file_identity(database_path) != database_identity:
            raise SourceError("SQLite source changed while it was being loaded.")
        return source
    except sqlite3.Error as exc:
        source.close()
        check_deadline()
        raise SourceError(f"SQLite source cannot be imported completely: {exc}") from exc
    except BaseException:
        source.close()
        check_deadline()
        raise


def load_source(path: Path, options: dict | None = None) -> RelationalSource:
    """Read the complete source within an explicit profile; never truncate rows.

    The default bounded profile preserves the historical limits. Larger limits
    are available only through the explicit ``resource_profile=full_scale``
    opt-in, and every individual override remains capped by that profile.
    """
    check_deadline()
    path = Path(path)
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise SourceError("Source options must be an object.")
    if not isinstance(options.get("domain", ""), str) or len(options.get("domain", "")) > 16000:
        raise SourceError("domain must be a string of at most 16000 characters.")
    limits = _resource_limits(options)
    maximum = limits.max_source_bytes
    if path.stat().st_size > maximum:
        raise SourceError(f"Source exceeds the {maximum}-byte limit.")
    # SQLite's in-memory deserialize path can fail above 2 GiB even when the
    # host has ample RAM. Explicit full-scale runs keep the validated local
    # image file-backed and read-only instead of copying it through that API.
    with path.open("rb") as handle:
        header = handle.read(16)
    if path.suffix.lower() == ".csv" and zipfile.is_zipfile(path):
        return _load_csv_bundle(path, options, limits)
    direct_sqlite = (options.get("resource_profile") == "full_scale"
                     and path.suffix.lower() == ".sql" and header == b"SQLite format 3\x00")
    data = None
    if not direct_sqlite:
        chunks = []
        remaining = maximum + 1
        with path.open("rb") as handle:
            # Avoid asking Python/filesystem implementations to reserve the
            # full 8 GiB full-scale ceiling for a small CSV/TXT/SQL source.
            while remaining:
                chunk = handle.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
                check_deadline()
        data = b"".join(chunks)
        if len(data) > maximum:
            raise SourceError(f"Source exceeds the {maximum}-byte limit.")
    if path.suffix.lower() == ".csv":
        if "txt_mode" in options:
            raise SourceError("txt_mode is supported only for TXT sources.")
        return _load_csv(data, path, options, limits)
    if path.suffix.lower() == ".txt":
        return _load_txt(data, path, options, limits)
    if path.suffix.lower() == ".sql":
        if any(key in options for key in ("tables", "table_name", "primary_key", "foreign_keys", "delimiter", "txt_mode")):
            raise SourceError("Delimited-text table options cannot override SQL schema; declare keys in SQLite DDL.")
        return _load_sql(data, options, limits,
                         database_path=path.resolve(strict=True) if direct_sqlite else None)
    raise SourceError("Relational construction accepts .csv, .txt or .sql sources (SQLite SQL/database image).")


def foreign_key_pairs(source: RelationalSource, table: Table, fk: ForeignKey) -> list[tuple[int, int]]:
    """Return source-authoritative (child row index, parent row index) pairs.

    SQL pairs use the original parent's affinity/collation; CSV/TXT pairs use exact
    scalar JSON values and treat blank text references as absent. Callers receive
    a copy and cannot modify the immutable source comparison cache.
    """
    if table.name not in source.tables or fk not in table.foreign_keys:
        raise SourceError("Relationship must be a declared source foreign key.")
    relation = foreign_key_type(table, fk)
    if relation in source._fk_pairs:
        return source._fk_pairs[relation].copy()
    parent = source.tables[fk.ref_table]
    parent_index = {}
    for index, row in enumerate(checked(parent.rows)):
        values = [row[column] for column in fk.ref_columns]
        if any(value is None for value in values):
            continue
        key = _encode(values)
        if key in parent_index:
            raise SourceError(f"Foreign key target {parent.name!r} is not unique.")
        parent_index[key] = index
    pairs = []
    for index, row in enumerate(checked(table.rows)):
        values = [row[column] for column in fk.columns]
        if any(value is None or value == "" for value in values):
            continue
        target = parent_index.get(_encode(values))
        if target is not None:
            pairs.append((index, target))
    return pairs


def source_integrity_errors(source: RelationalSource, graph: dict, limit: int = 20) -> list[str]:
    """Audit all source records and declared FK edges, beyond a finite workload.

    Derived nodes/edges may coexist. Source records may be stored on nodes or
    edges, but declared FK edges must still reference the proper source rows.
    This is a fidelity gate, never a graph query answer provider.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise SourceError("Integrity error limit must be between 1 and 100.")
    if (source._database_path is not None and
            _database_file_identity(source._database_path) != source._database_identity):
        return ["SQLite source file changed after loading."]
    from .representation import resolve_rows
    try:
        current_rows = resolve_rows(graph)
    except (ValueError, KeyError, TypeError) as exc:
        return [f"Invalid current row representation: {exc}"]
    errors, found, endpoints, schemas = [], {}, {}, {}
    for item in checked(current_rows):
        properties = item["properties"]
        if "table" not in properties or "data" not in properties:
            continue
        table, index = properties["table"], properties.get("row_index")
        if not isinstance(table, str) or table not in source.tables or type(index) is not int or not 0 <= index < len(source.tables[table].rows):
            errors.append("Unknown source record provenance.")
        elif (table, index) in found:
            errors.append(f"Duplicate source record: {table}[{index}].")
        else:
            found[table, index] = properties["data"]
            endpoints[item["id"]] = (table, index)
            if "relational_schema" in properties:
                expected_schema = {"column_types": source.tables[table].column_types,
                                   "column_collations": source.tables[table].column_collations,
                                   "sqlite_affinity": bool(source.tables[table].create_sql)}
                if _encode(properties["relational_schema"]) != _encode(expected_schema):
                    errors.append(f"Source column semantics changed: {table}.")
                schemas[table] = properties["relational_schema"]
        if len(errors) >= limit:
            return errors
    for table in source.tables.values():
        if table.rows and table.name not in schemas:
            errors.append(f"Source column semantics lost: {table.name}.")
            if len(errors) >= limit:
                return errors
        for index, expected in enumerate(checked(table.rows)):
            if (table.name, index) not in found or _encode(found[table.name, index]) != _encode(expected):
                errors.append(f"Source values changed or lost: {table.name}[{index}].")
                if len(errors) >= limit:
                    return errors
    declared, actual = {}, {}
    for table in source.tables.values():
        for fk in table.foreign_keys:
            key = _encode({"table": table.name, **asdict(fk)})
            declared[key] = {(table.name, child, fk.ref_table, parent)
                             for child, parent in foreign_key_pairs(source, table, fk)}
            actual[key] = set()
    relationships = []
    for edge in checked(graph["edges"]):
        if edge["properties"].get("foreign_key") is not None:
            relationships.append((edge["properties"]["foreign_key"], edge["source"], edge["target"]))
    for row in checked(current_rows):
        for endpoint in row["properties"].get("row_endpoints", []):
            relationships.append((endpoint["foreign_key"], row["id"], endpoint["node_id"]))
    for fk, child_id, parent_id in checked(relationships):
        key = _encode(fk)
        start, end = endpoints.get(child_id), endpoints.get(parent_id)
        if key not in declared or start is None or end is None:
            errors.append("Foreign-key edge has invalid source provenance.")
        else:
            pair = (*start, *end)
            if pair in actual[key]:
                errors.append("Duplicate declared foreign-key edge.")
            elif pair not in declared[key]:
                errors.append("Declared foreign-key edge contradicts the source relationship.")
            actual[key].add(pair)
        if len(errors) >= limit:
            return errors
    for key, expected in declared.items():
        missing = expected - actual[key]
        if missing:
            relationship = json.loads(key)
            errors.append(f"Missing {len(missing)} declared foreign-key edge(s): {relationship['table']} -> {relationship['ref_table']}.")
            if len(errors) >= limit:
                return errors
    return errors


def initial_graph(source: RelationalSource) -> dict:
    """Keep every input row; keyless CSV tables carry rows on relationship edges."""
    graph = {"directed": True, "nodes": [], "edges": []}
    limits = source.resource_limits
    relationship_tables = {}
    if source._csv_mode:
        for table in source.tables.values():
            if not table.primary_key:
                # The same columns may reference alternative entity types (for
                # example, a shareholder can be a company or a person).
                groups = {}
                for fk in table.foreign_keys:
                    groups.setdefault(tuple(fk.columns), []).append(fk)
                if len(groups) != 2:
                    raise CsvConfigurationError(
                        f"CSV table {table.name!r} has no primary key. Configure a primary key to create nodes, "
                        "or two foreign-key column groups to create relationship edges. "
                        "CSV 表未设置主键：请设置主键以生成节点，或配置两组外键列以确定关系边的两端。"
                    )
                relationship_tables[table.name] = list(groups.values())
            for fk in table.foreign_keys:
                if not source.tables[fk.ref_table].primary_key:
                    raise CsvConfigurationError(
                        f"CSV foreign key {table.name!r} -> {fk.ref_table!r} targets a table without a primary key. "
                        "Configure a primary key on the target table. 外键目标表必须设置主键，才能作为边的端点。"
                    )
    row_count = sum(len(table.rows) for table in source.tables.values() if table.name not in relationship_tables)
    if row_count > limits.max_graph_nodes:
        raise SourceError(f"Rows exceed the {limits.max_graph_nodes}-node construction limit.")
    identities = {}
    for table in source.tables.values():
        identities[table.name] = [row_id(table, row, index) for index, row in enumerate(checked(table.rows))]
        if table.name in relationship_tables:
            continue
        for index, row in enumerate(checked(table.rows)):
            properties = {"table": table.name, "row_index": index, "data": copy.deepcopy(row)}
            if index == 0:
                properties["relational_schema"] = {"column_types": table.column_types.copy(),
                                                    "column_collations": table.column_collations.copy(),
                                                    "sqlite_affinity": bool(table.create_sql)}
            graph["nodes"].append({"id": identities[table.name][index], "type": table.name,
                                   "properties": properties})
    for table in source.tables.values():
        if table.name in relationship_tables:
            endpoint_groups = [[(fk, dict(foreign_key_pairs(source, table, fk))) for fk in group]
                               for group in relationship_tables[table.name]]
            for index, row in enumerate(checked(table.rows)):
                endpoints = []
                for group in endpoint_groups:
                    matches = [(fk, pairs[index]) for fk, pairs in group if index in pairs]
                    if len(matches) != 1:
                        raise CsvConfigurationError(
                            f"CSV relationship table {table.name!r}, row {index + 2}: "
                            f"foreign key {group[0][0].columns!r} has {len(matches)} matching endpoints; expected exactly one. "
                            "Fill in the foreign key or correct its target configuration. "
                            "关系表外键为空、未匹配到节点或匹配多个目标，请补充外键值或修正目标表配置。"
                        )
                    fk, parent_index = matches[0]
                    endpoints.append({"foreign_key": {"table": table.name, **asdict(fk)},
                                      "node_id": identities[fk.ref_table][parent_index]})
                properties = {"table": table.name, "row_index": index, "data": copy.deepcopy(row),
                              "row_endpoints": endpoints}
                if index == 0:
                    properties["relational_schema"] = {"column_types": table.column_types.copy(),
                                                        "column_collations": table.column_collations.copy(),
                                                        "sqlite_affinity": False}
                graph["edges"].append({"id": _hash("row_edge_", identities[table.name][index]),
                                       "type": table.name, "source": endpoints[0]["node_id"],
                                       "target": endpoints[1]["node_id"], "properties": properties})
                if len(graph["edges"]) > limits.max_graph_edges:
                    raise SourceError(f"Relationships exceed the {limits.max_graph_edges}-edge construction limit.")
            continue
        for fk in table.foreign_keys:
            parent = source.tables[fk.ref_table]
            relation = foreign_key_type(table, fk)
            pairs = foreign_key_pairs(source, table, fk)
            matched = {index for index, _ in pairs}
            dangling = sum(1 for index, row in enumerate(table.rows) if index not in matched and
                           all(row[column] is not None and (relation in source._fk_pairs or row[column] != "") for column in fk.columns))
            if dangling:
                warning = f"{dangling} dangling foreign key(s) in {table.name!r} {fk.columns!r} -> {parent.name!r}; source rows are retained."
                if warning not in source.warnings:
                    source.warnings.append(warning)
            for child_index, parent_index in checked(pairs):
                start = identities[table.name][child_index]
                end = identities[parent.name][parent_index]
                graph["edges"].append({"id": _hash("edge_", relation, start, end),
                                       "type": relation, "source": start, "target": end,
                                       "properties": {"foreign_key": {"table": table.name, **asdict(fk)}}})
                if len(graph["edges"]) > limits.max_graph_edges:
                    raise SourceError(f"Foreign keys exceed the {limits.max_graph_edges}-edge construction limit.")
    return graph
