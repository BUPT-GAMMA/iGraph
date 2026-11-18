import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from graph_data_service.construction import ConstructionError, ConstructionStore, graph_schema
from graph_data_service.construction_questions import graph_questions
from graph_data_service.construction_schema import schema_foreign_key


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
