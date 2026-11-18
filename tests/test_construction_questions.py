import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from graph_data_service.construction import ConstructionError, ConstructionStore, graph_schema
from graph_data_service.construction_questions import (
    QuestionGenerationError,
    generate_questions,
    graph_question_context,
    parse_questions,
)
from graph_data_service.construction_schema import schema_foreign_key


def stations():
    nodes = [{"id": key, "type": "车站", "properties": {"data": {"name": label, "api_key": "private"}}}
             for key, label in (("a", "云桥站"), ("b", "南湾站"), ("c", "北林站"), ("d", "东台站"))]
    edges = [{"id": "e1", "type": "快线", "source": "a", "target": "b", "properties": {"耗时": "5"}},
             {"id": "e2", "type": "快线", "source": "a", "target": "c", "properties": {"耗时": "12"}}]
    return {"directed": True, "nodes": nodes, "edges": edges}


def model_output():
    return [dict(title_zh=f"线路 {n}", question_zh=f"如何分析第 {n} 条线路？",
                 title_en=f"Route {n}", question_en=f"How would you analyze route {n}?")
            for n in range(4)]


class ConstructionQuestionsTests(unittest.TestCase):
    def test_context_contains_real_fields_and_connections_with_a_byte_budget(self):
        graph = stations()
        context = graph_question_context(graph)
        sample = context["sample_connections"][0]
        assert (sample["from"]["fields"]["name"], sample["to"]["fields"]["name"]) == ("云桥站", "南湾站")
        assert context["relationship_types"][0]["count"] == 2
        assert sample["fields"] == {"耗时": "5"}
        self.assertNotIn("private", json.dumps(context))
        context = graph_question_context(graph, max_bytes=350)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()), 350)

    def test_requires_four_distinct_valid_questions_in_each_language(self):
        response = parse_questions(json.dumps(model_output()))
        self.assertEqual(len(response["questions"]), 4)
        self.assertEqual(len(response["translations"]["en"]["questions"]), 4)
        for broken in ("wrong", json.dumps(model_output()[:3]), json.dumps([model_output()[0]] * 4)):
            with self.assertRaisesRegex(QuestionGenerationError, "QUESTION_OUTPUT_INVALID"):
                parse_questions(broken)

    def test_save_and_regeneration_are_revision_and_owner_scoped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "transport.csv"
            source.write_text("id,name\na,Center\n", encoding="utf-8")
            store = ConstructionStore(root)
            store.register_algorithm("csv", lambda *_: copy.deepcopy(stations()))
            draft = store.build("owner", "rail", source, source.name)
            with self.assertRaises(ConstructionError):
                store.questions("owner", "rail")
            model = {"config_id": "m_rail", "revision": 8}
            saved = store.optimize("owner", "rail", draft["revision"], [], commit=True, question_model=model)
            generation = saved["question_generation"]
            self.assertEqual(store.questions("owner", "rail")["status"], "generating")
            self.assertIsNone(store.start_questions("owner", "rail", saved["revision"], model)[1])
            with self.assertRaises(ConstructionError):
                store.start_questions("owner", "rail", saved["revision"], {**model, "revision": 9})
            questions = {**parse_questions(json.dumps(model_output())), "model_name": "Rail Analyst"}
            store.finish_questions("owner", "rail", saved["revision"], generation["id"], questions, None)
            ready = ConstructionStore(root).questions("owner", "rail")
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["questions"], questions["questions"])
            self.assertEqual(ready["model_config_id"], model["config_id"])
            with self.assertRaises(ConstructionError):
                store.questions("other", "rail")

            _, retry, _ = store.start_questions("owner", "rail", saved["revision"], model)
            store.finish_questions("owner", "rail", saved["revision"], retry["id"], None, "QUESTION_TIMEOUT")
            failed = store.questions("owner", "rail")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["questions"], ready["questions"])

            operation = {"op": "rename_type", "target": "edge", "old_type": "快线", "new_type": "城际"}
            newer = store.optimize("owner", "rail", saved["revision"], [operation], commit=True)
            store.finish_questions("owner", "rail", saved["revision"], retry["id"], questions, None)
            found = store.questions("owner", "rail")
            self.assertEqual((found["revision"], found["status"], found["questions"]), (newer["revision"], "not_generated", []))

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


class ModelCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_selected_provider_configuration_and_bounded_graph_input(self):
        selection = {"config_id": "model-b", "revision": 4}
        connection = {"model_id": "upstream-b", "display_name": "Configured model", "base_url": "http://model.test",
                      "api_key": "test-key", "context_window": 25_000, "max_output_tokens": 2500}
        async def run_sync(fn, *args):
            return fn(*args)
        mod = "graph_data_service.construction_questions"
        with patch(f"{mod}.resolve_question_model", new=AsyncMock(return_value=connection)) as resolve, \
             patch(f"{mod}.ProviderGateway") as gateway:
            client = gateway.return_value
            client.context_window, client.output_limit = 25_000, 2500
            client.invoke_text = AsyncMock(return_value=json.dumps(model_output()))
            client.aclose = AsyncMock()
            result = await generate_questions(stations(), "owner", selection, None, run_sync)
            resolve.assert_awaited_once_with("owner", selection)
            self.assertEqual(gateway.call_args.kwargs["resolved_config"], connection)
            messages = client.invoke_text.call_args.args[0]
            self.assertIn("云桥站", messages[1]["content"])
            self.assertNotIn("test-key", messages[1]["content"])
            client.aclose.assert_awaited_once()
            self.assertEqual(result["model_name"], "Configured model")
