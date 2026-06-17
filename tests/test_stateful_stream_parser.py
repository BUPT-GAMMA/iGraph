"""
StatefulStreamParser 测试。

覆盖：
- 正常路径（think + tool_call / think + answer）
- 容错路径（无标签、只有 think、未闭合标签、多个动作标签）
- 流式状态属性正确性
- 工具调用 JSON 解析
"""

from __future__ import annotations

import json
import unittest
import asyncio

from backend.agent.context.text_protocol_driver import StatefulStreamParser, TextProtocolDriver


class _FakeGateway:
    def __init__(self, chunks):
        self._chunks = chunks

    async def stream_text(self, *_args, **_kwargs):
        for chunk in self._chunks:
            yield chunk


class TestStatefulStreamParser(unittest.TestCase):

    def _feed_all(self, parser: StatefulStreamParser, text: str):
        events = []
        for char in text:
            events.extend(parser.feed(char))
        events.extend(parser.flush())
        return events

    # ── 正常路径 ──────────────────────────────────────────────────

    def test_think_and_answer(self):
        parser = StatefulStreamParser()
        text = "<think>先分析</think><answer>最终回答</answer>"
        events = self._feed_all(parser, text)

        self.assertEqual(parser.action, "answer")
        self.assertEqual(parser.think_text, "先分析")
        self.assertEqual(parser.answer_text, "最终回答")
        self.assertEqual(parser.tool_calls, [])

        types = [e["type"] for e in events]
        self.assertIn("thinking_start", types)
        self.assertIn("thinking", types)
        self.assertIn("thinking_end", types)
        self.assertIn("token", types)

    def test_think_and_tool_call(self):
        tool_json = json.dumps({
            "tool_calls": [{"name": "shortest_path", "arguments": {"source": "A", "target": "B"}}]
        })
        parser = StatefulStreamParser()
        text = f"<think>需要计算路径</think><tool_call>{tool_json}</tool_call>"
        events = self._feed_all(parser, text)

        self.assertEqual(parser.action, "tool_calls")
        self.assertEqual(parser.think_text, "需要计算路径")
        self.assertEqual(len(parser.tool_calls), 1)
        self.assertEqual(parser.tool_calls[0]["name"], "shortest_path")
        self.assertEqual(parser.tool_calls[0]["arguments"], {"source": "A", "target": "B"})

        # tool_call 内容不应输出为 token
        token_events = [e for e in events if e["type"] == "token"]
        self.assertEqual(len(token_events), 0)

    def test_parser_closed_after_answer(self):
        parser = StatefulStreamParser()
        events = []
        for chunk in ["<think>t</think><answer>ans</answer>", "trailing text ignored"]:
            events.extend(parser.feed(chunk))
        self.assertTrue(parser.closed)
        # trailing text after close should be ignored
        trailing_tokens = [e for e in events if e["type"] == "token" and "ignored" in e.get("content", "")]
        self.assertEqual(len(trailing_tokens), 0)

    # ── 容错路径 ──────────────────────────────────────────────────

    def test_fallback_no_tags(self):
        """无任何标签 → 全文作为 answer"""
        parser = StatefulStreamParser()
        events = []
        events.extend(parser.feed("这是一段没有任何标签的回答"))
        events.extend(parser.flush())

        self.assertEqual(parser.action, "answer")
        self.assertIn("这是一段没有任何标签的回答", parser.answer_text)

    def test_fallback_only_think(self):
        """只有 <think> 无后续 → think 内容作为 answer"""
        parser = StatefulStreamParser()
        events = []
        events.extend(parser.feed("<think>"))
        events.extend(parser.feed("我的分析是"))
        events.extend(parser.feed("</think>"))
        events.extend(parser.flush())

        self.assertEqual(parser.action, "answer")
        # flush 后 think_text 应被保留，action 应为 answer
        self.assertIsNotNone(parser.action)
        self.assertEqual(parser.answer_text, "我的分析是")

    def test_implicit_answer_after_think_streams_without_tags(self):
        """<think> 后直接正文 → 正文作为 answer 流式输出，不泄漏 think 标签。"""
        parser = StatefulStreamParser()
        events = []
        events.extend(parser.feed("<think>先分析</think>"))
        streamed = parser.feed("最终")
        self.assertEqual(
            "".join(e["content"] for e in streamed if e["type"] == "token"),
            "最终",
        )
        events.extend(streamed)
        events.extend(parser.feed("回答"))
        events.extend(parser.flush())

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        thinking_text = "".join(e["content"] for e in events if e["type"] == "thinking")
        self.assertEqual(parser.action, "answer")
        self.assertEqual(parser.think_text, "先分析")
        self.assertEqual(parser.answer_text, "最终回答")
        self.assertEqual(token_text, "最终回答")
        self.assertEqual(thinking_text, "先分析")
        self.assertNotIn("<think>", token_text)

    def test_whitespace_between_think_and_answer_tag_is_not_implicit_answer(self):
        """</think> 和 <answer> 之间的空白不应抢先开启隐式 answer。"""
        parser = StatefulStreamParser()
        text = "<think>先分析</think>\n<answer>最终回答</answer>"
        events = self._feed_all(parser, text)

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        self.assertEqual(parser.action, "answer")
        self.assertEqual(parser.answer_text, "最终回答")
        self.assertEqual(token_text, "最终回答")

    def test_implicit_answer_then_tool_call_is_invalid(self):
        parser = StatefulStreamParser()
        events = []
        events.extend(parser.feed("<think>先分析</think>"))
        events.extend(parser.feed("直接回答"))
        events.extend(parser.feed("<tool_call>{}</tool_call>"))
        events.extend(parser.flush())

        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        self.assertEqual(token_text, "直接回答")
        self.assertIsNotNone(parser.parse_error)

    def test_multiple_action_tags_keeps_first(self):
        """多个动作标签 → 只保留第一个"""
        parser = StatefulStreamParser()
        text = "<think>t</think><answer>first answer</answer><answer>second answer</answer>"
        self._feed_all(parser, text)

        self.assertEqual(parser.action, "answer")
        self.assertIn("first answer", parser.answer_text)
        self.assertNotIn("second answer", parser.answer_text)

    def test_streaming_character_by_character(self):
        """字符级流式输入应正确处理部分 tag"""
        parser = StatefulStreamParser()
        text = "<think>分析中</think><answer>结论</answer>"
        events = []
        for ch in text:
            events.extend(parser.feed(ch))
        events.extend(parser.flush())

        self.assertEqual(parser.action, "answer")
        self.assertEqual(parser.think_text, "分析中")
        self.assertEqual(parser.answer_text, "结论")

    # ── tool_calls JSON 解析 ─────────────────────────────────────

    def test_tool_calls_json_parsed(self):
        calls = [
            {"name": "get_graph_info", "arguments": {}},
            {"name": "shortest_path", "arguments": {"source": "A", "target": "Z"}, "call_id": "c1"},
        ]
        tool_json = json.dumps({"tool_calls": calls})
        parser = StatefulStreamParser()
        text = f"<think></think><tool_call>{tool_json}</tool_call>"
        self._feed_all(parser, text)

        parsed = parser.tool_calls
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "get_graph_info")
        self.assertEqual(parsed[1]["name"], "shortest_path")
        self.assertEqual(parsed[1]["arguments"], {"source": "A", "target": "Z"})

    def test_tool_calls_invalid_json_returns_empty(self):
        parser = StatefulStreamParser()
        text = "<think></think><tool_call>not_valid_json</tool_call>"
        self._feed_all(parser, text)

        self.assertEqual(parser.action, "tool_calls")
        self.assertEqual(parser.tool_calls, [])

    def test_raw_text_accumulated(self):
        parser = StatefulStreamParser()
        chunks = ["<think>", "计划", "</think>", "<answer>", "回答", "</answer>"]
        for chunk in chunks:
            parser.feed(chunk)
        parser.flush()

        self.assertIn("计划", parser.raw_text)
        self.assertIn("回答", parser.raw_text)


class TestTextProtocolDriver(unittest.TestCase):
    def _collect_events(self, chunks):
        async def run():
            driver = TextProtocolDriver(_FakeGateway(chunks))
            events = []
            async for event in driver.stream_turn([], [], step=1):
                events.append(event)
            return events

        return asyncio.run(run())

    def test_invalid_tool_call_json_sets_parse_error(self):
        events = self._collect_events(["<think></think><tool_call>not-json</tool_call>"])
        turn_end = events[-1]
        self.assertEqual(turn_end["type"], "turn_end")
        self.assertEqual(turn_end["action"], "tool_calls")
        self.assertTrue(turn_end["parse_error"])

    def test_implicit_answer_then_tool_call_sets_parse_error(self):
        events = self._collect_events(["<think>先分析</think>直接回答<tool_call>{}</tool_call>"])
        turn_end = events[-1]
        token_text = "".join(e["content"] for e in events if e["type"] == "token")
        self.assertEqual(token_text, "直接回答")
        self.assertTrue(turn_end["parse_error"])


if __name__ == "__main__":
    unittest.main()
