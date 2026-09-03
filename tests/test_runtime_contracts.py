from __future__ import annotations

import unittest
from pathlib import Path

from agent_runtime import LegacyIGraphRuntime, RuntimeSession, adapt_legacy_agent


class _Logger:
    def __init__(self) -> None:
        self.graph_flags: list[bool] = []
        self.context_messages: list[tuple[dict, dict]] = []
        self.errors: list[str] = []

    def set_graph_flag(self, value: bool) -> None:
        self.graph_flags.append(value)

    def log_context_message(self, message: dict, **kwargs) -> None:
        self.context_messages.append((message, kwargs))

    def log_error(self, error: str) -> None:
        self.errors.append(error)


class _Context:
    def __init__(self) -> None:
        self.messages: list[dict] = []


class _FakeAgent:
    def __init__(self) -> None:
        self.session_id = "session-1"
        self.user_id = "user-1"
        self.provider = "fake"
        self.model = "model-1"
        self.reasoning_mode = "direct"
        self.planning_mode = "auto"
        self.last_active = 42.0
        self.logger = _Logger()
        self.context_manager = _Context()
        self._has_graph = False
        self._graph_node_count = 0
        self._graph_edge_count = 0
        self._graph_unique_edge_count = 0
        self._graph_is_directed = False
        self._graph_semantic_type = "unknown"
        self._graph_storage_type = "mixed"
        self._graph_has_weights = True
        self.events = [
            {"type": "tool_call", "tool": "get_graph_info"},
            {"type": "token", "content": "ok"},
            {"type": "done", "has_graph": True},
        ]
        self.cleaned = False
        self.rolled_back = False

    def set_model(self, model: str) -> None:
        self.model = model

    def set_reasoning_mode(self, mode: str) -> None:
        self.reasoning_mode = mode

    def set_planning_mode(self, mode: str) -> None:
        self.planning_mode = mode

    def chat(self, message: str) -> str:
        return f"echo:{message}"

    async def chat_stream(self, _message: str):
        for event in self.events:
            yield event

    async def resume_feedback_stream(self, **_kwargs):
        yield self.events[-1]

    async def rollback_last_user_turn(self) -> None:
        self.rolled_back = True

    def pending_feedback(self):
        return {"request_id": "feedback-1"}

    def context_usage(self):
        return {"current_tokens": 12}

    def has_graph(self) -> bool:
        return self._has_graph

    def cleanup(self) -> None:
        self.cleaned = True


class RuntimeContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agent = _FakeAgent()
        self.runtime = LegacyIGraphRuntime(self.agent)

    async def test_event_dictionaries_are_forwarded_without_translation(self) -> None:
        events = [event async for event in self.runtime.chat_stream("hello")]
        self.assertEqual(events, self.agent.events)
        for forwarded, original in zip(events, self.agent.events):
            self.assertIs(forwarded, original)

    async def test_feedback_and_rollback_delegate_to_original_agent(self) -> None:
        events = [
            event
            async for event in self.runtime.resume_feedback_stream(
                request_id="feedback-1",
                decision="approve",
            )
        ]
        self.assertIs(events[0], self.agent.events[-1])
        await self.runtime.rollback_last_user_turn()
        self.assertTrue(self.agent.rolled_back)

    def test_graph_state_assignments_match_gateway_baseline(self) -> None:
        self.runtime.apply_graph_load_result(
            {
                "num_nodes": 6,
                "num_edge_entries": 7,
                "num_unique_edges": 7,
                "semantic_type": "directed",
                "storage_type": "local",
            }
        )
        self.assertTrue(self.agent._has_graph)
        self.assertEqual(self.agent._graph_node_count, 6)
        self.assertEqual(self.agent._graph_edge_count, 7)
        self.assertEqual(self.agent._graph_unique_edge_count, 7)
        self.assertTrue(self.agent._graph_is_directed)
        self.assertEqual(self.agent._graph_semantic_type, "directed")
        self.assertEqual(self.agent._graph_storage_type, "local")
        # The original load-dataset path does not overwrite this field.
        self.assertTrue(self.agent._graph_has_weights)
        self.assertEqual(self.agent.logger.graph_flags, [True])

        self.runtime.clear_graph_state()
        self.assertFalse(self.agent._has_graph)
        self.assertEqual(self.agent._graph_node_count, 0)
        self.assertFalse(self.agent._graph_has_weights)
        self.assertEqual(self.agent.logger.graph_flags, [True, False])

    def test_synthetic_history_and_error_logging_are_exact_delegations(self) -> None:
        self.runtime.inject_synthetic_turn("用户消息", "助手消息")
        self.assertEqual(
            self.agent.context_manager.messages,
            [
                {"role": "user", "content": "用户消息"},
                {"role": "assistant", "content": "助手消息"},
            ],
        )
        self.assertEqual(len(self.agent.logger.context_messages), 2)
        self.runtime.log_error("boom")
        self.assertEqual(self.agent.logger.errors, ["boom"])

    def test_adapter_satisfies_runtime_protocol_and_keeps_public_state(self) -> None:
        self.assertIsInstance(self.runtime, RuntimeSession)
        self.assertEqual(self.runtime.session_id, self.agent.session_id)
        self.assertEqual(self.runtime.model, self.agent.model)
        self.assertEqual(self.runtime.last_active, self.agent.last_active)
        self.assertEqual(self.runtime.chat("hello"), "echo:hello")
        self.assertEqual(self.runtime.pending_feedback(), {"request_id": "feedback-1"})
        self.assertEqual(self.runtime.context_usage(), {"current_tokens": 12})

        self.runtime.set_model("model-2")
        self.runtime.set_reasoning_mode("staged")
        self.runtime.set_planning_mode("always")
        self.assertEqual(self.agent.model, "model-2")
        self.assertEqual(self.agent.reasoning_mode, "staged")
        self.assertEqual(self.agent.planning_mode, "always")

        self.runtime.cleanup()
        self.assertTrue(self.agent.cleaned)

    def test_adapt_legacy_agent_is_idempotent(self) -> None:
        self.assertIs(adapt_legacy_agent(self.runtime), self.runtime)
        wrapped = adapt_legacy_agent(self.agent)
        self.assertIsInstance(wrapped, LegacyIGraphRuntime)

    def test_backend_depends_only_on_runtime_public_boundary(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "backend" / "api_gateway" / "app.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "from backend.agent",
            "import backend.agent",
            "._provider_gateway",
            ".context_manager",
            "agent._graph_",
            "agent.logger",
            "LegacyIGraphRuntime",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
