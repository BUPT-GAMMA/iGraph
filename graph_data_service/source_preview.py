"""Bounded, read-only source samples, separate from graph construction.

SQL dumps are tokenized as data: uploaded statements are never executed. SQLite
images are opened read-only and queried only through quoted table identifiers.
Descriptions distinguish explicit comments, schema facts and tentative name hints.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import time
import zipfile
from pathlib import Path

from graph_data_service.construction import ConstructionError

MAX_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SAMPLE_ROWS = 5
CELL_CHARS = 300
TEXT_CHARS = 4000
MAX_TABLES = 128
MAX_COLUMNS = 256
MAX_SQL_TOKENS = 1_000_000
MAX_SQL_STATEMENT_TOKENS = 250_000
SQL_PREVIEW_SECONDS = 5
SQLITE_VALUE_BYTES = 1024 * 1024

# Only familiar exact names receive hints. These are translations of names,
# not inferred domain rules, units, status-code mappings, or data statistics.
_FIELD_NAME_HINTS = {
    "id": "标识字段候选（唯一性尚未确认）",
    "displayname": "显示名称",
    "reputation": "声望或信誉值（计算规则未提供）",
    "creationdate": "创建日期或时间（时区未提供）",
    "createdat": "创建日期或时间（时区未提供）",
    "location": "位置描述（具体对象及格式未提供）",
    "score": "评分或得分（计算规则未提供）",
    "title": "标题",
    "tagname": "标签名称",
    "count": "计数值（统计对象及口径未提供）",
    "name": "名称",
    "city": "城市名称或编码（格式未提供）",
    "status": "状态值（各取值的含义未提供）",
    "quantity": "数量（单位未提供）",
    "unitprice": "单价（币种及计价单位未提供）",
    "price": "价格（币种及计价单位未提供）",
    "amount": "数额（币种、单位及口径未提供）",
    "category": "分类名称或编码（分类规则未提供）",
}

# Quoted punctuation never acts as a separator. Comments remain available for
# descriptions, but are discarded before grammar/literal recognition.
# Chunked possessive quote branches (Python 3.12 runtime) avoid allocating one
# regex backtracking frame per character of a large literal/quoted identifier.
_TOKENS = re.compile(
    r"--[^\n]*(?:\n|$)|/\*[\s\S]*?(?:\*/|\Z)|'(?:[^']++|'')*+'|"
    r'"(?:[^"]++|"")*+"|`(?:[^`]++|``)*+`|\[[^\]]*\]|'
    r"[A-Za-z_\u0080-\uffff][\w$]*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[^\s]"
)


def _fail(message: str, code: str = "source_preview_invalid", status: int = 422):
    raise ConstructionError(code, message, status)


def _decode(data: bytes, *, partial: bool = False) -> str:
    import codecs
    try:
        return codecs.getincrementaldecoder("utf-8-sig")().decode(data, final=not partial)
    except UnicodeDecodeError:
        _fail("数据预览要求 UTF-8 编码，请先将源文件转换为 UTF-8。")


def _clip(value):
    if isinstance(value, bytes):
        return "[二进制数据]"
    if isinstance(value, int) and abs(value) > 2 ** 53 - 1:
        return str(value)  # Preserve identifiers beyond JavaScript's safe integer range.
    if isinstance(value, float) and not math.isfinite(value):
        return "[非有限数值]"
    if isinstance(value, str) and len(value) > CELL_CHARS:
        return value[:CELL_CHARS] + "…"
    return value


def _identifier(token: str) -> str:
    if token[:1] in {'"', "`", "'"}:
        return token[1:-1].replace(token[0] * 2, token[0])
    if token.startswith("["):
        return token[1:-1]
    return token


def _comment(token: str) -> bool:
    return token.startswith(("--", "/*"))


def _append_sql_token(tokens: list[str], token: str, whitespace_before: str) -> None:
    """A same-line comment after a comma documents the preceding column."""
    if _comment(token) and tokens and tokens[-1] == "," and "\n" not in whitespace_before:
        tokens.insert(len(tokens) - 1, token)
    else:
        tokens.append(token)


def _schema_tokens(sql: str) -> list[str]:
    tokens, previous_end = [], 0
    for match in _TOKENS.finditer(sql):
        if len(tokens) >= MAX_SQL_STATEMENT_TOKENS:
            _fail("SQL 表定义超过预览复杂度上限。", "source_preview_too_complex", 413)
        _append_sql_token(tokens, match.group(), sql[previous_end:match.start()])
        previous_end = match.end()
    return tokens


def _groups(tokens: list[str]):
    """Split commas only at the current level, retaining nested expressions."""
    depth, current = 0, []
    for token in tokens:
        if token == "," and depth == 0:
            yield current
            current = []
        else:
            current.append(token)
            depth += (token == "(") - (token == ")")
    if current:
        yield current


def _parenthesized(tokens: list[str], start: int) -> tuple[list[str], int]:
    if start >= len(tokens) or tokens[start] != "(":
        _fail("SQL 表结构或 VALUES 列表不完整。")
    depth = 1
    for index in range(start + 1, len(tokens)):
        depth += (tokens[index] == "(") - (tokens[index] == ")")
        if depth == 0:
            return tokens[start + 1:index], index + 1
    _fail("SQL 包含未闭合的括号。")


def _names(tokens: list[str]) -> list[str]:
    return [_identifier(group[0]) for group in _groups(tokens) if group]


def _table(name: str, columns: list[str]) -> dict:
    if (not name or len(name) > 1024 or not columns or len(columns) > MAX_COLUMNS
            or len(set(columns)) != len(columns)
            or any(not column or len(column) > 1024 for column in columns)):
        _fail("数据预览要求有效且不重复的表头，每张表最多 256 列。")
    return {"name": name, "columns": columns, "column_types": {}, "primary_key": [],
            "foreign_keys": [], "column_descriptions": {}, "sample_rows": [],
            "row_count": 0, "sample_truncated": False}


def _describe(table: dict):
    for column in table["columns"]:
        if column in table["column_descriptions"]:
            continue
        facts = []
        if column in table["primary_key"]:
            facts.append("主键字段，用于标识表中记录")
        for fk in table["foreign_keys"]:
            if column in fk["columns"]:
                index = fk["columns"].index(column)
                target = fk["ref_columns"][index] if index < len(fk["ref_columns"]) else "主键"
                facts.append(f"外键，关联 {fk['ref_table']}.{target}")
        if facts:
            table["column_descriptions"][column] = {"text": "；".join(facts), "source": "schema"}
        else:
            hint = _FIELD_NAME_HINTS.get(column.replace("_", "").casefold())
            table["column_descriptions"][column] = (
                {"text": f"按字段名推测：{hint}；需结合数据字典核实。", "source": "inferred"}
                if hint else {"text": "源文件未提供字段含义注释", "source": "unavailable"})


def _insert_columns(requested: list[str], declared: list[str]) -> list[str]:
    """Resolve SQL identifiers to their declared spelling without altering data."""
    result = []
    for name in requested:
        matches = [column for column in declared if column.casefold() == name.casefold()]
        if len(matches) != 1 or matches[0] in result:
            _fail("SQL INSERT 列名不存在、重复或大小写匹配不明确。")
        result.append(matches[0])
    return result


def _create_table(tokens: list[str]) -> dict | None:
    clean = [token for token in tokens if not _comment(token)]
    upper = [token.upper() for token in clean]
    if upper[:2] != ["CREATE", "TABLE"]:
        return None
    index = 5 if upper[2:5] == ["IF", "NOT", "EXISTS"] else 2
    if index + 1 >= len(clean) or clean[index + 1] != "(":
        _fail("数据预览仅支持带显式列定义的 CREATE TABLE。")
    name = _identifier(clean[index])
    # Preserve comments inside the original definition for documentation.
    start = tokens.index("(")
    body, _ = _parenthesized(tokens, start)
    columns, types, primary, foreign, descriptions = [], {}, [], [], {}
    for group in _groups(body):
        comments = [token for token in group if _comment(token)]
        group = [token for token in group if not _comment(token)]
        if not group:
            continue
        upper = [token.upper() for token in group]
        if upper[0] == "CONSTRAINT":
            group, upper = group[2:], upper[2:]
        if not group:
            continue
        if upper[:2] == ["PRIMARY", "KEY"]:
            key, _ = _parenthesized(group, 2)
            primary.extend(_names(key))
            continue
        if upper[:2] == ["FOREIGN", "KEY"]:
            child, after = _parenthesized(group, 2)
            if after + 1 < len(group) and upper[after] == "REFERENCES":
                refs = []
                if after + 2 < len(group) and group[after + 2] == "(":
                    refs, _ = _parenthesized(group, after + 2)
                foreign.append({"columns": _names(child), "ref_table": _identifier(group[after + 1]),
                                "ref_columns": _names(refs)})
            continue
        if upper[0] in {"UNIQUE", "CHECK"}:
            continue
        column = _identifier(group[0])
        columns.append(column)
        constraints = {"PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT", "REFERENCES", "COLLATE", "COMMENT", "GENERATED"}
        end = next((i for i, item in enumerate(upper[1:], 1) if item in constraints), len(group))
        types[column] = " ".join(group[1:end])[:100]
        if any(upper[i:i + 2] == ["PRIMARY", "KEY"] for i in range(len(upper))):
            primary.append(column)
        if "REFERENCES" in upper:
            ref = upper.index("REFERENCES")
            if ref + 1 < len(group):
                refs = []
                if ref + 2 < len(group) and group[ref + 2] == "(":
                    refs, _ = _parenthesized(group, ref + 2)
                foreign.append({"columns": [column], "ref_table": _identifier(group[ref + 1]),
                                "ref_columns": _names(refs)})
        description = ""
        if "COMMENT" in upper:
            comment = upper.index("COMMENT")
            if comment + 1 < len(group) and group[comment + 1].startswith("'"):
                description = _identifier(group[comment + 1])
        elif comments:
            description = " ".join(item[2:-2] if item.startswith("/*") else item[2:].strip() for item in comments)
        if description.strip():
            descriptions[column] = {"text": _clip(description.strip()), "source": "comment"}
    table = _table(name, columns)
    table.update(column_types=types, primary_key=primary, foreign_keys=foreign, column_descriptions=descriptions)
    return table


def _literal(tokens: list[str]):
    value = "".join(tokens)
    if len(tokens) == 1 and tokens[0].startswith("'"):
        return _identifier(tokens[0])
    if value.upper() == "NULL":
        return None
    if value.upper() in {"TRUE", "FALSE"}:
        return value.upper() == "TRUE"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value) if len(value) < 100 else _clip(value)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return _clip(float(value))
    return "[SQL 表达式，未执行]"


def _sql_text(content: str, warnings: list[str]) -> list[dict]:
    tables, statement = {}, []
    skipped = False
    deadline = time.monotonic() + SQL_PREVIEW_SECONDS

    def check_budget():
        if time.monotonic() > deadline:
            _fail("SQL 预览超过 5 秒解析预算，请使用较小的数据样本。", "source_preview_timeout")

    def consume(tokens):
        nonlocal skipped
        check_budget()
        clean = [token for token in tokens if not _comment(token)]
        if not clean:
            return
        upper = [token.upper() for token in clean]
        table = _create_table(tokens)
        if table:
            if len(tables) >= MAX_TABLES:
                _fail("数据预览最多支持 128 张表。")
            if table["name"] in tables:
                _fail("SQL 包含重复表定义，无法可靠展示示例。")
            tables[table["name"]] = table
        elif upper[:2] == ["INSERT", "INTO"] and len(clean) > 3:
            name = _identifier(clean[2])
            table = next((t for key, t in tables.items() if key.casefold() == name.casefold()), None)
            if table is None:
                skipped = True
                return
            index, columns = 3, table["columns"]
            if clean[index] == "(":
                names, index = _parenthesized(clean, index)
                columns = _insert_columns(_names(names), table["columns"])
            if index >= len(clean) or upper[index] != "VALUES":
                skipped = True
                table["row_count"] = None
                return
            index += 1
            while index < len(clean):
                check_budget()
                if clean[index] == ",":
                    index += 1
                    continue
                if clean[index] != "(":
                    skipped = True
                    break
                cells, index = _parenthesized(clean, index)
                if table["row_count"] is not None:
                    table["row_count"] += 1
                if len(table["sample_rows"]) < SAMPLE_ROWS:
                    values = [_clip(_literal(group)) for group in _groups(cells)]
                    if len(values) != len(columns) or any(column not in table["columns"] for column in columns):
                        _fail(f"SQL 表 {name} 的 INSERT 列与示例值不匹配。")
                    row = dict(zip(columns, values))
                    # Defaults may be expressions; never silently present them as NULL.
                    table["sample_rows"].append({column: row.get(column, "[未在 INSERT 中提供]") for column in table["columns"]})
                else:
                    table["sample_truncated"] = True
        elif upper[0] == "COMMENT" and upper[1:3] == ["ON", "COLUMN"]:
            if len(clean) >= 8 and clean[4] == "." and upper[6] == "IS" and clean[7].startswith("'"):
                table = tables.get(_identifier(clean[3]))
                column = _identifier(clean[5])
                if table and column in table["columns"]:
                    table["column_descriptions"][column] = {"text": _clip(_identifier(clean[7])), "source": "comment"}
        elif upper[0] not in {"BEGIN", "COMMIT", "END", "PRAGMA"} and upper[:2] != ["CREATE", "INDEX"]:
            skipped = True

    previous_end = 0
    for token_count, match in enumerate(_TOKENS.finditer(content), 1):
        if token_count > MAX_SQL_TOKENS or len(statement) >= MAX_SQL_STATEMENT_TOKENS:
            _fail("SQL 预览超过解析复杂度上限，请拆分 INSERT 语句或使用较小的数据样本。",
                  "source_preview_too_complex", 413)
        if token_count % 1024 == 1:
            check_budget()
        token = match.group()
        if token.startswith("/*") and not token.endswith("*/"):
            _fail("SQL 包含未闭合的块注释。")
        if token == ";":
            consume(statement)
            statement = []
        else:
            _append_sql_token(statement, token, content[previous_end:match.start()])
        previous_end = match.end()
    consume(statement)
    if skipped:
        warnings.append("部分 SQL 语句不是可预览的 CREATE TABLE / INSERT VALUES；已跳过且未执行。记录数仅统计识别到的 VALUES。")
    if not tables:
        warnings.append("未识别到可预览的 CREATE TABLE 表定义；SQL 预览支持显式建表和 INSERT VALUES。")
    return list(tables.values())


def _csv_table(data: bytes, name: str, delimiter: str, definition: dict | None = None) -> dict:
    reader = csv.reader(io.StringIO(_decode(data), newline=""), delimiter=delimiter, strict=True)
    try:
        table = _table(name, next(reader, []))
        for number, cells in enumerate(reader, 2):
            if len(cells) != len(table["columns"]):
                _fail(f"CSV 表 {name} 第 {number} 行的列数与表头不一致。")
            table["row_count"] += 1
            if len(table["sample_rows"]) < SAMPLE_ROWS:
                table["sample_rows"].append(dict(zip(table["columns"], map(_clip, cells))))
    except csv.Error:
        _fail(f"CSV 表 {name} 格式无效或单元格过长。")
    table["column_types"] = {column: "TEXT" for column in table["columns"]}
    table["sample_truncated"] = table["row_count"] > SAMPLE_ROWS
    if definition:
        if definition.get("columns") != table["columns"]:
            _fail("CSV 数据包的表头与清单不一致。")
        keys = definition.get("primary_key", [])
        foreign = definition.get("foreign_keys", [])
        if (not isinstance(keys, list) or any(not isinstance(key, str) or key not in table["columns"] for key in keys)
                or not isinstance(foreign, list) or len(foreign) > 128):
            _fail("CSV 数据包的键定义无效。")
        for key in foreign:
            if (not isinstance(key, dict) or not isinstance(key.get("ref_table"), str)
                    or not isinstance(key.get("columns"), list) or not isinstance(key.get("ref_columns"), list)
                    or not key["columns"] or len(key["columns"]) != len(key["ref_columns"])
                    or any(not isinstance(column, str) or column not in table["columns"] for column in key["columns"])
                    or any(not isinstance(column, str) for column in key["ref_columns"])):
                _fail("CSV 数据包的外键定义无效。")
        table["primary_key"] = keys
        table["foreign_keys"] = [{key: fk[key] for key in ("columns", "ref_table", "ref_columns")} for fk in foreign]
    return table


def _csv_bundle(path: Path, delimiter: str) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (len(names) != len(set(names)) or len(infos) > MAX_TABLES + 1
                    or sum(info.file_size for info in infos) > MAX_BYTES
                    or any(info.flag_bits & 1 for info in infos)):
                _fail("CSV 数据包超过预览大小限制，或包含重复/加密文件。")
            if archive.getinfo("manifest.json").file_size > 1024 * 1024:
                _fail("CSV 数据包清单过大。")
            manifest = json.loads(_decode(archive.read("manifest.json")))
            if not isinstance(manifest, dict):
                _fail("CSV 数据包清单无效。")
            definitions = manifest.get("tables", [])
            if (manifest.get("version") != "csv_bundle.v1" or not isinstance(definitions, list)
                    or not 1 <= len(definitions) <= MAX_TABLES):
                _fail("CSV 数据包清单无效。")
            result = []
            for item in definitions:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    _fail("CSV 数据包表定义无效。")
                entry = item["entry"]
                if not isinstance(entry, str) or not entry.startswith("tables/") or ".." in entry.split("/") or "\\" in entry:
                    _fail("CSV 数据包路径无效。")
                result.append(_csv_table(archive.read(entry), item["name"], delimiter, item))
            return result
    except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ConstructionError):
            raise
        _fail("CSV 数据包无法读取或清单无效。")


def _sqlite_image(path: Path) -> list[dict]:
    deadline = time.monotonic() + 3
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        # A tiny image can declare an expression that generates huge values.
        # Limit allocations inside SQLite before any schema/data query, and
        # reject generated columns before selecting values at all.
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQLITE_VALUE_BYTES)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        definitions = connection.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT ?", (MAX_TABLES + 1,)).fetchall()
        if len(definitions) > MAX_TABLES:
            _fail("数据预览最多支持 128 张表。")
        tables = []
        for name, sql in definitions:
            table = _create_table(_schema_tokens(sql or ""))
            if table is None:
                continue  # Virtual tables are never invoked.
            quote = lambda value: '"' + value.replace('"', '""') + '"'
            info = connection.execute(f"PRAGMA table_xinfo({quote(name)})").fetchall()
            if any(column[6] != 0 for column in info):
                _fail("SQLite 表包含生成或隐藏列，请先将其物化为普通列再预览。")
            columns = ",".join(quote(column) for column in table["columns"])
            rows = connection.execute(f"SELECT {columns} FROM {quote(name)} LIMIT ?", (SAMPLE_ROWS + 1,)).fetchall()
            table["sample_rows"] = [dict(zip(table["columns"], map(_clip, row))) for row in rows[:SAMPLE_ROWS]]
            table["sample_truncated"] = len(rows) > SAMPLE_ROWS
            table["row_count"] = None if table["sample_truncated"] else len(rows)
            tables.append(table)
        return tables
    except sqlite3.Error:
        _fail("SQLite 数据预览失败，文件无效或超过查询时间限制。")
    finally:
        connection.close()


def source_preview(source_path: str, source_filename: str, dataset_id: str, delimiter: str = ",") -> dict:
    path = Path(source_path)
    source_type = Path(source_filename).suffix.lower().lstrip(".")
    if source_type not in {"sql", "csv", "txt"}:
        _fail("数据预览支持 SQL、CSV 和 TXT 数据源。")
    if delimiter not in {",", "\t", ";", "|", " "}:
        _fail("CSV 分隔符必须为逗号、制表符、分号、竖线或空格。")
    try:
        size = path.stat().st_size
        if source_type != "txt" and size > MAX_BYTES:
            _fail("源文件超过 32 MiB 预览上限；请使用较小的数据样本。", "source_preview_too_large", 413)
        with path.open("rb") as handle:
            if source_type == "sql":
                data = handle.read(16)
                if data != b"SQLite format 3\x00":
                    data += handle.read(MAX_BYTES + 1 - len(data))
            else:
                data = handle.read(min(size, TEXT_CHARS * 4 + 4) if source_type == "txt" else MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            _fail("源文件超过预览上限。", "source_preview_too_large", 413)
        result = {"success": True, "dataset_id": dataset_id, "source_type": source_type,
                  "source_filename": source_filename, "tables": [], "warnings": [], "truncated": False,
                  "limits": {"sample_rows": SAMPLE_ROWS, "cell_chars": CELL_CHARS, "text_chars": TEXT_CHARS}}
        if source_type == "txt":
            partial = len(data) < size
            text = _decode(data, partial=partial)
            result["text"] = {"excerpt": text[:TEXT_CHARS], "truncated": partial or len(text) > TEXT_CHARS,
                              "total_lines": None if partial else len(text.splitlines())}
            result["truncated"] = result["text"]["truncated"]
        elif source_type == "csv":
            result["tables"] = (_csv_bundle(path, delimiter) if zipfile.is_zipfile(path)
                                else [_csv_table(data, Path(source_filename).stem, delimiter)])
        elif data.startswith(b"SQLite format 3\x00"):
            result["tables"] = _sqlite_image(path)
        else:
            result["tables"] = _sql_text(_decode(data), result["warnings"])
        for table in result["tables"]:
            _describe(table)
            table["sample_truncated"] = table["sample_truncated"] or any(
                isinstance(value, str) and value.endswith("…") for row in table["sample_rows"] for value in row.values())
        result["truncated"] = result["truncated"] or any(table["sample_truncated"] for table in result["tables"])
        # Bound the serialized response as well as input, rows, columns and cells.
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            _fail("预览内容超过 4 MiB 响应上限；请拆分数据表。", "source_preview_too_large", 413)
        return result
    except OSError:
        _fail("源文件不可用，请重新上传数据。", "source_preview_unavailable", 404)
