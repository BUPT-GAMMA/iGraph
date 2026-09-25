from __future__ import annotations

import asyncio
import io
import unittest

from scripts import igraph_agent_adapter as adapter


class _FakeRuntime:
    async def chat_stream(self, _prompt: str):
        for event in (
            {"type": "thinking", "phase": "thinking", "content": "inspect"},
            {
                "type": "tool_call",
                "tool": "tool_select",
                "args": {"query": "density"},
                "call_id": "internal-1",
            },
            {"type": "tool_result", "tool": "tool_select", "call_id": "internal-1"},
            {
                "type": "tool_call",
                "tool": "get_density",
                "args": {"directed": False},
                "call_id": "call-1",
            },
            {"type": "tool_result", "tool": "get_density", "call_id": "call-1"},
            {"type": "token", "content": "0."},
            {"type": "token", "content": "25"},
            {"type": "done", "has_graph": True},
        ):
            yield event


class IGraphAgentAdapterTests(unittest.TestCase):
    def test_strict_envelope_rejects_reference_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema"):
            adapter._strict_envelope(
                io.StringIO('{"schema":"wrong","question":"hidden"}')
            )

    def test_prompt_exposes_only_public_task_fields(self) -> None:
        prompt = adapter._prompt(
            {
                "question": "What is the density?",
                "category": "insight",
                "dataset": "cora",
                "task_type": "graph",
                "context": {"graph_idx": 2},
                "dataset_path": "/secret/path.pt",
                "ground_truth": "must-not-leak",
                "required_tools": ["must-not-leak"],
            }
        )

        self.assertIn("What is the density?", prompt)
        self.assertIn('"graph_idx":2', prompt)
        self.assertNotIn("/secret/path.pt", prompt)
        self.assertNotIn("must-not-leak", prompt)

    def test_event_collection_keeps_real_call_and_final_answer(self) -> None:
        events, answer, reasoning, error, calls = asyncio.run(
            adapter._collect_events(_FakeRuntime(), "question")
        )

        self.assertEqual(answer, "0.25")
        self.assertIsNone(error)
        self.assertEqual(calls[0]["name"], "get_density")
        self.assertEqual(calls[0]["arguments"], {"directed": False})
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(event.get("tool") == "tool_select" for event in events))
        self.assertEqual(reasoning["blocks"][0]["content"], "inspect")
        self.assertEqual(events[-1]["type"], "done")

    def test_tool_result_is_never_invented(self) -> None:
        calls = [{"name": "get_density", "arguments": {}}]
        merged = adapter._merge_tool_results(calls, [])
        self.assertFalse(merged[0]["success"])
        self.assertIsNone(merged[0]["result"])

        merged = adapter._merge_tool_results(
            [{"name": "get_density", "arguments": {}}],
            [("get_density", '{"success":false,"error":"boom"}')],
        )
        self.assertFalse(merged[0]["success"])

    def test_secret_redaction_is_recursive(self) -> None:
        redacted = adapter._redact(
            {"error": "key-123 at https://provider.invalid/v1", "rows": ["key-123"]},
            ("key-123", "https://provider.invalid/v1"),
        )
        self.assertEqual(redacted["error"], "[REDACTED] at [REDACTED]")
        self.assertEqual(redacted["rows"], ["[REDACTED]"])


if __name__ == "__main__":
    unittest.main()
