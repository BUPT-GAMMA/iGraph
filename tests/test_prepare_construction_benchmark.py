from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.prepare_construction_benchmark import SampleError, prepare


@unittest.skipUnless(importlib.util.find_spec("pyarrow") and importlib.util.find_spec("yaml"),
                     "Optional benchmark dependencies PyArrow and PyYAML required")
class PrepareConstructionBenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.specs = []

    def table(self, name, rows, columns):
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist(rows), self.root / f"{name}.pqt", row_group_size=2)
        self.specs.append({"name": name, "source": f"{name}.pqt", "format": "parquet", "columns": columns})

    def export(self, **options):
        import yaml
        (self.root / "metadata.yaml").write_text(yaml.safe_dump({"dataset_name": "fixture", "tables": self.specs}))
        sql, manifest = prepare(self.root, seed_rows=1, batch_size=2, **options)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(sql)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        return conn, manifest

    def test_recursive_closure_fetches_original_rows_and_attributes(self):
        self.table("Event", [{"Id": 1, "PostId": 30, "Text": "O'Brien"}], [
            {"name": "Id", "dtype": "primary_key"},
            {"name": "PostId", "dtype": "foreign_key", "link_to": "Post.Id"},
            {"name": "Text", "dtype": "text"},
        ])
        self.table("Post", [
            {"Id": 10, "ParentId": None, "Body": "seed body"},
            {"Id": 20, "ParentId": 10, "Body": "actual parent"},
            {"Id": 30, "ParentId": 20, "Body": "actual referenced body"},
            {"Id": 40, "ParentId": None, "Body": "not sampled"},
        ], [{"name": "Id", "dtype": "primary_key"},
            {"name": "ParentId", "dtype": "foreign_key", "link_to": "Post.Id"},
            {"name": "Body", "dtype": "text"}])
        conn, manifest = self.export()
        self.assertEqual(conn.execute('SELECT Id, Body FROM Post ORDER BY Id').fetchall(), [
            (10, "seed body"), (20, "actual parent"), (30, "actual referenced body")])
        self.assertEqual(conn.execute('SELECT Text FROM Event').fetchone()[0], "O'Brien")
        post = next(table for table in manifest["tables"] if table["table"] == "Post")
        self.assertEqual(post["source_total_rows"], 4)
        self.assertEqual(post["sample_rows"], 3)
        self.assertEqual(post["rows_added_by_fk_closure"], 2)
        self.assertGreaterEqual(manifest["closure_rounds"], 2)

    def test_missing_real_rows_are_pruned_without_inventing_properties(self):
        self.table("Event", [{"Id": 1, "UserId": 99, "TagId": 8}], [
            {"name": "Id", "dtype": "primary_key"},
            {"name": "UserId", "dtype": "foreign_key", "link_to": "User.Id"},
            {"name": "TagId", "dtype": "foreign_key", "link_to": "Tag.Id"},
        ])
        self.table("User", [{"Id": 2, "Name": "Real user"}], [
            {"name": "Id", "dtype": "primary_key"}, {"name": "Name", "dtype": "text"}])
        conn, manifest = self.export()
        self.assertEqual(conn.execute('SELECT * FROM Event').fetchall(), [])
        self.assertEqual(conn.execute('SELECT * FROM User').fetchall(), [(2, "Real user")])
        event = next(table for table in manifest["tables"] if table["table"] == "Event")
        self.assertEqual(len(event["dropped_rows"]), 1)
        virtual = next(table for table in manifest["tables"] if table["table"] == "Tag")
        self.assertTrue(virtual["virtual"])
        self.assertIsNone(virtual["source_total_rows"])
        self.assertEqual(virtual["columns"], {"Id": "INTEGER"})
        self.assertEqual(virtual["sample_rows"], 0)

    def test_bounds_and_missing_rows_error_instead_of_exporting_incomplete_data(self):
        self.table("Event", [{"Id": 1, "UserId": 99}], [
            {"name": "Id", "dtype": "primary_key"},
            {"name": "UserId", "dtype": "foreign_key", "link_to": "User.Id"},
        ])
        self.table("User", [{"Id": 2}], [{"name": "Id", "dtype": "primary_key"}])
        with self.assertRaisesRegex(SampleError, "absent from real table"):
            self.export(missing_reference_policy="error")
        with self.assertRaisesRegex(SampleError, "max-rows"):
            self.export(max_rows=1)

    def test_no_pk_rows_remain_distinct_and_composite_pk_is_declared(self):
        self.table("Pair", [{"Left": 1, "Right": 2}, {"Left": 1, "Right": 3}], [
            {"name": "Left", "dtype": "primary_key"}, {"name": "Right", "dtype": "primary_key"}])
        self.table("Duplicates", [{"Value": "same"}, {"Value": "same"}], [{"name": "Value", "dtype": "text"}])
        import yaml
        (self.root / "metadata.yaml").write_text(yaml.safe_dump({"tables": self.specs}))
        sql, manifest = prepare(self.root, seed_rows=2)
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(sql)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM Duplicates').fetchone()[0], 2)
        self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(Pair)') if row[5]], ["Left", "Right"])
        self.assertEqual(manifest["total_sample_rows"], 4)


if __name__ == "__main__":
    unittest.main()
