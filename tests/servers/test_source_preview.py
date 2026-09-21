"""Source previews preserve values and scope while staying bounded/read-only."""
import json
import sqlite3
import zipfile

import pytest

from graph_data_service.construction import ConstructionError
from graph_data_service.source_preview import CELL_CHARS, SAMPLE_ROWS, TEXT_CHARS, source_preview


def preview(tmp_path, content, filename="source.sql", **kwargs):
    path = tmp_path / filename
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return source_preview(str(path), filename, "sample", **kwargs)


def test_sql_literals_comments_keys_and_multiple_tables(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("SQL text must never connect to or execute a database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = preview(tmp_path, """
        CREATE TABLE people (
          id INTEGER PRIMARY KEY, -- 用户编号
          name TEXT COMMENT '显示名称',
          note TEXT /* 备注内容 */
        );
        CREATE TABLE orders (id INT PRIMARY KEY, owner INT REFERENCES people(id), amount DECIMAL(10, 2));
        INSERT INTO people VALUES (1, 'O''Brien, (admin);', NULL), (2, '中文', 'a--b/*c*/');
        INSERT INTO orders(id,owner,amount) VALUES(10,1,1.25e2);
        COMMENT ON COLUMN orders.amount IS '订单金额';
        ATTACH DATABASE '/private.db' AS secrets;
        SELECT load_extension('anything');
    """)
    people, orders = result["tables"]
    assert people["sample_rows"][0] == {"id": 1, "name": "O'Brien, (admin);", "note": None}
    assert people["sample_rows"][1]["note"] == "a--b/*c*/"
    assert people["column_descriptions"]["id"] == {"text": "用户编号", "source": "comment"}
    assert people["column_descriptions"]["name"]["text"] == "显示名称"
    assert people["column_descriptions"]["note"]["text"] == "备注内容"
    assert orders["sample_rows"][0]["amount"] == 125.0
    assert orders["foreign_keys"] == [{"columns": ["owner"], "ref_table": "people", "ref_columns": ["id"]}]
    assert orders["column_descriptions"]["owner"]["source"] == "schema"
    assert orders["column_descriptions"]["amount"] == {"text": "订单金额", "source": "comment"}
    assert result["warnings"]


def test_sql_sampling_and_omitted_insert_values_are_explicit(tmp_path):
    rows = ",".join(f"({i}, '{'长' * (CELL_CHARS + 20)}')" for i in range(9))
    result = preview(tmp_path, f"CREATE TABLE t(id INTEGER PRIMARY KEY, note TEXT, other TEXT DEFAULT 'x'); INSERT INTO t(id,note) VALUES {rows};")
    table = result["tables"][0]
    assert table["row_count"] == 9
    assert len(table["sample_rows"]) == SAMPLE_ROWS
    assert len(table["sample_rows"][0]["note"]) == CELL_CHARS + 1
    assert table["sample_rows"][0]["other"] == "[未在 INSERT 中提供]"
    assert result["truncated"] is True
    assert table["column_descriptions"]["note"]["source"] == "unavailable"


def test_sql_expressions_are_never_evaluated(tmp_path):
    result = preview(tmp_path, "CREATE TABLE t(value TEXT); INSERT INTO t VALUES (random()), (1 + 2); INSERT INTO t SELECT 'hidden';")
    table = result["tables"][0]
    assert table["sample_rows"] == [{"value": "[SQL 表达式，未执行]"}] * 2
    assert table["row_count"] is None
    assert result["warnings"]


def test_sql_large_identifiers_remain_exact_in_browser_json(tmp_path):
    result = preview(tmp_path, "CREATE TABLE t(id INTEGER); INSERT INTO t VALUES (9007199254740993);")
    assert result["tables"][0]["sample_rows"] == [{"id": "9007199254740993"}]


def test_sql_insert_column_identifiers_are_case_insensitive_and_keep_declared_spelling(tmp_path):
    result = preview(tmp_path, '''
        CREATE TABLE People (ID INTEGER PRIMARY KEY, DisplayName TEXT, Region TEXT);
        INSERT INTO people (displayNAME, id, REGION) VALUES ('Alice', 1, 'US');
        INSERT INTO PEOPLE ("Id", "displayname") VALUES (2, 'alice');
    ''')
    assert result["tables"][0]["sample_rows"] == [
        {"ID": 1, "DisplayName": "Alice", "Region": "US"},
        {"ID": 2, "DisplayName": "alice", "Region": "[未在 INSERT 中提供]"},
    ]


def test_field_meanings_keep_comment_schema_hint_and_unknown_provenance(tmp_path):
    result = preview(tmp_path, '''
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE t (id INTEGER PRIMARY KEY COMMENT '业务编号', name TEXT REFERENCES parent(id),
                        DisplayName TEXT, Reputation INTEGER, CreationDate TEXT, Location TEXT,
                        Score INTEGER, Title TEXT, TagName TEXT, Count INTEGER, unknown_metric TEXT);
    ''')
    fields = result["tables"][1]["column_descriptions"]
    assert fields["id"] == {"text": "业务编号", "source": "comment"}
    assert fields["name"]["source"] == "schema" and "parent.id" in fields["name"]["text"]
    for column in ("DisplayName", "Reputation", "CreationDate", "Location", "Score", "Title", "TagName", "Count"):
        assert fields[column]["source"] == "inferred"
        assert "按字段名推测" in fields[column]["text"] and "核实" in fields[column]["text"]
    assert fields["unknown_metric"]["source"] == "unavailable"


def test_csv_name_hints_do_not_invent_units_or_status_mappings(tmp_path):
    result = preview(tmp_path, "name,city,status,quantity,unit_price,opaque\nWidget,Beijing,1,2,19.5,x\n", "items.csv")
    fields = result["tables"][0]["column_descriptions"]
    assert all(fields[column]["source"] == "inferred" for column in ("name", "city", "status", "quantity", "unit_price"))
    assert "各取值的含义未提供" in fields["status"]["text"]
    assert "单位未提供" in fields["quantity"]["text"]
    assert "币种及计价单位未提供" in fields["unit_price"]["text"]
    assert fields["opaque"]["source"] == "unavailable"


def test_sql_unterminated_comment_is_consumed_once_then_rejected(tmp_path):
    from graph_data_service.source_preview import _TOKENS
    hostile = "/*x" * 100_000
    # The EOF alternative consumes one token instead of retrying each opener.
    assert [match.group() for match in _TOKENS.finditer(hostile)] == [hostile]
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, hostile)
    assert "未闭合" in str(error.value)


def test_sql_unterminated_bracket_is_consumed_once_then_rejected(tmp_path):
    from graph_data_service.source_preview import _TOKENS
    hostile = "[" * 100_000
    assert [match.group() for match in _TOKENS.finditer(hostile)] == [hostile]
    with pytest.raises(ConstructionError, match="未闭合"):
        preview(tmp_path, hostile)


def test_sql_validates_insert_arity_beyond_the_sample(tmp_path):
    with pytest.raises(ConstructionError, match="不匹配"):
        preview(tmp_path, "CREATE TABLE t(id INT); INSERT INTO t VALUES (1),(2),(3),(4),(5),(6,7);")


@pytest.mark.parametrize("kind", ["csv", "sql", "sqlite"])
@pytest.mark.parametrize("value,clipped", [("x" * (CELL_CHARS + 1), True), ("原文省略号…", False)])
def test_value_clipping_does_not_claim_additional_rows(tmp_path, kind, value, clipped):
    if kind == "csv":
        result = preview(tmp_path, f"note\n{value}\n", "source.csv")
    elif kind == "sql":
        result = preview(tmp_path, f"CREATE TABLE t(note TEXT); INSERT INTO t VALUES ('{value}');")
    else:
        path = tmp_path / "source.sql"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE t(note TEXT)")
            connection.execute("INSERT INTO t VALUES(?)", (value,))
        result = source_preview(str(path), path.name, "sample")
    table = result["tables"][0]
    assert table["row_count"] == 1
    assert table["sample_truncated"] is False
    assert table["values_truncated"] is clipped
    assert result["truncated"] is clipped


def test_sql_large_quoted_literal_does_not_allocate_per_character_regex_frames():
    import tracemalloc
    from graph_data_service.source_preview import _TOKENS
    literal = "'" + "x" * 1_000_000 + "'"
    escaped = "'" + "''" * 100_000 + "'"
    tracemalloc.start()
    try:
        assert [match.group() for match in _TOKENS.finditer(literal)] == [literal]
        assert [match.group() for match in _TOKENS.finditer(escaped)] == [escaped]
        assert tracemalloc.get_traced_memory()[1] < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("limit", ["MAX_SQL_TOKENS", "MAX_SQL_STATEMENT_TOKENS"])
def test_sql_token_budgets_stop_large_statement_allocation(tmp_path, monkeypatch, limit):
    import graph_data_service.source_preview as module
    monkeypatch.setattr(module, limit, 40)
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, "CREATE TABLE t(id INT); INSERT INTO t VALUES " + ",".join("(1)" for _ in range(50)) + ";")
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "source_preview_too_complex"


def test_sql_time_budget_is_enforced(tmp_path, monkeypatch):
    import itertools
    import graph_data_service.source_preview as module
    clock = itertools.count(0, module.SQL_PREVIEW_SECONDS + 1)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, "CREATE TABLE t(id INT);")
    assert error.value.detail["code"] == "source_preview_timeout"


def test_sqlite_image_is_read_only_and_samples_are_bounded(tmp_path):
    path = tmp_path / "source.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, payload TEXT)")
        connection.executemany("INSERT INTO records VALUES(?,?)", [(i, f"值{i}") for i in range(8)])
    before = path.read_bytes()
    result = source_preview(str(path), path.name, "image")
    table = result["tables"][0]
    assert table["columns"] == ["id", "payload"]
    assert table["sample_rows"][0] == {"id": 0, "payload": "值0"}
    assert len(table["sample_rows"]) == SAMPLE_ROWS
    assert table["row_count"] is None  # No full COUNT scan for database images.
    assert table["sample_truncated"] is True
    assert path.read_bytes() == before


def test_sqlite_column_comments_match_sql_text_and_do_not_shift_to_next_field(tmp_path):
    ddl = """CREATE TABLE records (
        id INTEGER, -- 记录编号
        name TEXT,
        /* 分类说明 */ category TEXT
    );"""
    text = preview(tmp_path, ddl, "schema.sql")
    path = tmp_path / "image.sql"
    with sqlite3.connect(path) as connection:
        connection.execute(ddl)
    image = source_preview(str(path), path.name, "image")
    fields = image["tables"][0]["column_descriptions"]
    assert fields == text["tables"][0]["column_descriptions"]
    assert fields["id"] == {"text": "记录编号", "source": "comment"}
    assert fields["name"]["source"] == "inferred"
    assert fields["category"] == {"text": "分类说明", "source": "comment"}


def test_sqlite_virtual_generated_expressions_are_rejected_before_value_selection(tmp_path):
    path = tmp_path / "generated.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(id INTEGER, payload TEXT GENERATED ALWAYS AS (printf('%2000000s','x')) VIRTUAL)")
        connection.execute("INSERT INTO t(id) VALUES(1)")
    assert path.stat().st_size < 20000
    with pytest.raises(ConstructionError) as error:
        source_preview(str(path), path.name, "generated")
    assert "生成或隐藏列" in str(error.value)


def test_sqlite_value_limit_applies_before_fetching_large_stored_cells(tmp_path):
    from graph_data_service.source_preview import SQLITE_VALUE_BYTES
    path = tmp_path / "large-cell.sql"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(payload TEXT)")
        connection.execute("INSERT INTO t VALUES(?)", ("x" * (SQLITE_VALUE_BYTES + 1),))
    with pytest.raises(ConstructionError) as error:
        source_preview(str(path), path.name, "large-cell")
    assert error.value.status_code == 422


def test_csv_utf8_bom_exact_values_quoted_multiline_and_delimiter(tmp_path):
    result = preview(tmp_path, '\ufeffid;name;note\r\n001;张三;"a; b\r\nc"\r\n002;李四;\r\n', "people.csv", delimiter=";")
    table = result["tables"][0]
    assert table["columns"] == ["id", "name", "note"]
    assert table["sample_rows"] == [{"id": "001", "name": "张三", "note": "a; b\r\nc"}, {"id": "002", "name": "李四", "note": ""}]
    assert table["row_count"] == 2
    assert table["primary_key"] == []  # No uniqueness claim based on five samples.


def test_csv_bundle_previews_every_table_with_retained_keys(tmp_path):
    path = tmp_path / "tables.csv"
    tables = [
        {"name": "customers", "entry": "tables/customers.csv", "file_name": "customers.csv", "columns": ["id", "name"], "primary_key": ["id"], "foreign_keys": []},
        {"name": "orders", "entry": "tables/orders.csv", "file_name": "orders.csv", "columns": ["id", "customer_id"], "primary_key": ["id"], "foreign_keys": [{"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}]},
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"version": "csv_bundle.v1", "tables": tables}))
        archive.writestr("tables/customers.csv", "id,name\n001,Alice\n")
        archive.writestr("tables/orders.csv", "id,customer_id\no1,001\n")
    result = source_preview(str(path), path.name, "bundle")
    assert [table["name"] for table in result["tables"]] == ["customers", "orders"]
    assert result["tables"][1]["sample_rows"] == [{"id": "o1", "customer_id": "001"}]
    assert result["tables"][1]["column_descriptions"]["customer_id"]["source"] == "schema"


def test_txt_preserves_blank_lines_and_reports_clipping(tmp_path):
    result = preview(tmp_path, "第一段\n\n第二段\n", "notes.txt")
    assert result["text"] == {"excerpt": "第一段\n\n第二段\n", "truncated": False, "total_lines": 3}
    result = preview(tmp_path, "😀" * (TEXT_CHARS * 2), "notes.txt")
    assert result["text"]["excerpt"] == "😀" * TEXT_CHARS
    assert result["text"]["truncated"] is True
    assert result["text"]["total_lines"] is None


@pytest.mark.parametrize("content,filename", [(b'\xff\xfe', 'invalid.csv'), (b'id,name\n1\n', 'invalid.csv'), (b'id,id\n1,2\n', 'invalid.csv'), (b'CREATE TABLE t (id INT;', 'invalid.sql')])
def test_invalid_sources_return_structured_error(tmp_path, content, filename):
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, content, filename)
    assert error.value.status_code == 422


def test_size_limit_checked_before_full_read(tmp_path, monkeypatch):
    import graph_data_service.source_preview as module
    monkeypatch.setattr(module, "MAX_BYTES", 10)
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, "id,name\n1,longname\n", "large.csv")
    assert error.value.status_code == 413


def test_response_size_limit_counts_utf8_bytes_for_chinese(tmp_path, monkeypatch):
    import graph_data_service.source_preview as module
    content = "name\n" + "汉" * 300 + "\n"
    result = preview(tmp_path, content, "names.csv")
    encoded = json.dumps(result, ensure_ascii=False)
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(encoded) + 1)
    assert len(encoded.encode("utf-8")) > module.MAX_RESPONSE_BYTES
    with pytest.raises(ConstructionError) as error:
        preview(tmp_path, content, "names.csv")
    assert error.value.status_code == 413


def test_csv_bundle_rejects_oversized_uncompressed_entries(tmp_path, monkeypatch):
    import graph_data_service.source_preview as module
    path = tmp_path / "bomb.csv"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("tables/a.csv", "x" * 100000)
    monkeypatch.setattr(module, "MAX_BYTES", 1000)
    assert path.stat().st_size < 1000
    with pytest.raises(ConstructionError):
        source_preview(str(path), path.name, "bomb")


@pytest.mark.parametrize("manifest", [[], {"version": "csv_bundle.v1", "tables": ["bad"]},
    {"version": "csv_bundle.v1", "tables": [{"name": "bad", "entry": "../outside.csv"}]},
    {"version": "csv_bundle.v1", "tables": [{"name": "bad", "entry": "tables/bad.csv", "columns": ["id"], "foreign_keys": ["bad"]}]}])
def test_csv_bundle_malformed_manifest_returns_422(tmp_path, manifest):
    path = tmp_path / "invalid.csv"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("tables/bad.csv", "id\n1\n")
    with pytest.raises(ConstructionError) as error:
        source_preview(str(path), path.name, "invalid")
    assert error.value.status_code == 422
