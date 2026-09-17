from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graph_engine.construction.workload import GraphExecutor, evaluate_workload, generate_workload, validate_queries


def fixture():
    def table(name, columns, rows, pk, foreign_keys=()):
        return SimpleNamespace(name=name, columns=columns, rows=rows, primary_key=pk,
                               foreign_keys=list(foreign_keys), column_types={})
    region_fk = {"table": "customers", "columns": ["region_id"], "ref_table": "regions", "ref_columns": ["id"]}
    customer_fk = {"table": "orders", "columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}
    source = SimpleNamespace(domain="电商订单履约", tables={
        "regions": table("regions", ["id", "name"], [{"id": 1, "name": "华东"}, {"id": 2, "name": "华北"}], ["id"]),
        "customers": table("customers", ["id", "region_id", "name"], [
            {"id": 10, "region_id": 1, "name": "甲"}, {"id": 11, "region_id": 1, "name": "乙"},
            {"id": 12, "region_id": None, "name": "丙"}], ["id"], [region_fk]),
        "orders": table("orders", ["id", "customer_id", "amount", "status"], [
            {"id": 100, "customer_id": 10, "amount": 10.1, "status": "paid"},
            {"id": 101, "customer_id": 10, "amount": 20.2, "status": "paid"},
            {"id": 102, "customer_id": 11, "amount": None, "status": "pending"},
            {"id": 103, "customer_id": 12, "amount": 0, "status": "cancelled"},
            {"id": 104, "customer_id": None, "amount": 4, "status": "pending"}], ["id"], [customer_fk]),
    })
    nodes = [{"id": f"{name}:{i}", "type": name,
              "properties": {"table": name, "row_index": i, "data": copy.deepcopy(row)}}
             for name, spec in source.tables.items() for i, row in enumerate(spec.rows)]
    edges = []
    for name, spec in source.tables.items():
        for f in spec.foreign_keys:
            for child in [n for n in nodes if n["properties"]["table"] == name]:
                for parent in [n for n in nodes if n["properties"]["table"] == f["ref_table"]]:
                    values = [child["properties"]["data"][c] for c in f["columns"]]
                    if None not in values and values == [parent["properties"]["data"][c] for c in f["ref_columns"]]:
                        edges.append({"id": f"e{len(edges)}", "source": child["id"], "target": parent["id"],
                                      "type": "fk", "properties": {"foreign_key": f}})
    return source, {"nodes": nodes, "edges": edges}, customer_fk, region_fk


def query(table="orders", **parts):
    result = {"id": "test", "question": "核对业务数据", "root": {"table": table, "alias": "t0"}}
    result.update(parts)
    return result


def join(f, alias="t1", from_alias="t0", direction="forward", kind="inner"):
    return {"from": from_alias, "alias": alias, "table": f["ref_table"] if direction == "forward" else f["table"],
            "direction": direction, "kind": kind, "foreign_key": f}


class ConstructionWorkloadTest(unittest.TestCase):
    def setUp(self):
        self.source, self.graph, self.customer_fk, self.region_fk = fixture()

    def evaluate(self, q):
        return evaluate_workload(self.source, self.graph, [q], repeats=1)

    def test_generated_workload_is_grounded_diverse_and_correct(self):
        queries = generate_workload(self.source)
        self.assertEqual(len(queries), 15)
        self.assertEqual(len({q["root"]["table"] for q in queries}), 3)
        self.assertTrue(any(len(q["joins"]) >= 2 for q in queries))
        self.assertTrue(any(q["filters"] and "value" in q["filters"][0] for q in queries))
        self.assertTrue(all("电商订单履约" in q["question"] for q in queries))
        report = evaluate_workload(self.source, self.graph, queries, repeats=2)
        self.assertEqual(report["accuracy"], 1, report)
        self.assertGreaterEqual(report["preparation_ms"], 0)
        self.assertEqual(report["total"], 15)

    def test_all_generated_plans_match_independent_oracle(self):
        report = evaluate_workload(self.source, self.graph, generate_workload(self.source, 100), repeats=1)
        self.assertEqual(report["accuracy"], 1, [r for r in report["results"] if not r["correct"]])

    def test_workload_balances_nine_roots_even_when_the_first_table_is_wide(self):
        first = self.source.tables["regions"]
        for i in range(80):
            column = f"business_attribute_{i}"
            first.columns.append(column)
            for row in first.rows:
                row[column] = f"value_{i}"
        for i in range(6):
            name = f"activity_{i}"
            self.source.tables[name] = SimpleNamespace(
                name=name, columns=["Id", "Status", "Score"], primary_key=["Id"], foreign_keys=[],
                column_types={}, rows=[{"Id": i + 1, "Status": "active", "Score": i + 10}])
        queries = generate_workload(self.source, 15)
        root_counts = {table: sum(q["root"]["table"] == table for q in queries) for table in self.source.tables}
        self.assertEqual(sum(value > 0 for value in root_counts.values()), 9, root_counts)
        self.assertLessEqual(max(root_counts.values()), 2, root_counts)
        queries = generate_workload(self.source, 5)
        self.assertEqual(len({q["root"]["table"] for q in queries}), 5)

    def test_identifier_fields_are_not_treated_as_business_measures(self):
        queries = generate_workload(self.source, 100)
        measures = [a["column"] for q in queries for a in q["aggregations"] if a["function"] in ("sum", "avg")]
        self.assertTrue(measures)
        self.assertEqual(set(measures), {"t0.amount"})
        for q in queries:
            self.assertFalse(any(column.endswith((".id", "_id")) for column in q["group_by"]))
        first_lookup = next(q for q in queries if any(f["op"] == "eq" for f in q["filters"]))
        self.assertEqual(first_lookup["filters"][0]["column"], "t0.name")
        # Exact primary-key lookup remains a legitimate detailed query.
        self.assertTrue(any(q["projection"] and any(f["column"] == "t0.id" and f["op"] == "eq" for f in q["filters"])
                            for q in queries))

    def test_multi_hop_plans_have_actual_source_paths_without_using_graph_answers(self):
        from graph_engine.construction.sources import initial_graph, load_source

        ddl = """CREATE TABLE Users(Id INTEGER PRIMARY KEY, DisplayName TEXT);
        CREATE TABLE Badges(Id INTEGER PRIMARY KEY, UserId INTEGER REFERENCES Users(Id), Name TEXT);
        CREATE TABLE Posts(Id INTEGER PRIMARY KEY, OwnerUserId INTEGER REFERENCES Users(Id), Score INTEGER);
        CREATE TABLE Comments(Id INTEGER PRIMARY KEY, UserId INTEGER REFERENCES Users(Id), PostId INTEGER REFERENCES Posts(Id), Body TEXT);
        INSERT INTO Users VALUES (1,'Alice'),(2,'Bob');
        INSERT INTO Badges VALUES (1,1,'gold');
        INSERT INTO Posts VALUES (1,2,10);
        INSERT INTO Comments VALUES (1,2,1,'helpful');"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sql"
            path.write_text(ddl, encoding="utf-8")
            source = load_source(path)
            try:
                with patch("graph_engine.construction.workload.GraphExecutor", side_effect=AssertionError("Generation must not execute a graph")):
                    queries = generate_workload(source, 60)
                self.assertEqual(len({q["root"]["table"] for q in queries[:15]}), 4)
                multi_hop = [q for q in queries if len(q["joins"]) >= 2]
                self.assertTrue(multi_hop)
                self.assertFalse(any(q["root"]["table"] == "Badges" and [j["table"] for j in q["joins"][:2]] == ["Users", "Comments"]
                                     for q in multi_hop))
                report = evaluate_workload(source, initial_graph(source), multi_hop, repeats=1)
                self.assertEqual(report["accuracy"], 1, report)
                for q, result in zip(multi_hop, report["results"]):
                    if q["aggregations"]:
                        self.assertGreater(result["expected_preview"][0][0], 0)
                    else:
                        self.assertGreater(result["expected_count"], 0)
            finally:
                source.close()

    def test_missing_fk_edge_fails_instead_of_rejoining_source_rows(self):
        self.graph["edges"] = [e for e in self.graph["edges"] if e["source"] != "orders:0"]
        report = self.evaluate(query(joins=[join(self.customer_fk)], aggregations=[{"function": "count", "column": "*", "as": "n"}]))
        self.assertEqual(report["accuracy"], 0)
        self.assertEqual(report["results"][0]["expected_preview"], [[4]])
        self.assertEqual(report["results"][0]["actual_preview"], [[3]])

    def test_corrupted_graph_values_fail_even_if_source_retains_original(self):
        self.graph["nodes"][-1]["properties"]["data"]["amount"] = 999
        report = self.evaluate(query(aggregations=[{"function": "sum", "column": "t0.amount", "as": "n"}]))
        self.assertEqual(report["accuracy"], 0)

    def test_duplicate_rows_and_null_count_are_preserved(self):
        q = query(projection=[{"column": "t0.status", "as": "status"}])
        result = self.evaluate(q)
        self.assertEqual(result["accuracy"], 1)
        self.assertEqual(result["results"][0]["actual_count"], 5)
        result = self.evaluate(query(aggregations=[{"function": "count", "column": "t0.amount", "as": "n"}]))
        self.assertEqual(result["results"][0]["actual_preview"], [[4]])

    def test_left_reverse_joins_and_group_aggregations(self):
        q = query("customers", joins=[join(self.customer_fk, direction="reverse", kind="left")],
                  projection=[{"column": "t0.id", "as": "id"}], group_by=["t0.id"],
                  aggregations=[{"function": "count", "column": "t1.id", "as": "n"},
                                {"function": "sum", "column": "t1.amount", "as": "total"},
                                {"function": "avg", "column": "t1.amount", "as": "average"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)

    def test_empty_aggregate_and_null_predicate_semantics(self):
        for op in ("eq", "ne", "is_null", "not_null", "in"):
            predicate = {"column": "t0.amount", "op": op}
            if op == "in":
                predicate["value"] = [None, 0]
            elif op in ("eq", "ne"):
                predicate["value"] = None
            q = query(filters=[predicate], aggregations=[{"function": f, "column": "t0.amount", "as": f}
                                                         for f in ("count", "sum", "min", "max", "avg")])
            self.assertEqual(self.evaluate(q)["accuracy"], 1, op)

    def test_order_ties_are_multisets_and_limit_is_deterministic(self):
        self.graph["nodes"].reverse()
        q = query(projection=[{"column": "t0.status", "as": "status"}, {"column": "t0.id", "as": "id"}],
                  order_by=[{"column": "status", "direction": "asc"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)
        q["limit"] = 3
        with self.assertRaises(ValueError):
            self.evaluate(q)
        q["order_by"].append({"column": "id", "direction": "desc"})
        self.assertEqual(self.evaluate(q)["accuracy"], 1)

    def test_identifier_and_literal_validation(self):
        for invalid in (
            query(projection=[{"column": "t0.id; DROP TABLE orders", "as": "x"}]),
            query(projection=[{"column": "t0.id", "as": "x"}], sql="SELECT 1"),
            query(projection=[{"column": "t0.id", "as": "x"}], filters=[{"column": "t0.id", "op": "eq", "value": float("nan")}]),
            query(joins=[dict(join(self.customer_fk), foreign_key=dict(self.customer_fk, columns=["amount"]))],
                  aggregations=[{"function": "count", "column": "*", "as": "n"}]),
        ):
            with self.assertRaises(ValueError):
                validate_queries(self.source, [invalid])
        q = query(projection=[{"column": "t0.status", "as": "status"}],
                  filters=[{"column": "t0.status", "op": "eq", "value": "'; DROP TABLE orders; --"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)

    def test_no_duplicate_plans_count_as_coverage(self):
        q = query(aggregations=[{"function": "count", "column": "*", "as": "n"}])
        duplicate = dict(q, id="another", question="Different wording")
        with self.assertRaisesRegex(ValueError, "Duplicate query plans"):
            validate_queries(self.source, [q, duplicate])

    def test_output_and_join_alias_changes_cannot_fake_independent_queries(self):
        q = query(joins=[join(self.customer_fk)],
                  aggregations=[{"function": "count", "column": "t1.id", "as": "n"}],
                  filters=[{"column": "t0.id", "op": "gt", "value": 0},
                           {"column": "t1.id", "op": "lt", "value": 100}])
        duplicate = copy.deepcopy(q)
        duplicate["id"] = "alternate"
        duplicate["joins"][0]["alias"] = "customer"
        duplicate["aggregations"][0].update(column="customer.id", **{"as": "total"})
        duplicate["filters"][1]["column"] = "customer.id"
        duplicate["filters"].reverse()
        with self.assertRaisesRegex(ValueError, "Duplicate query plans"):
            validate_queries(self.source, [q, duplicate])

    def test_source_sqlite_collation_affinity_and_real_oracle_are_preserved(self):
        from graph_engine.construction.sources import initial_graph, load_source

        ddl = """CREATE TABLE customers(id TEXT COLLATE NOCASE PRIMARY KEY, label TEXT COLLATE RTRIM);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id TEXT REFERENCES customers(id), code TEXT, amount NUMERIC);
        INSERT INTO customers VALUES ('abc','A '), ('DEF','B');
        INSERT INTO orders VALUES (1,'ABC','1',1), (2,'def','01',2), (3,NULL,'2',3);"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sql"
            path.write_text(ddl, encoding="utf-8")
            self.source = load_source(path)
            try:
                self.graph = initial_graph(self.source)
                fk = {"table": "orders", "columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}
                plans = [query(joins=[join(fk)], aggregations=[{"function": "count", "column": "*", "as": "n"}]),
                         query(filters=[{"column": "t0.code", "op": "eq", "value": 1}], projection=[{"column": "t0.id", "as": "id"}]),
                         query(filters=[{"column": "t0.amount", "op": "eq", "value": "1"}], projection=[{"column": "t0.id", "as": "id"}]),
                         query("customers", filters=[{"column": "t0.id", "op": "eq", "value": "ABC"}], projection=[{"column": "t0.id", "as": "id"}]),
                         query("customers", filters=[{"column": "t0.label", "op": "eq", "value": "A"}], projection=[{"column": "t0.id", "as": "id"}])]
                for plan in plans:
                    report = self.evaluate(plan)
                    self.assertEqual(report["accuracy"], 1, report)
                self.assertEqual(self.evaluate(plans[0])["results"][0]["expected_preview"], [[2]])
                self.assertEqual(self.evaluate(plans[1])["results"][0]["expected_preview"], [[1]])
                report = evaluate_workload(self.source, self.graph, generate_workload(self.source, 30), repeats=1)
                self.assertEqual(report["accuracy"], 1, [r for r in report["results"] if not r["correct"]])
                plans[1]["filters"][0]["value"] = 1.0
                with self.assertRaisesRegex(ValueError, "Float-to-TEXT"):
                    validate_queries(self.source, [plans[1]])
            finally:
                self.source.close()

    def test_collation_order_ties_allow_any_order_within_the_same_sql_key(self):
        from graph_engine.construction.sources import initial_graph, load_source

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sql"
            path.write_text("CREATE TABLE t(id INTEGER PRIMARY KEY, label TEXT COLLATE NOCASE); INSERT INTO t VALUES (1,'a'),(2,'A'),(3,'B');", encoding="utf-8")
            self.source = load_source(path)
            try:
                self.graph = initial_graph(self.source)
                self.graph["nodes"].reverse()
                q = query("t", projection=[{"column": "t0.label", "as": "label"}, {"column": "t0.id", "as": "id"}],
                          order_by=[{"column": "label", "direction": "asc"}])
                self.assertEqual(self.evaluate(q)["accuracy"], 1)
                q["projection"].pop()
                q["limit"] = 1
                result = self.evaluate(q)
                self.assertEqual(result["accuracy"], 1)
                self.assertEqual(result["results"][0]["expected_preview"], [["A"]])
            finally:
                self.source.close()

    def test_same_table_joins_preserve_distinct_foreign_key_roles(self):
        from graph_engine.construction.sources import initial_graph, load_source

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sql"
            path.write_text("""CREATE TABLE staff(id INTEGER PRIMARY KEY,
                manager_id INTEGER REFERENCES staff(id), mentor_id INTEGER REFERENCES staff(id));
                INSERT INTO staff VALUES (1,2,3),(2,1,NULL),(3,NULL,1);""", encoding="utf-8")
            self.source = load_source(path)
            try:
                self.graph = initial_graph(self.source)
                manager = {"table": "staff", "columns": ["manager_id"], "ref_table": "staff", "ref_columns": ["id"]}
                mentor = dict(manager, columns=["mentor_id"])
                q = query("staff", joins=[join(manager, kind="left"), join(mentor, "t2", "t0", kind="left")],
                          projection=[{"column": "t0.id", "as": "employee"}, {"column": "t1.id", "as": "manager"},
                                      {"column": "t2.id", "as": "mentor"}])
                self.assertEqual(self.evaluate(q)["accuracy"], 1)
                for edge in self.graph["edges"]:
                    edge["source"], edge["target"] = edge["target"], edge["source"]
                self.assertEqual(self.evaluate(q)["accuracy"], 1)
                self.graph["edges"].pop()
                self.assertEqual(self.evaluate(q)["accuracy"], 0)
            finally:
                self.source.close()

    def test_exceeding_intermediate_or_oracle_limit_is_failure_not_truncation(self):
        q = query(projection=[{"column": "t0.id", "as": "id"}])
        with patch("graph_engine.construction.workload.MAX_INTERMEDIATE_ROWS", 2):
            report = self.evaluate(q)
            self.assertEqual(report["accuracy"], 0)
            self.assertIn("Oracle", report["results"][0]["error"])
            q = query(aggregations=[{"function": "count", "column": "*", "as": "n"}])
            report = self.evaluate(q)
            self.assertEqual(report["accuracy"], 0)
            self.assertIn("intermediate", report["results"][0]["error"])

    def test_expired_query_deadline_is_failure(self):
        with patch("graph_engine.construction.workload.QUERY_TIMEOUT_SECONDS", -1):
            result = self.evaluate(query(aggregations=[{"function": "count", "column": "*", "as": "n"}]))
            self.assertEqual(result["accuracy"], 0)
            self.assertIn("exceeded", result["results"][0]["error"])

    def test_malformed_json_types_are_validation_errors(self):
        base = query(aggregations=[{"function": "count", "column": "*", "as": "n"}])
        for changed in (dict(base, root={"table": [], "alias": "t0"}),
                        dict(base, joins=[dict(join(self.customer_fk), **{"from": []})]),
                        dict(base, filters=[{"column": "t0.id", "op": [], "value": 1}]),
                        dict(base, aggregations=[{"function": {}, "column": "*", "as": "n"}]),
                        dict(base, order_by=[{"column": [], "direction": "asc"}])):
            with self.assertRaises(ValueError):
                validate_queries(self.source, [changed])

    def test_composite_fk_and_quoted_identifiers(self):
        fk = {"table": 'line items', "columns": ['tenant id', 'account"id'],
              "ref_table": 'accounts', "ref_columns": ['tenant id', 'id']}
        self.source = SimpleNamespace(tables={
            'accounts': SimpleNamespace(columns=['tenant id', 'id'], primary_key=['tenant id', 'id'],
                                        rows=[{'tenant id': 1, 'id': 2}, {'tenant id': 2, 'id': 2}], foreign_keys=[]),
            'line items': SimpleNamespace(columns=['tenant id', 'account"id'], primary_key=[], foreign_keys=[fk],
                                          rows=[{'tenant id': 1, 'account"id': 2}, {'tenant id': None, 'account"id': 2}])})
        self.graph = {"nodes": [
            {"id": "a", "properties": {"table": "accounts", "row_index": 0, "data": {'tenant id': 1, 'id': 2}}},
            {"id": "b", "properties": {"table": "accounts", "row_index": 1, "data": {'tenant id': 2, 'id': 2}}},
            {"id": "c", "properties": {"table": "line items", "row_index": 0, "data": {'tenant id': 1, 'account"id': 2}}},
            {"id": "d", "properties": {"table": "line items", "row_index": 1, "data": {'tenant id': None, 'account"id': 2}}}],
            "edges": [{"id": "e", "source": "c", "target": "a", "properties": {"foreign_key": fk}}]}
        q = query('line items', joins=[join(fk, kind='left')],
                  projection=[{"column": 't0.tenant id', "as": 'tenant'}, {"column": 't1.id', "as": 'account'}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)
        self.graph["edges"][0]["target"] = "b"
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_mixed_storage_classes_use_sqlite_null_number_text_order(self):
        values = [None, 2, "2", 1.5, "a", "", False]
        spec = SimpleNamespace(columns=["value"], rows=[{"value": v} for v in values], primary_key=[], foreign_keys=[])
        self.source = SimpleNamespace(tables={"mixed": spec})
        self.graph = {"nodes": [{"id": i, "properties": {"table": "mixed", "row_index": i, "data": r}} for i, r in enumerate(spec.rows)], "edges": []}
        for op in ("eq", "ne", "lt", "le", "gt", "ge", "in"):
            q = query("mixed", projection=[{"column": "t0.value", "as": "value"}],
                      filters=[{"column": "t0.value", "op": op, "value": [2, None] if op == "in" else 2}],
                      order_by=[{"column": "value", "direction": "asc"}])
            self.assertEqual(self.evaluate(q)["accuracy"], 1, op)

    def test_reversed_storage_and_renamed_types_keep_fk_semantics(self):
        for edge in self.graph["edges"]:
            edge["source"], edge["target"] = edge["target"], edge["source"]
        for node in self.graph["nodes"]:
            node["type"] = "Renamed"
        self.assertEqual(evaluate_workload(self.source, self.graph, generate_workload(self.source), repeats=1)["accuracy"], 1)

    def _materialize(self, reverse=False):
        if reverse:
            path = [dict(self.region_fk, direction="reverse"), dict(self.customer_fk, direction="reverse")]
            node_ids = [["regions:0", "customers:0", "orders:0"], ["regions:0", "customers:0", "orders:1"],
                        ["regions:0", "customers:1", "orders:2"]]
        else:
            path = [dict(self.customer_fk, direction="forward"), dict(self.region_fk, direction="forward")]
            node_ids = [["orders:0", "customers:0", "regions:0"], ["orders:1", "customers:0", "regions:0"],
                        ["orders:2", "customers:1", "regions:0"]]
        for ids in node_ids:
            self.graph["edges"].append({"id": "shortcut:" + ":".join(ids), "source": ids[0], "target": ids[-1],
                                        "properties": {"materialized_join": {"path": path, "node_ids": ids}}})

    def test_materialized_path_reads_current_intermediate_entities(self):
        self._materialize()
        q = query(joins=[join(self.customer_fk), join(self.region_fk, "t2", "t1")],
                  projection=[{"column": "t0.id", "as": "order"}, {"column": "t1.name", "as": "customer"},
                              {"column": "t2.name", "as": "region"}])
        result = self.evaluate(q)
        self.assertEqual(result["accuracy"], 1)
        self.assertGreater(result["results"][0]["materialized_path_hits"], 0)
        self.graph["nodes"][2]["properties"]["data"]["name"] = "changed"
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_partial_shortcuts_fall_back_and_preserve_path_multiplicity(self):
        self._materialize(reverse=True)
        self.graph["edges"].pop()
        q = query("regions", joins=[join(self.region_fk, direction="reverse"),
                                     join(self.customer_fk, "t2", "t1", direction="reverse")],
                  projection=[{"column": "t0.name", "as": "region"}],
                  aggregations=[{"function": "count", "column": "*", "as": "n"}], group_by=["t0.name"])
        result = self.evaluate(q)
        self.assertEqual(result["accuracy"], 1)
        self.assertEqual(result["results"][0]["materialized_path_hits"], 0)
        self.assertEqual(result["results"][0]["actual_preview"], [["华东", 3]])

    def test_shortcuts_cannot_hide_deleted_original_relationship(self):
        self._materialize()
        self.graph["edges"] = [e for e in self.graph["edges"]
                               if not (e["source"] == "orders:0" and "foreign_key" in e["properties"])]
        q = query(joins=[join(self.customer_fk), join(self.region_fk, "t2", "t1")],
                  aggregations=[{"function": "count", "column": "*", "as": "n"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_collapse_provenance_is_never_expanded(self):
        removed = self.graph["nodes"].pop()
        self.graph["edges"].append({"id": "collapsed", "source": "orders:0", "target": "customers:0",
                                    "properties": {"_collapsed_node": removed, "_collapsed_edges": []}})
        q = query(aggregations=[{"function": "count", "column": "*", "as": "n"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_current_edge_can_carry_bridge_row_and_its_actual_fk_roles(self):
        region_fk = {"table": "orders", "columns": ["region_id"], "ref_table": "regions", "ref_columns": ["id"]}
        self.source.tables["orders"].columns.append("region_id")
        self.source.tables["orders"].foreign_keys.append(region_fk)
        for i, row in enumerate(self.source.tables["orders"].rows):
            row["region_id"] = 1 if i == 0 else None
        for node in self.graph["nodes"]:
            if node["properties"]["table"] == "orders":
                node["properties"]["data"]["region_id"] = 1 if node["id"] == "orders:0" else None
        row = next(n for n in self.graph["nodes"] if n["id"] == "orders:0")
        self.graph["nodes"] = [n for n in self.graph["nodes"] if n["id"] != row["id"]]
        self.graph["edges"] = [e for e in self.graph["edges"] if e["source"] != row["id"]]
        bridge = {"id": row["id"], "source": "customers:0", "target": "regions:0", "type": "order_record", "properties": row["properties"]}
        bridge["properties"]["row_endpoints"] = [
            {"foreign_key": self.customer_fk, "node_id": "customers:0"},
            {"foreign_key": region_fk, "node_id": "regions:0"}]
        self.graph["edges"].append(bridge)
        q = query(joins=[join(self.customer_fk), join(region_fk, "t2", "t0")],
                  filters=[{"column": "t0.id", "op": "eq", "value": 100}],
                  projection=[{"column": "t0.amount", "as": "amount"}, {"column": "t1.name", "as": "customer"},
                              {"column": "t2.name", "as": "region"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)
        bridge["properties"]["data"]["amount"] = 999
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_queries_read_extracted_values_from_current_entity(self):
        row = next(n for n in self.graph["nodes"] if n["id"] == "orders:0")
        value = row["properties"]["data"].pop("status")
        entity = {"id": "paid_status", "type": "Status", "properties": {"value": value}}
        self.graph["nodes"].append(entity)
        self.graph["edges"].append({"id": "status_edge", "source": row["id"], "target": entity["id"],
                                    "properties": {"extracted_attribute": {"table": "orders", "column": "status"}}})
        q = query(filters=[{"column": "t0.status", "op": "eq", "value": "paid"}],
                  aggregations=[{"function": "count", "column": "*", "as": "n"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)
        entity["properties"]["value"] = "unpaid"
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_queries_read_relocated_attribute_from_current_incident_edge(self):
        row = next(n for n in self.graph["nodes"] if n["id"] == "orders:0")
        edge = next(e for e in self.graph["edges"] if e["source"] == row["id"])
        value = row["properties"]["data"].pop("amount")
        edge["properties"]["relocated_attributes"] = [{"row_id": row["id"], "column": "amount", "value": value}]
        q = query(aggregations=[{"function": "sum", "column": "t0.amount", "as": "total"}])
        self.assertEqual(self.evaluate(q)["accuracy"], 1)
        edge["properties"]["relocated_attributes"][0]["value"] = 999
        self.assertEqual(self.evaluate(q)["accuracy"], 0)

    def test_executor_has_no_source_or_sql_dependency(self):
        q = validate_queries(self.source, [query(aggregations=[{"function": "count", "column": "*", "as": "n"}])])[0]
        self.source.tables.clear()
        self.assertEqual(GraphExecutor(self.graph).execute(q), [[5]])


if __name__ == "__main__":
    unittest.main()
