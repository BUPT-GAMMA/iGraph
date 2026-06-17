from __future__ import annotations

import unittest

from backend.agent.model_registry import normalize_model_name
from backend.agent.provider_gateway import ProviderGateway


class ModelRegistryTests(unittest.TestCase):
    def test_accepts_canonical_hyphen_names(self) -> None:
        self.assertEqual(normalize_model_name("claude-sonnet-4-6"), "claude-sonnet-4-6")
        self.assertEqual(normalize_model_name("gpt-5-2"), "gpt-5-2")

    def test_rejects_unsupported_model(self) -> None:
        with self.assertRaises(ValueError):
            normalize_model_name("unknown-model")


class ProviderGatewayMessageTests(unittest.TestCase):
    def _build_gateway(self, model: str) -> ProviderGateway:
        gw = object.__new__(ProviderGateway)
        gw.model = model
        return gw

    def test_claude_keeps_assistant_as_last_message(self) -> None:
        gw = self._build_gateway("claude-sonnet-4-6")
        msgs = gw._to_provider_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": "<think>计划</think><answer>结论</answer>"},
            ]
        )
        self.assertEqual(msgs[-1]["role"], "assistant")

    def test_non_claude_keeps_assistant_as_last_message(self) -> None:
        gw = self._build_gateway("gpt-5-2")
        msgs = gw._to_provider_messages(
            [
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": "<think>计划</think><answer>结论</answer>"},
            ]
        )
        self.assertEqual(msgs[-1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
