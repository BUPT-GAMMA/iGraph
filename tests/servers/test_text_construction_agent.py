"""Semantic TXT construction is explicit, grounded, bounded, and deterministic."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_data_service.construction import validate_graph
from graph_engine.construction.agent import ConstructionFailed, TabularConstructionSubAgent
from graph_engine.ingestion.text_graph_extractor import TextChunker


class SemanticTextLLM:
    model = "semantic-text-test-double"
    # The extraction path must use complete_text even if the tabular planner
    # would otherwise require a construction tool call.
    tool_choice = "required"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_text(self, messages, *, timeout=None, max_tokens=8192):
        self.calls.append({
            "messages": copy.deepcopy(messages),
            "timeout": timeout,
            "max_tokens": max_tokens,
        })
        if not self.responses:
            raise AssertionError("unexpected semantic extraction call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return {"role": "assistant", "content": response,
                "reasoning_content": "must not enter a graph or report"}

    def complete(self, messages, tools, *, timeout=None):
        raise AssertionError("semantic TXT extraction must not use construction tools")

    def audit_metadata(self):
        return {"successful_calls": len(self.calls), "provider_host": "test.invalid",
                "dns_override_used": False, "response_models": [],
                "usage_available_calls": 0,
                "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class SemanticTextConstructionAgentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "people.txt"
        self.path.write_text(
            "Alice works at OpenAI.\n"
            "Bob works at OpenAI.\n",
            encoding="utf-8",
        )

    @staticmethod
    def options(**overrides):
        return {
            "txt_mode": "entities",
            "mode": "llm",
            "experience_enabled": False,
            "text_chunk_tokens": 128,
            "text_overlap_tokens": 0,
            **overrides,
        }

    def build(self, responses, **options):
        llm = SemanticTextLLM(responses)
        progress_path = self.root / "construction-progress.json"
        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
            self.path, self.options(**options), progress_path=progress_path,
            build_id="semantic-text-build",
        )
        return result, llm, json.loads(progress_path.read_text(encoding="utf-8"))

    def test_chunker_preserves_paragraph_boundaries_and_hard_splits_long_sentences(self):
        paragraph_chunks = TextChunker(max_chunk_tokens=8, overlap_tokens=0).split(
            "Alice works.\n\nBob works."
        )
        self.assertEqual("".join(chunk.text for chunk in paragraph_chunks),
                         "Alice works.\n\nBob works.")
        long_chunks = TextChunker(max_chunk_tokens=2, overlap_tokens=1).split(
            "one two three four five"
        )
        self.assertGreater(len(long_chunks), 1)
        self.assertTrue(all(TextChunker.count_tokens(chunk.text) <= 2
                            for chunk in long_chunks))

    def test_extracts_deduplicated_grounded_entities_and_relations_with_provenance(self):
        response = """<triples>
Alice | works at | OpenAI
alice | works for | OpenAI
Bob | works at | OpenAI
</triples>"""
        result, llm, progress = self.build([response])

        self.assertEqual(validate_graph(result.graph), result.graph)
        self.assertTrue(result.graph["directed"])
        self.assertEqual(len(result.graph["nodes"]), 3)
        self.assertEqual(len(result.graph["edges"]), 2)
        self.assertEqual([edge["type"] for edge in result.graph["edges"]],
                         ["works_at", "works_at"])

        nodes = {node["properties"]["data"]["name"]: node for node in result.graph["nodes"]}
        self.assertEqual(set(nodes), {"Alice", "OpenAI", "Bob"})
        self.assertEqual(nodes["Alice"]["properties"]["source_chunk_ids"], [0])
        self.assertEqual(nodes["Alice"]["properties"]["mentions"], 2)
        self.assertEqual(nodes["OpenAI"]["properties"]["mentions"], 3)
        self.assertEqual(nodes["Bob"]["properties"]["mentions"], 1)
        for edge in result.graph["edges"]:
            self.assertIn(edge["source"], {node["id"] for node in result.graph["nodes"]})
            self.assertIn(edge["target"], {node["id"] for node in result.graph["nodes"]})
            self.assertEqual(edge["properties"]["source_chunk_ids"], [0])
            self.assertNotIn("reasoning_content", json.dumps(edge))

        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["max_tokens"], 4096)
        self.assertIn("未受信任的数据", llm.calls[0]["messages"][0]["content"])
        self.assertIn("Alice works at OpenAI.", llm.calls[0]["messages"][1]["content"])
        self.assertEqual(result.report["construction_kind"], "text_entities")
        self.assertEqual(result.report["version"], "text_entity_construction.v1")
        self.assertEqual(result.report["method"],
                         "bounded_grounded_llm_triple_extraction")
        self.assertEqual(result.report["papers"], [])
        self.assertEqual(result.report["query_backend"], "not_applicable")
        self.assertEqual(result.report["final_size"], {"nodes": 3, "edges": 2})
        extraction = result.report["semantic_extraction"]
        self.assertEqual(extraction["chunk_count"], 1)
        self.assertEqual(extraction["llm_calls"], 1)
        self.assertEqual(extraction["raw_triple_count"], 3)
        self.assertEqual(extraction["deduped_triple_count"], 2)
        self.assertNotIn("triples", extraction)
        self.assertEqual((progress["status"], progress["stage"]), ("ready", "ready"))
        self.assertEqual(progress["metrics"]["chunks_completed"], 1)
        self.assertEqual(progress["metrics"]["chunks_total"], 1)
        self.assertEqual(progress["metrics"]["extracted_nodes"], 3)
        self.assertEqual(progress["metrics"]["extracted_edges"], 2)

    def test_same_text_and_triples_produce_identical_graph_ids_and_order(self):
        response = "<triples>\nAlice | works at | OpenAI\nBob | works at | OpenAI\n</triples>"
        first, _, _ = self.build([response])
        second, _, _ = self.build([response])
        self.assertEqual(first.graph, second.graph)
        self.assertEqual(first.report["final_graph_sha256"], second.report["final_graph_sha256"])

    def test_selected_entity_types_become_node_types_without_refinement_calls(self):
        response = "<triples>\nAlice | 人物 | works at | OpenAI | 组织\n</triples>"
        result, llm, _ = self.build([response], entity_types=["人物", "组织"],
                                    text_refine_enabled=False)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual({node["type"] for node in result.graph["nodes"]}, {"人物", "组织"})
        self.assertEqual(result.report["text_refinement"], {"enabled": False})
        self.assertIn("主体类型", llm.calls[0]["messages"][1]["content"])

    def test_typed_homonyms_keep_distinct_nodes_and_relations(self):
        self.path.write_text(
            "Washington, the person, is named after George. "
            "Washington, the city, is named after George.", encoding="utf-8")
        response = (
            "<triples>\nWashington | 人物 | named after | George | 人物\n"
            "Washington | 地点 | named after | George | 人物\n"
            "Washington | 人物 | named after | George | 人物\n</triples>")
        result, _, progress = self.build([response], entity_types=["人物", "地点"])
        self.assertEqual(result.report["final_size"], {"nodes": 3, "edges": 2})
        self.assertEqual({node["type"] for node in result.graph["nodes"]
                          if node["properties"]["data"]["name"] == "Washington"}, {"人物", "地点"})
        self.assertEqual(sorted(edge["properties"]["mentions"] for edge in result.graph["edges"]), [1, 2])
        self.assertEqual(progress["metrics"]["extracted_nodes"], 3)
        self.assertEqual(progress["metrics"]["extracted_edges"], 2)
        self.assertEqual(result.report["semantic_extraction"]["raw_triple_count"], 3)

    def test_refinement_can_add_a_fact_for_a_different_type_of_the_same_name(self):
        self.path.write_text(
            "Washington, the person, is named after George. "
            "Washington, the city, is named after George.", encoding="utf-8")
        extraction = "<triples>Washington | 人物 | named after | George | 人物</triples>"
        query = "<queries>这座城市以谁命名？ | Washington | 地点 | named after | George | 人物</queries>"
        repair = "<triples>Washington | 地点 | named after | George | 人物</triples>"
        result, _, _ = self.build([extraction, query, repair], entity_types=["人物", "地点"],
                                   text_refine_enabled=True, query_count=1)
        self.assertEqual(result.report["final_size"], {"nodes": 3, "edges": 2})
        self.assertEqual(result.report["text_refinement"]["queries_repaired"], 1)
        self.assertEqual(len({edge["id"] for edge in result.graph["edges"]}), 2)

    def test_typed_mixed_empty_declaration_is_retried_without_losing_facts(self):
        valid = "<triples>\nAlice | 人物 | works at | OpenAI | 组织\n</triples>"
        for response in (valid.replace("</triples>", "无实体\n</triples>"),
                         valid.replace("<triples>", "<triples>无实体\n")):
            with self.subTest(response=response):
                result, llm, _ = self.build([response, valid], entity_types=["人物", "组织"])
                self.assertEqual(len(llm.calls), 2)
                self.assertEqual(result.report["final_size"], {"nodes": 2, "edges": 1})

    def test_refinement_preserves_the_extraction_focus_in_queries_and_repairs(self):
        focus = "只抽取雇佣关系，忽略人物之间的爱好和亲属关系。"
        extraction = "<triples>Alice | 人物 | works at | OpenAI | 组织</triples>"
        query = "<queries>Bob 在哪里工作？ | Bob | 人物 | works at | OpenAI | 组织</queries>"
        repair = "<triples>Bob | 人物 | works at | OpenAI | 组织</triples>"
        _, llm, _ = self.build([extraction, query, repair], domain=focus,
                               entity_types=["人物", "组织"], text_refine_enabled=True, query_count=1)
        for call in llm.calls:
            self.assertIn(focus, call["messages"][1]["content"])

    def test_refinement_queries_source_and_repairs_a_missing_fact(self):
        extraction = "<triples>\nAlice | 人物 | works at | OpenAI | 组织\n</triples>"
        queries = "<queries>Bob 在哪里工作？ | Bob | 人物 | works at | OpenAI | 组织</queries>"
        repair = "<triples>\nBob | 人物 | works at | OpenAI | 组织\n</triples>"
        result, llm, progress = self.build([extraction, queries, repair],
                                           entity_types=["人物", "组织"],
                                           text_refine_enabled=True, query_count=1)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(len(result.graph["edges"]), 2)
        self.assertEqual(result.report["text_refinement"]["queries_repaired"], 1)
        self.assertEqual(result.report["query_backend"], "semantic_triple_lookup")
        self.assertEqual(progress["metrics"]["queries_repaired"], 1)
        self.assertNotIn("Bob 在哪里工作", json.dumps(result.report, ensure_ascii=False))

    def test_refinement_does_not_publish_an_unresolved_query(self):
        extraction = "<triples>\nAlice | 人物 | works at | OpenAI | 组织\n</triples>"
        queries = "<queries>Bob 在哪里工作？ | Bob | 人物 | works at | OpenAI | 组织</queries>"
        llm = SemanticTextLLM([extraction, queries, "<triples>无实体</triples>"])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options(entity_types=["人物", "组织"],
                                        text_refine_enabled=True, query_count=1))
        self.assertEqual(caught.exception.code, "text_refine_unresolved")
        self.assertNotIn("final_graph_sha256", caught.exception.report)

    def test_refinement_retries_an_ungrounded_query_before_publishing(self):
        extraction = "<triples>\nAlice | 人物 | works at | OpenAI | 组织\n</triples>"
        invalid = "<queries>谁在 OpenAI 工作？ | Invented | 人物 | works at | OpenAI | 组织</queries>"
        valid = "<queries>Alice 在哪里工作？ | Alice | 人物 | works at | OpenAI | 组织</queries>"
        result, llm, _ = self.build([extraction, invalid, valid],
                                    entity_types=["人物", "组织"], text_refine_enabled=True,
                                    query_count=1)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(result.report["text_refinement"]["queries_discarded"], 1)
        self.assertEqual(result.report["text_refinement"]["queries_passed"], 1)

    def test_refinement_without_type_filter_uses_the_existing_untyped_protocol(self):
        extraction = "<triples>Alice | works at | OpenAI</triples>"
        query = "<queries>Bob 在哪里工作？ | Bob | Entity | works at | OpenAI | Entity</queries>"
        repair = "<triples>Bob | works at | OpenAI</triples>"
        result, llm, _ = self.build([extraction, query, repair],
                                    text_refine_enabled=True, query_count=1)
        self.assertEqual(result.report["final_size"], {"nodes": 3, "edges": 2})
        self.assertEqual({node["type"] for node in result.graph["nodes"]}, {"Entity"})
        self.assertEqual(result.report["text_refinement"]["queries_repaired"], 1)
        self.assertEqual(len(llm.calls), 3)

    def test_refinement_shares_the_extraction_call_budget_and_does_not_publish_partial_work(self):
        extraction = "<triples>Alice | works at | OpenAI</triples>"
        query = "<queries>Bob 在哪里工作？ | Bob | Entity | works at | OpenAI | Entity</queries>"
        llm = SemanticTextLLM([extraction, query])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options(text_refine_enabled=True, query_count=1, text_max_calls=2))
        self.assertEqual(caught.exception.code, "text_extraction_call_limit_exceeded")
        self.assertEqual(len(llm.calls), 2)
        self.assertNotIn("final_graph_sha256", caught.exception.report)

    def test_entity_categories_cannot_contain_protocol_delimiters(self):
        for category in ("人|物", "人｜物"):
            with self.subTest(category=category):
                factory = unittest.mock.Mock()
                with self.assertRaises(ConstructionFailed):
                    TabularConstructionSubAgent(llm_factory=factory).run(
                        self.path, self.options(entity_types=[category]))
                factory.assert_not_called()

    def test_rules_mode_rejects_entity_extraction_before_creating_an_llm(self):
        factory_calls = []

        def factory(config):
            factory_calls.append(config)
            raise AssertionError("LLM factory must not be called in rules mode")

        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=factory).run(
                self.path, self.options(mode="rules"))

        self.assertEqual(caught.exception.code, "text_extraction_requires_llm")
        self.assertEqual(factory_calls, [])
        self.assertEqual(caught.exception.report["construction_kind"], "text_entities")

    def test_missing_construction_credentials_uses_semantic_unavailable_code(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent().run(self.path, self.options())

        self.assertEqual(caught.exception.code, "text_extraction_llm_unavailable")
        self.assertEqual(caught.exception.report["failure_code"],
                         "text_extraction_llm_unavailable")
        self.assertFalse(caught.exception.report["llm_used"])
        self.assertEqual(caught.exception.report["construction_kind"], "text_entities")
        self.assertNotIn("API_KEY", json.dumps(caught.exception.report))

    def test_explicit_no_entities_fails_without_publishing_an_empty_graph(self):
        llm = SemanticTextLLM(["<triples>\nnone\n</triples>"])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options())

        self.assertEqual(caught.exception.code, "text_extraction_empty")
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("final_graph_sha256", caught.exception.report)

    def test_invalid_or_ungrounded_responses_fail_closed_after_bounded_retries(self):
        cases = {
            "four columns": "<triples>\nAlice | works at | OpenAI | invented\n</triples>",
            "hallucinated endpoint": "<triples>\nAlice | works at | Anthropic\n</triples>",
            "missing protocol tags": "Alice | works at | OpenAI",
        }
        for label, response in cases.items():
            with self.subTest(label=label):
                llm = SemanticTextLLM([response, response, response])
                with self.assertRaises(ConstructionFailed) as caught:
                    TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                        self.path, self.options())
                self.assertEqual(caught.exception.code, "text_extraction_response_invalid")
                self.assertEqual(len(llm.calls), 3)
                self.assertNotIn("final_graph_sha256", caught.exception.report)
                self.assertNotIn(response, json.dumps(caught.exception.report, ensure_ascii=False))

    def test_a_later_bad_chunk_rejects_the_whole_graph_instead_of_publishing_partial_work(self):
        self.path.write_text(
            "Alpha relates to Beta. " + "context " * 70 + "\n\n"
            "Gamma relates to Delta. " + "context " * 70,
            encoding="utf-8",
        )
        valid = "<triples>\nAlpha | relates to | Beta\n</triples>"
        invalid = "<triples>\nGamma | relates to | Invented\n</triples>"
        llm = SemanticTextLLM([valid, invalid, invalid, invalid])

        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options(text_max_chunks=4))

        self.assertEqual(caught.exception.code, "text_extraction_response_invalid")
        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(caught.exception.report["semantic_extraction"]["extracted_edges"], 1)
        self.assertNotIn("final_graph_sha256", caught.exception.report)

    def test_character_and_model_call_limits_stop_before_unbounded_processing(self):
        llm = SemanticTextLLM(["invalid response"])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options(text_max_characters=10))
        self.assertEqual(caught.exception.code, "text_extraction_input_too_large")
        self.assertEqual(llm.calls, [])

        llm = SemanticTextLLM(["invalid response"])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(
                self.path, self.options(text_max_calls=1))
        self.assertEqual(caught.exception.code, "text_extraction_call_limit_exceeded")
        self.assertEqual(len(llm.calls), 1)


if __name__ == "__main__":
    unittest.main()
