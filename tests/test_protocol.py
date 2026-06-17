from __future__ import annotations

import unittest

from backend.agent.protocol import (
    ProtocolStreamParser,
    build_tool_observation_batch_message,
    normalize_model_output,
    parse_model_output,
    tool_message_to_user_content,
)


class ProtocolNormalizeTests(unittest.TestCase):
    def test_parse_think_and_answer_tag(self) -> None:
        parsed = parse_model_output("<think>先分析</think><answer>最终答复</answer>")
        self.assertEqual(parsed["action"], "answer")
        self.assertEqual(parsed["think"], "先分析")
        self.assertEqual(parsed["final_answer"], "最终答复")
        self.assertEqual(parsed["tool_calls"], [])

    def test_parse_tool_calls_ignores_trailing_text(self) -> None:
        raw = (
            '<think>规划</think><tool_call>{"tool_calls":[{"name":"x","arguments":{}}]}</tool_call>'
            "尾随文本应被忽略"
        )
        parsed = parse_model_output(raw)
        self.assertEqual(parsed["action"], "tool_calls")
        self.assertEqual(parsed["think"], "规划")
        self.assertEqual(len(parsed["tool_calls"]), 1)
        self.assertEqual(parsed["final_answer"], "")

    def test_parse_legacy_tool_calls_tag_is_rejected(self) -> None:
        raw = '<tool_calls>{"tool_calls":[{"name":"x","arguments":{}}]}</tool_calls>'
        parsed = parse_model_output(raw)
        self.assertIsNone(parsed["action"])
        self.assertEqual(parsed["tool_calls"], [])
        self.assertIn("<tool_calls>", parsed["parse_error"])

    def test_normalize_missing_tag_wraps_answer(self) -> None:
        normalized = normalize_model_output("这是一个未带标签的回答", reasoning_mode="direct")
        self.assertEqual(
            normalized["normalized_text"],
            "<think></think><answer>这是一个未带标签的回答</answer>",
        )
        self.assertFalse(normalized["had_action_tag"])

    def test_normalize_keeps_only_first_action_tag(self) -> None:
        raw = (
            "<think>先规划</think>"
            "<answer>应保留</answer>"
            '<tool_call>{"tool_calls":[{"name":"x","arguments":{}}]}</tool_call>'
        )
        normalized = normalize_model_output(raw, reasoning_mode="staged")
        self.assertEqual(
            normalized["normalized_text"],
            "<think>先规划</think><answer>应保留</answer>",
        )

    def test_direct_mode_keeps_dual_tag_shape(self) -> None:
        raw = "<think>仅供推理</think><answer>结论</answer>"
        normalized = normalize_model_output(raw, reasoning_mode="direct")
        self.assertEqual(normalized["normalized_text"], raw)

    def test_stream_parser_stops_after_closing_tag(self) -> None:
        parser = ProtocolStreamParser()
        raw = "<think>先想</think><answer>abc</answer>should_be_dropped"
        events = parser.feed(raw) + parser.flush()
        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        think_text = "".join(e["content"] for e in events if e["type"] == "thinking")
        self.assertEqual(think_text, "先想")
        self.assertEqual(token_text, "abc")
        self.assertTrue(parser.closed)
        self.assertEqual(parser.feed("more_text"), [])

    def test_batch_tool_observation_message_to_user_content(self) -> None:
        msg = build_tool_observation_batch_message(
            [
                {
                    "call_id": "c1",
                    "server": "graph",
                    "name": "query_nodes",
                    "arguments": {"limit": 3},
                    "result": '{"nodes":[1,2,3]}',
                },
                {
                    "call_id": "c2",
                    "server": "graph",
                    "name": "query_edges",
                    "arguments": {"limit": 2},
                    "result": '{"edges":[[1,2],[2,3]]}',
                },
            ],
            reasoning_mode="staged",
        )
        content = tool_message_to_user_content(msg)
        self.assertIn("以下是本轮工具调用结果", content)
        self.assertIn("query_nodes 工具调用结果为：", content)
        self.assertIn("query_edges 工具调用结果为：", content)
        self.assertNotIn("<tool_observation_batch>", content)
        self.assertEqual(msg["role"], "tool")
        self.assertEqual(msg["name"], "tool_batch")


if __name__ == "__main__":
    unittest.main()
