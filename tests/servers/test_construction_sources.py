"""Source fidelity, key semantics, and isolation of untrusted SQL imports."""
import sqlite3
import copy
import json
import zipfile
from contextlib import closing
from unittest.mock import patch

import pytest

import graph_engine.construction.sources as sources_module
from graph_engine.construction.sources import (
    ResourceLimits, SourceError, foreign_key_pairs, foreign_key_type, initial_graph, load_source, row_id,
    source_integrity_errors,
)
from graph_engine.construction.workload import _oracle


def sql_source(tmp_path, sql, **options):
    path = tmp_path / "source.sql"
    path.write_text(sql, encoding="utf-8")
    return load_source(path, options)


def test_composite_pk_fk_retains_null_and_dangling_rows(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE tenants (region TEXT, id INTEGER, name TEXT,
                              PRIMARY KEY (region, id));
        CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, tenant INTEGER,
                             FOREIGN KEY(region, tenant) REFERENCES tenants(region, id));
        INSERT INTO tenants VALUES ('EU', 1, 'first'), ('US', 1, 'second');
        INSERT INTO orders VALUES (10, 'EU', 1), (11, 'US', 1),
                                  (12, NULL, 1), (13, 'EU', 404);
    """, domain="retail")
    try:
        graph = initial_graph(source)
        assert set(graph) == {"directed", "nodes", "edges"}
        assert len(graph["nodes"]) == 6
        assert len(graph["edges"]) == 2
        assert source.tables["tenants"].primary_key == ["region", "id"]
        assert any("1 dangling" in warning for warning in source.warnings)
        assert source.schema()["domain"] == "retail"
        sample = source.samples(1)
        sample["tenants"][0]["name"] = "changed"
        assert source.tables["tenants"].rows[0]["name"] == "first"
        orders = source.tables["orders"]
        assert graph["edges"][0]["type"] == foreign_key_type(orders, orders.foreign_keys[0])
        assert graph["edges"][0]["properties"]["foreign_key"]["columns"] == ["region", "tenant"]
    finally:
        source.close()


def test_sqlite_implicit_composite_reference_and_identifier_casing(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE Parent(A TEXT, B INT, PRIMARY KEY(A, B)) WITHOUT ROWID;
        CREATE TABLE Child(Id INT PRIMARY KEY, a TEXT, b INT,
                           FOREIGN KEY(a,b) REFERENCES PARENT);
        INSERT INTO Parent VALUES('A',1);
        INSERT INTO Child VALUES(2,'A',1);
    """)
    try:
        assert source.tables["Child"].foreign_keys[0].ref_columns == ["A", "B"]
        assert len(initial_graph(source)["edges"]) == 1
    finally:
        source.close()


def test_sql_values_preserve_case_and_parent_fk_affinity(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE parent(id TEXT PRIMARY KEY);
        CREATE TABLE child(id INTEGER PRIMARY KEY, pid TEXT REFERENCES parent(id));
        INSERT INTO parent VALUES ('A'),('a');
        INSERT INTO child VALUES(1,'A'),(2,'a');
        CREATE TABLE numeric_parent(id INTEGER PRIMARY KEY);
        CREATE TABLE numeric_child(id INTEGER PRIMARY KEY, pid TEXT REFERENCES numeric_parent(id));
        INSERT INTO numeric_parent VALUES(7);
        INSERT INTO numeric_child VALUES(9,'007');
        CREATE TABLE nocase_parent(id TEXT COLLATE NOCASE PRIMARY KEY);
        CREATE TABLE nocase_child(id INTEGER PRIMARY KEY, pid TEXT REFERENCES nocase_parent(id));
        INSERT INTO nocase_parent VALUES('ABC');
        INSERT INTO nocase_child VALUES(8,'abc');
    """)
    try:
        graph = initial_graph(source)
        assert len(graph["edges"]) == 4
        parent_targets = [edge["target"] for edge in graph["edges"]
                          if edge["properties"]["foreign_key"]["ref_table"] == "parent"]
        assert len(set(parent_targets)) == 2
        assert not source.warnings
    finally:
        source.close()


def test_csv_exact_strings_inline_tables_and_stable_primary_key(tmp_path):
    path = tmp_path / "orders.csv"
    path.write_text("id,person_id,amount\n001,A,1.20\n002,a,2.40\n003,,0.00\n", encoding="utf-8")
    source = load_source(path, {"foreign_keys": [{"columns": ["person_id"], "ref_table": "people", "ref_columns": ["id"]}],
                                "tables": [{"name": "people", "rows": [{"id": "A"}, {"id": "a"}]}]})
    graph = initial_graph(source)
    assert len(graph["edges"]) == 2
    assert len({edge["target"] for edge in graph["edges"]}) == 2
    table = source.tables["orders"]
    assert table.primary_key == ["id"]
    assert table.rows[0] == {"id": "001", "person_id": "A", "amount": "1.20"}
    assert row_id(table, table.rows[0], 0) == row_id(table, table.rows[0], 100)
    assert not source.warnings


def test_csv_bundle_loads_multiple_tables_and_declared_pk_fk(tmp_path):
    path = tmp_path / "retail.csv"
    manifest = {"version": "csv_bundle.v1", "tables": [
        {"file_name": "customers.csv", "entry": "tables/000-customers.csv", "name": "customers",
         "columns": ["customer_id", "name"], "primary_key": ["customer_id"], "foreign_keys": []},
        {"file_name": "orders.csv", "entry": "tables/001-orders.csv", "name": "orders",
         "columns": ["order_id", "customer_id", "amount"], "primary_key": ["order_id"],
         "foreign_keys": [{"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["customer_id"]}]},
    ]}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("tables/000-customers.csv", "customer_id,name\nc1,Alice\nc2,Bob\n")
        archive.writestr("tables/001-orders.csv", "order_id,customer_id,amount\no1,c1,9.50\no2,c2,3.00\n")
    source = load_source(path)
    try:
        assert list(source.tables) == ["customers", "orders"]
        assert source.tables["customers"].primary_key == ["customer_id"]
        assert source.tables["orders"].foreign_keys[0].ref_table == "customers"
        graph = initial_graph(source)
        assert len(graph["nodes"]) == 4
        assert len(graph["edges"]) == 2
    finally:
        source.close()


def test_csv_header_starting_with_pk_is_not_treated_as_bundle(tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text("PK_ID,value\n1,alpha\n2,beta\n", encoding="utf-8")

    source = load_source(path)
    try:
        assert source.tables["plain"].columns == ["PK_ID", "value"]
        assert len(initial_graph(source)["nodes"]) == 2
    finally:
        source.close()


def test_txt_auto_detects_tabular_delimiter_and_preserves_quoted_multiline_values(tmp_path):
    path = tmp_path / "people.txt"
    path.write_bytes(
        b"\xef\xbb\xbfid\tname\tnote\r\n"
        b"001\tAlice\t\"line one\r\nline two\"\r\n"
        b"002\tBob\tplain\r\n"
    )
    source = load_source(path)
    try:
        table = source.tables["people"]
        assert table.columns == ["id", "name", "note"]
        assert table.primary_key == ["id"]
        assert table.rows == [
            {"id": "001", "name": "Alice", "note": "line one\r\nline two"},
            {"id": "002", "name": "Bob", "note": "plain"},
        ]
        graph = initial_graph(source)
        assert len(graph["nodes"]) == 2
        assert source_integrity_errors(source, graph) == []
        assert not source.warnings
    finally:
        source.close()


def test_txt_uses_csv_pk_fk_and_inline_table_options(tmp_path):
    path = tmp_path / "orders.txt"
    path.write_text("id,person_id,amount\n001,A,1.20\n002,B,2.40\n", encoding="utf-8")
    source = load_source(path, {
        "delimiter": ",",
        "foreign_keys": [{"columns": ["person_id"], "ref_table": "people", "ref_columns": ["id"]}],
        "tables": [{"name": "people", "rows": [{"id": "A"}, {"id": "B"}]}],
    })
    try:
        assert source.tables["orders"].primary_key == ["id"]
        assert source.tables["orders"].rows[0]["amount"] == "1.20"
        graph = initial_graph(source)
        assert len(graph["nodes"]) == 4
        assert len(graph["edges"]) == 2
        assert source_integrity_errors(source, graph) == []
    finally:
        source.close()


def test_plain_txt_imports_every_line_as_a_text_record_without_dropping_blanks(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("first line\n\nthird,line\n", encoding="utf-8")
    source = load_source(path)
    try:
        table = source.tables["notes"]
        assert table.columns == ["text"]
        assert table.rows == [{"text": "first line"}, {"text": ""}, {"text": "third,line"}]
        assert table.primary_key == []
        graph = initial_graph(source)
        assert len(graph["nodes"]) == 3
        assert source_integrity_errors(source, graph) == []
        assert any("each line" in warning for warning in source.warnings)
        assert any("row-position" in warning for warning in source.warnings)
    finally:
        source.close()


def test_one_line_txt_with_punctuation_is_not_mistaken_for_a_header_only_table(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("Hello, world", encoding="utf-8")
    source = load_source(path)
    try:
        assert source.tables["note"].rows == [{"text": "Hello, world"}]
        assert len(initial_graph(source)["nodes"]) == 1
    finally:
        source.close()


def test_punctuation_in_plain_txt_does_not_consume_the_first_line_as_a_header(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("Hello, world\nGoodbye, world\n", encoding="utf-8")
    source = load_source(path)
    try:
        assert source.tables["note"].rows == [
            {"text": "Hello, world"}, {"text": "Goodbye, world"},
        ]
        assert len(initial_graph(source)["nodes"]) == 2
    finally:
        source.close()


def test_txt_delimiter_inference_checks_all_records_before_selecting_table_mode(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("kind\tvalue\n" + "note\tok\n" * 64 + "plain final line\n", encoding="utf-8")
    source = load_source(path)
    try:
        table = source.tables["notes"]
        assert table.columns == ["text"]
        assert len(table.rows) == 66
        assert table.rows[-1] == {"text": "plain final line"}
    finally:
        source.close()


def test_txt_explicit_delimiters_enable_non_tsv_table_inputs(tmp_path):
    path = tmp_path / "items.txt"
    path.write_text("id|label\n001|first\n002|second\n", encoding="utf-8")
    source = load_source(path, {"delimiter": "|"})
    try:
        assert source.tables["items"].primary_key == ["id"]
        assert source.tables["items"].rows[1] == {"id": "002", "label": "second"}
    finally:
        source.close()


def test_txt_line_mode_can_preserve_tab_separated_prose(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("chapter\tintroduction\nbody\tcontinued\n", encoding="utf-8")
    source = load_source(path, {"txt_mode": "lines"})
    try:
        assert source.tables["notes"].rows == [
            {"text": "chapter\tintroduction"}, {"text": "body\tcontinued"},
        ]
    finally:
        source.close()


def test_txt_entity_mode_retains_logical_lines_for_semantic_extraction(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Alice works at OpenAI.\n\nBob works at OpenAI.\n", encoding="utf-8")
    source = load_source(path, {"txt_mode": "entities"})
    try:
        table = source.tables["notes"]
        assert table.columns == ["text"]
        assert table.rows == [
            {"text": "Alice works at OpenAI."},
            {"text": ""},
            {"text": "Bob works at OpenAI."},
        ]
        assert table.primary_key == []
        assert source._txt_mode == "entities"
        assert any("LLM semantic extraction" in warning for warning in source.warnings)
        assert not any("row-position" in warning for warning in source.warnings)
    finally:
        source.close()


@pytest.mark.parametrize("content,options,message", [
    (b"\xff", {}, "UTF-8"),
    (b"id|name\n1\n", {"delimiter": "|"}, "expected 2"),
    (b"id\tname\n1\tAlice\n", {"delimiter": "\n"}, "one character"),
    (b"one\ntwo\n", {"txt_mode": "table"}, "auto, lines or entities"),
    (b"one\ntwo\n", {"txt_mode": []}, "auto, lines or entities"),
    (b"one\ntwo\n", {"txt_mode": "lines", "delimiter": ","}, "cannot be combined"),
    (b"one\ntwo\n", {"txt_mode": "entities", "delimiter": ","}, "cannot be combined"),
    (b"one\ntwo\n", {"txt_mode": "entities", "primary_key": ["text"]}, "relational PK/FK"),
    (b"one\ntwo\n", {"txt_mode": "entities", "foreign_keys": []}, "relational PK/FK"),
    (b"one\ntwo\n", {"txt_mode": "entities", "tables": []}, "relational PK/FK"),
    (b"one\ntwo\n", {"max_source_rows": 1}, "row limit"),
])
def test_txt_rejects_invalid_or_ambiguous_inputs(tmp_path, content, options, message):
    path = tmp_path / "source.txt"
    path.write_bytes(content)
    with pytest.raises(SourceError, match=message):
        load_source(path, options)


def test_csv_keyless_duplicate_rows_and_non_identity_unique_columns(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("name\nSame\nSame\n", encoding="utf-8")
    source = load_source(path)
    graph = initial_graph(source)
    assert len({row["id"] for row in graph["nodes"]}) == 2
    assert source.tables["source"].primary_key == []
    assert any("no reliable primary key" in warning for warning in source.warnings)
    path.write_text("name\nOne\nTwo\n", encoding="utf-8")
    assert load_source(path).tables["source"].primary_key == []


@pytest.mark.parametrize("content,options,message", [
    ("id,id\n1,2\n", {}, "duplicate"),
    ("id,name\n1\n", {}, "expected 2"),
    ("id\n1\n1\n", {"primary_key": ["id"]}, "not unique"),
    ("id,name\n,x\n", {"primary_key": ["id"]}, "missing"),
    ("id\n1\n", {"tables": [{"name": "external", "path": "/etc/passwd"}]}, "paths are unsupported"),
    ("id\n1\n2\n", {"max_source_rows": 1}, "row limit"),
    ("id\n1\n", {"max_source_bytes": 2}, "byte limit"),
])
def test_csv_fails_instead_of_losing_data(tmp_path, content, options, message):
    path = tmp_path / "source.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SourceError, match=message):
        load_source(path, options)


def test_resource_profiles_are_explicit_typed_and_capped(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("id\n1\n", encoding="utf-8")
    bounded = load_source(path)
    assert bounded.resource_limits == ResourceLimits(
        32 * 1024 * 1024, 200_000, 10, 200_000, 1_000_000, 200_000, 10,
    )
    with pytest.raises(SourceError, match="max_source_rows"):
        load_source(path, {"max_source_rows": 200_001})
    full_defaults = load_source(path, {"resource_profile": "full_scale"})
    assert full_defaults.resource_limits == ResourceLimits(
        8 * 1024 * 1024 * 1024, 10_000_000, 7_200, 15_000_000, 30_000_000, 10_000_000, 600,
    )
    full = load_source(path, {"resource_profile": "full_scale", "max_source_rows": 200_001})
    assert full.resource_limits.max_source_rows == 200_001
    assert load_source(path, {"max_source_bytes": 64 * 1024 * 1024}).resource_limits.max_source_bytes == 64 * 1024 * 1024
    for options in (
        {"resource_profile": True},
        {"resource_profile": "unbounded"},
        {"resource_profile": "full_scale", "max_source_rows": 10_000_001},
        {"resource_profile": "full_scale", "query_timeout_seconds": 1.5},
    ):
        with pytest.raises(SourceError):
            load_source(path, options)


def test_full_scale_profile_can_be_reduced_for_a_tiny_run(tmp_path):
    path = tmp_path / "source.sql"
    path.write_text("CREATE TABLE t(id INTEGER PRIMARY KEY); INSERT INTO t VALUES(1),(2);", encoding="utf-8")
    options = {"resource_profile": "full_scale", "max_source_bytes": 1024, "max_source_rows": 2,
               "source_timeout_seconds": 1, "max_graph_nodes": 2, "max_graph_edges": 1,
               "max_intermediate_rows": 2, "query_timeout_seconds": 1}
    source = load_source(path, options)
    try:
        assert source.resource_limits == ResourceLimits(1024, 2, 1, 2, 1, 2, 1)
        assert len(initial_graph(source)["nodes"]) == 2
    finally:
        source.close()


def test_sql_fk_pairing_uses_rowid_without_serializing_full_rows(tmp_path):
    original_encode = sources_module._encode

    def reject_full_rows(value):
        if isinstance(value, dict):
            raise AssertionError("fast rowid path serialized a complete row")
        return original_encode(value)

    with patch.object(sources_module, "_encode", side_effect=reject_full_rows):
        source = sql_source(tmp_path, """
            CREATE TABLE parent(id TEXT COLLATE NOCASE PRIMARY KEY, payload TEXT);
            CREATE TABLE child(id INTEGER PRIMARY KEY, pid TEXT REFERENCES parent(id), payload TEXT);
            INSERT INTO parent VALUES('P', 'large parent payload');
            INSERT INTO child VALUES(1, 'p', 'first'), (2, 'P', 'second');
        """)
        try:
            fk = source.tables["child"].foreign_keys[0]
            assert foreign_key_pairs(source, source.tables["child"], fk) == [(0, 0), (1, 0)]
            assert source._sql_rowids == {}
            assert source._sql_rowid_aliases == {}
        finally:
            source.close()


def test_initial_graph_uses_source_node_and_edge_limits(tmp_path):
    path = tmp_path / "children.csv"
    path.write_text("id,parent_id\n1,P\n2,P\n", encoding="utf-8")
    node_limited = load_source(path, {"max_graph_nodes": 1})
    with pytest.raises(SourceError, match="node construction limit"):
        initial_graph(node_limited)
    edge_limited = load_source(path, {
        "max_graph_edges": 1,
        "foreign_keys": [{"columns": ["parent_id"], "ref_table": "parents", "ref_columns": ["id"]}],
        "tables": [{"name": "parents", "rows": [{"id": "P"}]}],
    })
    with pytest.raises(SourceError, match="edge construction limit"):
        initial_graph(edge_limited)


@pytest.mark.parametrize("sql", [
    "ATTACH DATABASE 'evil.db' AS outside;",
    "CREATE TABLE data(id); VACUUM INTO 'evil.db';",
    "CREATE TABLE data(id); SELECT load_extension('evil.dll');",
    "CREATE TABLE data(id); CREATE TRIGGER t AFTER INSERT ON data BEGIN DELETE FROM data; END;",
    "CREATE VIRTUAL TABLE data USING fts5(text);",
    "CREATE VIEW data AS SELECT 1;",
    "CREATE TABLE data(id); INSERT INTO data SELECT 1;",
    "CREATE TABLE data(id); INSERT INTO data VALUES(randomblob(100000));",
    "PRAGMA writable_schema=ON;",
    "CREATE TABLE data(id); DROP TABLE data;",
    "CREATE TABLE data(id); UPDATE data SET id=5;",
    "CREATE TABLE data(id); INSERT OR IGNORE INTO data VALUES(1);",
    "COPY data FROM '/etc/passwd';",
    "CREATE TABLE data(id SERIAL PRIMARY KEY); INSERT INTO data(id) VALUES(1) RETURNING id;",
])
def test_sql_rejects_unsafe_or_unsupported_statements(tmp_path, sql):
    with pytest.raises(SourceError):
        sql_source(tmp_path, sql)
    assert not (tmp_path / "evil.db").exists()


def test_sqlite_binary_import_is_memory_only_and_read_only(tmp_path):
    path = tmp_path / "database.sql"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE sqliteXnormal(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO sqliteXnormal VALUES(1, 'unchanged')")
        connection.commit()
    original = path.read_bytes()
    source = load_source(path)
    try:
        assert source.tables["sqliteXnormal"].rows == [{"id": 1, "name": "unchanged"}]
        with pytest.raises(sqlite3.OperationalError):
            source._connection.execute("DELETE FROM sqliteXnormal")
        assert path.read_bytes() == original
    finally:
        source.close()
    assert source._connection is None


def test_full_scale_sqlite_binary_uses_read_only_file_backing(tmp_path):
    path = tmp_path / "database.sql"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE data(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO data VALUES(1, 'kept')")
        connection.commit()
    original = path.read_bytes()
    source = load_source(path, {
        "resource_profile": "full_scale",
        "max_source_bytes": 1024 * 1024,
        "max_source_rows": 10,
        "max_graph_nodes": 10,
        "max_graph_edges": 10,
        "max_intermediate_rows": 10,
        "source_timeout_seconds": 10,
        "query_timeout_seconds": 10,
    })
    try:
        assert source._database_path == path
        assert source.tables["data"].rows == [{"id": 1, "value": "kept"}]
        with pytest.raises(sqlite3.OperationalError):
            source._connection.execute("DELETE FROM data")
        oracle = _oracle(source)
        try:
            assert oracle.execute("SELECT COUNT(*) FROM data").fetchone() == (1,)
            with pytest.raises(sqlite3.OperationalError):
                oracle.execute("DELETE FROM data")
        finally:
            oracle.close()
        assert path.read_bytes() == original
    finally:
        source.close()


def test_full_scale_file_backing_reads_committed_wal_rows(tmp_path):
    path = tmp_path / "wal.sql"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("CREATE TABLE data(id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO data VALUES(1)")
        writer.commit()
        writer.execute("INSERT INTO data VALUES(2)")
        writer.commit()
        source = load_source(path, {"resource_profile": "full_scale"})
        try:
            assert source.tables["data"].rows == [{"id": 1}, {"id": 2}]
            oracle = _oracle(source)
            try:
                assert oracle.execute("SELECT COUNT(*) FROM data").fetchone() == (2,)
            finally:
                oracle.close()
        finally:
            source.close()
    finally:
        writer.close()


def test_full_scale_oracle_rejects_changed_database_file(tmp_path):
    path = tmp_path / "changed.sql"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE data(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO data VALUES(1)")
        connection.commit()
    source = load_source(path, {"resource_profile": "full_scale"})
    try:
        original_identity = source._database_identity
        stat = path.stat()
        path.touch()
        if source._database_identity == (
                (stat.st_dev, stat.st_ino, stat.st_size, path.stat().st_mtime_ns), None, None):
            pytest.skip("Filesystem timestamp resolution cannot expose this test mutation")
        assert source._database_identity == original_identity
        with pytest.raises(ValueError, match="changed after loading"):
            _oracle(source)
        assert source_integrity_errors(source, initial_graph(source)) == [
            "SQLite source file changed after loading."]
    finally:
        source.close()
    assert source._database_path is None
    assert source._database_identity is None


def test_sqlite_binary_trigger_rejected_without_execution(tmp_path):
    path = tmp_path / "source.sql"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("CREATE TABLE data(id); CREATE TRIGGER t AFTER INSERT ON data BEGIN DELETE FROM data; END;")
    with pytest.raises(SourceError, match="triggers"):
        load_source(path)


def test_duplicate_keyless_sql_rows_all_get_their_fk_edge(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE parent(id INT PRIMARY KEY);
        CREATE TABLE child(pid INT REFERENCES parent(id));
        INSERT INTO parent VALUES(1);
        INSERT INTO child VALUES(1),(1),(NULL);
    """)
    try:
        graph = initial_graph(source)
        assert len(graph["nodes"]) == 4
        assert len(graph["edges"]) == 2
        assert len({edge["source"] for edge in graph["edges"]}) == 2
    finally:
        source.close()


@pytest.mark.parametrize("sql,message", [
    ("CREATE TABLE t(id TEXT PRIMARY KEY); INSERT INTO t VALUES(NULL);", "missing"),
    ("CREATE TABLE t(id INT); INSERT INTO t VALUES(1),(2);", "row limit"),
    ("CREATE TABLE t(id, p REFERENCES missing(id));", "referenced table"),
    ("CREATE TABLE p(id); CREATE TABLE c(pid REFERENCES p(id)); INSERT INTO p VALUES(1),(1);", "not unique"),
    ("CREATE TABLE t(id BLOB); INSERT INTO t VALUES(X'FF');", "BLOB"),
])
def test_sql_rejects_invalid_keys_non_json_data_and_row_overflow(tmp_path, sql, message):
    with pytest.raises(SourceError, match=message):
        sql_source(tmp_path, sql, max_source_rows=1 if message == "row limit" else 100)


def test_sql_dump_comments_literal_semicolons_and_autoincrement(tmp_path):
    source = sql_source(tmp_path, """
        -- standard SQLite dump
        PRAGMA foreign_keys=OFF;
        BEGIN TRANSACTION;
        CREATE TABLE data(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT);
        INSERT INTO data VALUES(1,'semi;colon -- still literal');
        DELETE FROM sqlite_sequence;
        INSERT INTO sqlite_sequence VALUES('data',1);
        COMMIT;
        /* trailing comment */
    """)
    try:
        assert list(source.tables) == ["data"]
        assert source.tables["data"].rows[0]["text"] == "semi;colon -- still literal"
    finally:
        source.close()


def test_schema_preserves_declared_collations_without_reading_check_literals(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE data(
            "id" TEXT COLLATE /* comment */ "NOCASE" PRIMARY KEY,
            value TEXT COLLATE [RTRIM],
            plain TEXT CHECK(plain COLLATE NOCASE != 'forbidden'),
            note TEXT DEFAULT 'COLLATE NOCASE'
        );
        INSERT INTO data VALUES('ID', 'space ', 'Allowed', 'literal');
    """)
    try:
        table = source.tables["data"]
        assert table.column_collations == {"id": "NOCASE", "value": "RTRIM", "plain": "BINARY", "note": "BINARY"}
        assert "CHECK" in table.create_sql
        graph = initial_graph(source)
        assert graph["nodes"][0]["properties"]["relational_schema"] == {
            "column_types": table.column_types,
            "column_collations": table.column_collations,
            "sqlite_affinity": True,
        }
        assert source_integrity_errors(source, graph) == []
    finally:
        source.close()


def test_source_integrity_covers_unqueried_rows_schema_and_relationships(tmp_path):
    source = sql_source(tmp_path, """
        CREATE TABLE p(id TEXT COLLATE NOCASE PRIMARY KEY);
        CREATE TABLE c(id INT PRIMARY KEY, pid TEXT REFERENCES p(id));
        INSERT INTO p VALUES('P');
        INSERT INTO c VALUES(1,'p'),(2,'P');
    """)
    try:
        graph = initial_graph(source)
        table = source.tables["c"]
        pairs = foreign_key_pairs(source, table, table.foreign_keys[0])
        assert pairs == [(0, 0), (1, 0)]
        pairs.clear()
        assert foreign_key_pairs(source, table, table.foreign_keys[0]) == [(0, 0), (1, 0)]
        assert source_integrity_errors(source, graph) == []
        missing_edge = copy.deepcopy(graph)
        missing_edge["edges"].pop()
        assert any("Missing 1 declared" in error for error in source_integrity_errors(source, missing_edge))
        changed_row = copy.deepcopy(graph)
        changed_row["nodes"][1]["properties"]["data"]["pid"] = "lost"
        assert any("values changed" in error for error in source_integrity_errors(source, changed_row))
        changed_schema = copy.deepcopy(graph)
        changed_schema["nodes"][0]["properties"]["relational_schema"]["sqlite_affinity"] = False
        assert any("semantics changed" in error for error in source_integrity_errors(source, changed_schema))
        duplicate = copy.deepcopy(graph)
        duplicate["edges"].append({**copy.deepcopy(graph["edges"][0]), "id": "duplicate"})
        assert any("Duplicate declared" in error for error in source_integrity_errors(source, duplicate))
    finally:
        source.close()


def test_sqlite_binary_unknown_custom_collation_fails_clearly(tmp_path):
    path = tmp_path / "source.sql"
    with closing(sqlite3.connect(path)) as connection:
        connection.create_collation("EXTERNAL", lambda a, b: (a > b) - (a < b))
        connection.execute("CREATE TABLE data(id TEXT COLLATE EXTERNAL)")
        connection.execute("INSERT INTO data VALUES('one')")
        connection.commit()
    with pytest.raises(SourceError, match="collation"):
        load_source(path)
