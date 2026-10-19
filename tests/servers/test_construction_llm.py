"""Construction client transport and bounded auto-tool recovery; no network."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from graph_engine.construction.agent import ConstructionFailed, TabularConstructionSubAgent
from graph_engine.construction.llm import ConstructionLLMError, ToolCallingLLM
from graph_engine.construction.tools import tool_specs


class ConstructionLLMClientTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "GRAPH_CONSTRUCTION_API_KEY": "synthetic-test-key",
            "GRAPH_CONSTRUCTION_BASE_URL": "https://synthetic.invalid/v1",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def complete_with(self, payload, *, client=None, timeout=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        with patch("graph_engine.construction.llm.urllib.request.urlopen", return_value=io.BytesIO(body)) as transport:
            result = (client or ToolCallingLLM()).complete(
                [{"role": "user", "content": "Construct the graph"}], tool_specs(), timeout=timeout)
        return result, transport

    def test_auto_request_keeps_all_schemas_budget_and_filters_private_response_fields(self):
        call = {"id": "synthetic-call", "type": "function", "function": {"name": "inspect_schema", "arguments": "{}"}}
        client = ToolCallingLLM(timeout=20)
        result, transport = self.complete_with({"model": "provider-model-receipt", "usage": {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            "prompt_tokens_details": {"private": "excluded"}}, "choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [call],
            "reasoning_content": "private reasoning must be excluded", "internal_metadata": "excluded",
        }}]}, client=client, timeout=5)
        self.assertEqual(result, {"role": "assistant", "content": None, "tool_calls": [call]})
        request = transport.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["max_tokens"], 14000)
        self.assertEqual(body["temperature"], 0)
        self.assertFalse(body["stream"])
        self.assertEqual(body["tools"], tool_specs())
        self.assertTrue(any(spec["function"]["name"] == "transform_batch" for spec in body["tools"]))
        self.assertEqual(transport.call_args.kwargs["timeout"], 5)
        self.assertEqual(request.full_url, "https://synthetic.invalid/v1/chat/completions")
        transport.assert_called_once()
        self.assertEqual(client.audit_metadata(), {
            "successful_calls": 1, "provider_host": "synthetic.invalid", "dns_override_used": False,
            "response_models": ["provider-model-receipt"],
            "usage_available_calls": 1,
            "token_usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    def test_deepseek_structured_text_disables_thinking_and_rejects_truncation(self):
        client = ToolCallingLLM(model="deepseek-v4-pro")
        _, transport = self.complete_with({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}, client=client)
        self.assertEqual(json.loads(transport.call_args.args[0].data)["thinking"], {"type": "disabled"})
        with self.assertRaisesRegex(ConstructionLLMError, "output_truncated"):
            self.complete_with({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, client=client)

    def test_explicit_dns_override_changes_only_resolution_and_keeps_url_host(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        body = json.dumps(payload).encode("utf-8")
        looked_up = []

        def resolver(host, port, *args, **kwargs):
            looked_up.append((host, port))
            return [(2, 1, 6, "", (host, port))]

        def transport(request, timeout=None):
            __import__("socket").getaddrinfo("synthetic.invalid", 443)
            self.assertEqual(request.full_url, "https://synthetic.invalid/v1/chat/completions")
            return io.BytesIO(body)

        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_RESOLVED_IP": "203.0.113.7"}), \
                patch("graph_engine.construction.llm.socket.getaddrinfo", side_effect=resolver), \
                patch("graph_engine.construction.llm.urllib.request.urlopen", side_effect=transport):
            client = ToolCallingLLM()
            client.complete([], [])
        self.assertEqual(looked_up, [("203.0.113.7", 443)])
        self.assertTrue(client.audit_metadata()["dns_override_used"])

        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_RESOLVED_IP": "not-an-ip"}):
            with self.assertRaisesRegex(ConstructionLLMError, "invalid_resolved_ip"):
                ToolCallingLLM()

    def test_explicit_or_environment_choice_is_typed_and_serialized(self):
        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_TOOL_CHOICE": "required"}):
            client = ToolCallingLLM(timeout=4)
            self.assertEqual(client.tool_choice, "required")
            _, transport = self.complete_with({"choices": [{"message": {"role": "assistant", "content": "text"}}]}, client=client, timeout=20)
            self.assertEqual(json.loads(transport.call_args.args[0].data)["tool_choice"], "required")
            self.assertEqual(transport.call_args.kwargs["timeout"], 4)
            self.assertEqual(ToolCallingLLM(tool_choice="auto").tool_choice, "auto")
        for choice in ("", "none", True, {"type": "function"}, 1):
            with self.subTest(choice=choice), self.assertRaisesRegex(ConstructionLLMError, "invalid_tool_choice"):
                ToolCallingLLM(tool_choice=choice)
        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_TOOL_CHOICE": "invalid"}):
            with self.assertRaisesRegex(ConstructionLLMError, "invalid_tool_choice"):
                ToolCallingLLM()

    def test_text_response_is_preserved_without_inventing_a_tool_call(self):
        result, _ = self.complete_with({"choices": [{"message": {"role": "assistant", "content": "I will inspect the graph."}}]})
        self.assertEqual(result, {"role": "assistant", "content": "I will inspect the graph."})
        self.assertNotIn("tool_calls", result)

    def test_semantic_text_request_omits_tools_and_required_tool_choice(self):
        payload = {"model": "provider-model-receipt", "choices": [{"message": {
            "role": "assistant", "content": "<triples>\nAlice | works at | OpenAI\n</triples>",
            "reasoning_content": "private reasoning must be excluded",
            "internal_metadata": "excluded",
        }}]}
        body = json.dumps(payload).encode("utf-8")
        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_TOOL_CHOICE": "required"}), \
                patch("graph_engine.construction.llm.urllib.request.urlopen", return_value=io.BytesIO(body)) as transport:
            client = ToolCallingLLM(timeout=20)
            result = client.complete_text(
                [{"role": "user", "content": "Extract explicit relations"}], timeout=5, max_tokens=4096)

        self.assertEqual(result, {
            "role": "assistant", "content": "<triples>\nAlice | works at | OpenAI\n</triples>",
        })
        request_body = json.loads(transport.call_args.args[0].data)
        self.assertNotIn("tools", request_body)
        self.assertNotIn("tool_choice", request_body)
        self.assertEqual(request_body["max_tokens"], 4096)
        self.assertEqual(request_body["temperature"], 0)
        self.assertFalse(request_body["stream"])
        self.assertEqual(transport.call_args.kwargs["timeout"], 5)
        self.assertEqual(client.audit_metadata()["successful_calls"], 1)

    def test_semantic_text_request_rejects_invalid_max_token_bounds_without_network(self):
        client = ToolCallingLLM()
        for value in (0, 14001, 1.5, True):
            with self.subTest(value=value), \
                    patch("graph_engine.construction.llm.urllib.request.urlopen") as transport, \
                    self.assertRaisesRegex(ConstructionLLMError, "invalid_max_tokens"):
                client.complete_text([], max_tokens=value)
            transport.assert_not_called()

    def test_http_errors_remain_redacted_and_are_not_automatically_retried(self):
        error = urllib.error.HTTPError("https://private.invalid", 400, "sensitive provider response", {}, io.BytesIO(b"secret body"))
        with patch("graph_engine.construction.llm.urllib.request.urlopen", side_effect=error) as transport:
            with self.assertRaises(ConstructionLLMError) as caught:
                ToolCallingLLM().complete([], tool_specs())
        self.assertEqual(str(caught.exception), "construction_llm_http_400")
        transport.assert_called_once()

    def test_response_size_shape_and_network_failures_are_bounded(self):
        for payload, code in ((b"x" * (4 * 1024 * 1024 + 1), "response_too_large"),
                              (b"not json", "invalid_response"), ({"choices": []}, "invalid_response"),
                              ({"choices": [{"message": []}]}, "invalid_response")):
            with self.subTest(code=code), self.assertRaisesRegex(ConstructionLLMError, code):
                self.complete_with(payload)
        for error in (TimeoutError("private address"), urllib.error.URLError("private address")):
            with self.subTest(error=type(error).__name__):
                with patch("graph_engine.construction.llm.urllib.request.urlopen", side_effect=error) as transport:
                    with self.assertRaises(ConstructionLLMError) as caught:
                        ToolCallingLLM().complete([], [])
                self.assertEqual(str(caught.exception), "construction_llm_connection_or_timeout")
                transport.assert_called_once()


class TextOnlyPlanner:
    model = "synthetic-text-only"
    tool_choice = "auto"

    def __init__(self):
        self.messages = []

    def complete(self, messages, tools, *, timeout=None):
        self.messages.append(copy.deepcopy(messages))
        return {"role": "assistant", "content": "I will use a construction tool next."}


class AutoToolRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "rows.csv"
        self.source.write_text("id,name\n1,Alice\n2,Bob\n", encoding="utf-8")

    def test_text_only_workload_responses_consume_bounded_attempts_and_cannot_freeze(self):
        planner = TextOnlyPlanner()
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: planner).run(self.source, {
                "primary_key": ["id"], "query_count": 1, "workload_attempts": 2, "min_query_seconds": 0,
            })
        self.assertEqual(caught.exception.code, "workload_generation_failed")
        self.assertEqual(caught.exception.report["llm_tool_choice"], "auto")
        self.assertEqual(len(planner.messages), 2)
        self.assertIn("plain text cannot freeze", planner.messages[1][-1]["content"])
        self.assertEqual(len(caught.exception.report["workload_validation_errors"]), 2)

    def test_text_only_evolution_cannot_count_as_an_evaluated_candidate(self):
        planner = TextOnlyPlanner()
        query = {"id": "count", "question": "How many rows?", "root": {"table": "rows", "alias": "t0"},
                 "aggregations": [{"function": "count", "column": "*", "as": "n"}]}
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: planner).run(self.source, {
                "primary_key": ["id"], "queries": [query], "query_count": 1, "max_iterations": 2, "min_query_seconds": 0,
            })
        self.assertEqual(caught.exception.code, "no_evolution_candidate_evaluated")
        self.assertEqual(len(planner.messages), 2)
        self.assertIn("Use construction tools", planner.messages[1][-1]["content"])
        self.assertEqual(caught.exception.report["history"], [])
        self.assertEqual(caught.exception.report["iterations"], 2)


if __name__ == "__main__":
    unittest.main()
