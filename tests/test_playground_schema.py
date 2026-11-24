import json
import tempfile
import unittest
import uuid
from pathlib import Path

from graph_data_service.construction import ConstructionError, ConstructionStore, _atomic_json
from graph_data_service.playground_schema import EditBatch, PendingEdits, apply_edits, candidate, key, saved_schema, snapshot


def fixture():
    return {"revision": 1, "status": "ready", "playground": True,
            "graph": {"directed": True,
                      "nodes": [{"id": i, "type": name, "properties": {"name": i, "cost": 7}}
                                for i, name in [("d1", "Depot"), ("d2", "Depot"), ("s1", "Store"), ("s2", "Store")]],
                      "edges": [{"id": "e1", "source": "d1", "target": "s1", "type": "links", "properties": {"weight": 4}},
                                {"id": "e2", "source": "s1", "target": "s2", "type": "links", "properties": {}}]},
            "generated_questions": {"questions": ["哪个仓库？"], "translations": {"en": {"questions": ["Which depot?"]}}}}


class SchemaEditTest(unittest.TestCase):
    def test_legacy_schema_and_in_place_rename_of_only_one_edge_signature(self):
        original = fixture()
        old = snapshot(original["graph"], {}, 1)
        self.assertEqual(sum(x["count"] for x in old["nodes"]), 4)
        self.assertEqual(len(old["edges"]), 2)
        self.assertTrue(all(row["description"]["zh"] and row["description"]["en"] for row in old["edges"] + old["nodes"]))
        batch = EditBatch(changes=[
            {"kind": "node", "action": "update", "name": "Depot", "new_name": "Hub"},
            {"kind": "edge", "action": "update", "name": "links", "source": "Hub", "target": "Store",
             "new_name": "delivers", "description": {"zh": "为门店送货。", "en": "Delivers goods to the store."}}])
        edited, meanings, _ = candidate(original, batch)
        self.assertEqual(original, fixture())
        self.assertEqual([n["id"] for n in edited["nodes"]], [n["id"] for n in original["graph"]["nodes"]])
        self.assertEqual(edited["edges"][0], {**original["graph"]["edges"][0], "type": "delivers"})
        self.assertEqual(edited["edges"][1], original["graph"]["edges"][1])
        self.assertEqual(meanings[key("delivers", "Hub", "Store")]["zh"], "为门店送货。")
        with self.assertRaises(ValueError):
            candidate(original, EditBatch(changes=[{"kind": "edge", "action": "remove", "name": "links"}]))

    def test_add_reconnect_reverse_and_cascade_remove(self):
        edits = [{"kind": "node", "action": "add", "name": "Supplier", "examples": ["Cedar", "Maple"]},
                 {"kind": "edge", "action": "add", "name": "supplies", "source": "Supplier", "target": "Depot"},
                 {"kind": "edge", "action": "update", "name": "supplies", "new_target": "Store"},
                 {"kind": "edge", "action": "reverse", "name": "supplies"},
                 {"kind": "node", "action": "remove", "name": "Depot"}]
        graph, meanings, _ = candidate(fixture(), EditBatch(changes=edits))
        actual = snapshot(graph, meanings, 2)
        self.assertEqual({n["type"] for n in actual["nodes"]}, {"Supplier", "Store"})
        self.assertEqual({(r["source"], r["type"], r["target"], r["count"]) for r in actual["edges"]},
                         {("Store", "supplies", "Supplier", 2), ("Store", "links", "Store", 1)})

    def test_rename_preserves_meaning_and_updates_type_references(self):
        saved = fixture()
        saved["type_meanings"] = {key("Depot"): {"zh": "Depot 接收货物。", "en": "A Depot receives DepotNet goods."},
                                  key("Store"): {"zh": "从Depot接收货物。", "en": "Receives goods from the Depot."},
                                  key("links", "Depot", "Store"): {"zh": "从Depot送货到Store。", "en": "A Depot links to a Store."}}
        graph, meanings, _ = candidate(saved, EditBatch(changes=[
            {"kind": "node", "action": "update", "name": "Depot", "new_name": "Hub"},
            {"kind": "edge", "action": "update", "name": "links", "new_name": "delivers", "source": "Hub", "target": "Store"}]))
        self.assertEqual(meanings[key("Hub")]["en"], "A Hub receives DepotNet goods.")
        self.assertEqual(meanings[key("Store")]["zh"], "从Hub接收货物。")
        self.assertEqual(meanings[key("delivers", "Hub", "Store")]["en"], "A Hub delivers to a Store.")
        self.assertEqual(meanings[key("delivers", "Hub", "Store")]["zh"], "从Hub送货到Store。")
        self.assertIn("Depot", saved["type_meanings"][key("Depot")]["en"])

    def test_followup_can_use_original_names_without_changing_another_signature(self):
        changes = [{"kind": "node", "action": "update", "name": "Depot", "new_name": "Hub"},
                   {"kind": "edge", "action": "update", "name": "links", "new_name": "delivers", "source": "Depot", "target": "Store"},
                   {"kind": "edge", "action": "update", "name": "links", "source": "Depot", "target": "Store",
                    "description": {"zh": "配送货物。", "en": "Delivers goods."}}]
        graph, meanings, resolved = candidate(fixture(), EditBatch(changes=changes))
        self.assertEqual((resolved[1].source, resolved[2].name), ("Hub", "delivers"))
        self.assertEqual(meanings[key("delivers", "Hub", "Store")]["en"], "Delivers goods.")
        self.assertEqual(graph["edges"][1], fixture()["graph"]["edges"][1])
        self.assertEqual(candidate(fixture(), EditBatch(changes=resolved))[:2], (graph, meanings))

    def test_rename_updates_unambiguous_type_references_in_existing_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, uid, did = ConstructionStore(Path(tmp)), "alice", str(uuid.uuid4())
            record = {**fixture(), "dataset_id": did, "generated_questions": {
                "questions": ["Depot与Store之间有几条links边？"],
                "translations": {"en": {"questions": ["How many links connect Depot to Store in DepotNet?"]}}}}
            with store._write_lock(uid, did) as directory:
                _atomic_json(directory / "record.json", record)
            apply_edits(store, uid, did, PendingEdits(base_revision=1, proposal_id=uuid.uuid4(), changes=[
                {"kind": "node", "action": "update", "name": "Depot", "new_name": "Hub"},
                {"kind": "edge", "action": "update", "name": "links", "new_name": "supplies", "source": "Hub", "target": "Store"}]))
            result = store.questions(uid, did)
            self.assertEqual(result["questions"], ["Hub与Store之间有几条links边？"])
            self.assertEqual(result["translations"]["en"]["questions"], ["How many links connect Hub to Store in DepotNet?"])

    def test_revision_retry_questions_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConstructionStore(Path(tmp))
            uid, did = "alice", str(uuid.uuid4())
            record = {**fixture(), "dataset_id": did}
            with store._write_lock(uid, did) as directory:
                _atomic_json(directory / "record.json", record)
            edit = PendingEdits(base_revision=1, proposal_id=uuid.uuid4(), changes=[
                {"kind": "node", "action": "update", "name": "Depot", "new_name": "Hub"}],
                questions=[{"en": "Which hub?", "zh": "哪个枢纽？"}])
            applied = apply_edits(store, uid, did, edit)
            self.assertEqual(applied["revision"], 2)
            self.assertEqual(apply_edits(store, uid, did, edit)["schema"], applied["schema"])
            self.assertEqual(saved_schema(store, uid, did), applied["schema"])
            self.assertEqual(store.questions(uid, did)["questions"], ["哪个枢纽？"])
            self.assertEqual(store.questions(uid, did)["translations"]["en"]["questions"], ["Which hub?"])
            with self.assertRaises(ConstructionError) as stale:
                apply_edits(store, uid, did, edit.model_copy(update={"proposal_id": uuid.uuid4()}))
            self.assertEqual(stale.exception.detail["code"], "PLAYGROUND_SCHEMA_CHANGED")
            with self.assertRaises(ConstructionError):
                saved_schema(store, "other-owner", did)
            saved = json.loads((directory / "record.json").read_text())
            saved["draft_graph"] = saved["graph"]
            with self.assertRaises(ConstructionError):
                candidate(saved, edit)
