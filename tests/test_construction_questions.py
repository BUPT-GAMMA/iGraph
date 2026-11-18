import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from graph_data_service.construction import ConstructionError, ConstructionStore, graph_schema
from graph_data_service.construction_questions import graph_questions
from graph_data_service.construction_schema import schema_foreign_key
from graph_engine.construction.sources import ForeignKey, RelationalSource, Table, initial_graph


def stations():
    nodes = [{"id": key, "type": "车站", "properties": {"data": {"name": label}}}
             for key, label in (("a", "云桥站"), ("b", "南湾站"), ("c", "北林站"), ("d", "东城站"))]
    edges = [{"id": "e1", "type": "快线", "source": "a", "target": "b", "properties": {"耗时": "5"}},
             {"id": "e2", "type": "快线", "source": "a", "target": "c", "properties": {"耗时": "12"}}]
    return {"directed": True, "nodes": nodes, "edges": edges}


class ConstructionQuestionsTests(unittest.TestCase):
    def test_prompts_come_from_real_topology_and_values(self):
        graph = stations()
        result = graph_questions(graph)
        self.assertEqual(len(result["questions"]), 4)
        zh = result["questions"]
        self.assertTrue(all(text in zh[0] for text in ("云桥站", "快线")))
        self.assertIn("耗时", zh[1])
        self.assertTrue(all(text in zh[2] for text in ("北林站", "南湾站")))
        self.assertIn("没有任何连边", zh[3])
        self.assertEqual(len(result["translations"]["en"]["questions"]), len(zh))
        self.assertEqual(len(set(zh)), len(zh))
        self.assertEqual(graph_questions({"nodes": [], "edges": []})["questions"], [])
        graph["edges"][0]["properties"]["evidence"] = "source passage"
        self.assertTrue(any("原文片段" in q for q in graph_questions(graph)["questions"]))

    def test_order_questions_follow_joined_rows_with_or_without_a_line_primary_key(self):
        def ref(column, table):
            return ForeignKey([column], table, [column])

        customers = Table("customers", ["customer_id", "name"],
                          [{"customer_id": "C8", "name": "Ada"}, {"customer_id": "C9", "name": "Lin"}], ["customer_id"])
        products = Table("products", ["product_id", "name"],
                         [{"product_id": "P1", "name": "Pen"}, {"product_id": "P2", "name": "Paper"}], ["product_id"])
        orders = Table("orders", ["order_id", "customer_id"],
                       [{"order_id": "A", "customer_id": "C8"}, {"order_id": "B", "customer_id": "C9"}],
                       ["order_id"], [ref("customer_id", "customers")])
        fields = ["order_id", "line_no", "product_id", "quantity", "unit_price"]
        values = [("A", 1, "P1", 1, 10), ("A", 2, "P2", 2, 4), ("B", 1, "P1", 1, 10)]
        for key in (["order_id", "line_no"], []):
            with self.subTest(primary_key=key):
                items = Table("order_items", fields, [dict(zip(fields, row)) for row in values],
                              key, [ref("order_id", "orders"), ref("product_id", "products")])
                source = RelationalSource({t.name: t for t in (customers, items, orders, products)}, _csv_mode=True)
                graph = initial_graph(source)
                result = graph_questions(graph)
                self.assertIn("Ada", result["questions"][0])
                self.assertIn("数量乘以单价", result["questions"][1])
                self.assertIn("订购量最高", result["questions"][2])
                self.assertTrue(all(p in result["translations"]["en"]["questions"][3] for p in ("Pen", "Paper")))
                graph["edges"] = []
                self.assertNotIn("追踪客户购买记录", graph_questions(graph)["question_titles"])

    def test_save_reload_revision_and_owner(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "transport.csv"
            source.write_text("id,name\na,Center\n", encoding="utf-8")
            store = ConstructionStore(root)
            store.register_algorithm("csv", lambda *_: copy.deepcopy(stations()))
            draft = store.build("owner", "rail", source, source.name)
            with self.assertRaises(ConstructionError):
                store.questions("owner", "rail")
            saved = store.optimize("owner", "rail", draft["revision"], [], commit=True)
            first = ConstructionStore(root).questions("owner", "rail")
            self.assertEqual(first["revision"], saved["revision"])
            record_path = store._directory("owner", "rail") / "record.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["example_questions"]["questions"], first["questions"])
            record["example_questions"] = {"questions": ["obsolete examples"]}
            record.pop("example_questions_version")
            record_path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(store.questions("owner", "rail"), first)
            del record["example_questions"]
            record_path.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(store.questions("owner", "rail"), first)
            with self.assertRaises(ConstructionError):
                store.questions("other", "rail")
            operation = {"op": "rename_type", "target": "edge", "old_type": "快线", "new_type": "城际"}
            store.optimize("owner", "rail", first["revision"], [operation], commit=True)
            next_set = store.questions("owner", "rail")
            self.assertGreater(next_set["revision"], first["revision"])
            self.assertIn("城际", next_set["questions"][0])
            self.assertNotIn("快线", next_set["questions"][0])

    def test_schema_exposes_verified_foreign_key_columns_only(self):
        graph = stations()
        fk = {"table": "institution_shareholder_relationship", "columns": ["UUID"],
              "ref_table": "institution_main", "ref_columns": ["UUID"]}
        edge = graph["edges"][0]
        edge.update(type="fk_464ac5706e769fd8efe7b986bec5bb7b9cf4e13c", properties={"foreign_key": {**fk, "sample": "private"}})
        view = next(e for e in graph_schema(graph)["graph"]["edges"] if e["type"] == edge["type"])
        self.assertEqual(view["properties"]["foreign_key"], fk)
        self.assertEqual(view["label"], "UUID → institution_main.UUID")
        edge["properties"]["foreign_key"]["columns"] = ["mismatch"]
        self.assertIsNone(schema_foreign_key(edge))
