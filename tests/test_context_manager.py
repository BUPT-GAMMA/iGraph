from __future__ import annotations

import unittest
import tempfile

from backend.agent.context import (
    ContextManager,
    build_mode_system_prompt,
    build_role_system_prompt,
    build_user_turn_content,
)
from backend.agent.context.token_counter import context_usage, count_message_tokens
from backend.agent.skills.skill_loader import build_skills_system_prompt
from backend.agent.skills.tools import handle_skill_tool_call
from backend.logging.session_store import SessionStore


class ContextManagerTests(unittest.TestCase):
    def test_init_creates_system_message(self) -> None:
        cm = ContextManager(session_id="s1", reasoning_mode="staged")
        self.assertEqual(len(cm.messages), 1)
        self.assertEqual(cm.messages[0]["role"], "system")

    def test_set_reasoning_mode_updates_system_message(self) -> None:
        cm = ContextManager(session_id="s1", reasoning_mode="direct")
        cm.set_reasoning_mode("staged")
        self.assertEqual(cm.reasoning_mode, "staged")
        prompt = cm.messages[0]["content"]
        # staged 模式的 role prompt 不应包含 mode rule（mode rule 是动态注入的）
        self.assertNotIn("## 推理模式回答规则", prompt)

    def test_user_turn_content_is_clean(self) -> None:
        staged = build_user_turn_content("请分析这个图", "staged")
        direct = build_user_turn_content("直接回答", "direct")
        self.assertEqual(staged, "请分析这个图")
        self.assertEqual(direct, "直接回答")
        self.assertNotIn("[回答模式]", staged)
        self.assertNotIn("[回答模式]", direct)

    def test_mode_system_prompt_content(self) -> None:
        staged_rule = build_mode_system_prompt("staged")
        direct_rule = build_mode_system_prompt("direct")
        self.assertIn("## 推理模式回答规则", staged_rule)
        self.assertIn("## 非推理模式回答规则", direct_rule)
        self.assertIn("核心工具", staged_rule)
        self.assertIn("get_graph_info", staged_rule)
        self.assertIn("不在当前可用工具列表中的非核心图分析业务工具", direct_rule)

    def test_role_system_prompt_excludes_mode_rule_staged(self) -> None:
        prompt = build_role_system_prompt("staged")
        self.assertNotIn("## 推理模式回答规则", prompt)
        self.assertNotIn("## 非推理模式回答规则", prompt)

    def test_role_system_prompt_excludes_mode_rule_direct(self) -> None:
        prompt = build_role_system_prompt("direct")
        self.assertNotIn("## 非推理模式回答规则", prompt)
        self.assertNotIn("## 推理模式回答规则", prompt)

    def test_mode_system_prompt_for_native(self) -> None:
        staged_rule = build_mode_system_prompt("staged", protocol="native")
        direct_rule = build_mode_system_prompt("direct", protocol="native")
        self.assertIn("## 推理模式回答规则", staged_rule)
        self.assertIn("Function Calling", staged_rule)
        self.assertIn("## 非推理模式回答规则", direct_rule)
        self.assertIn("不输出 `<think>` 标签", direct_rule)

    def test_role_system_prompt_contains_exhaustive_resolution_rule(self) -> None:
        prompt = build_role_system_prompt("staged")
        self.assertIn("不要过早给出", prompt)

    def test_role_system_prompt_encourages_graph_ml_inference_path(self) -> None:
        prompt = build_role_system_prompt("staged")
        self.assertIn("可以进一步考虑使用图机器学习相关工具进行建模与推断", prompt)

    def test_role_system_prompt_contains_graph_analysis_reporting_rules(self) -> None:
        prompt = build_role_system_prompt("direct", protocol="text")
        self.assertIn("## 图分析报告原则", prompt)
        self.assertIn("整体理解、统计总结、结构解释、模式发现、诊断判断、对比分析", prompt)
        self.assertIn("图表化呈现", prompt)
        self.assertIn("轻量文本答复", prompt)
        self.assertIn("单个节点、单条边、单条路径、单个指标值", prompt)
        self.assertIn("create_analysis_report", prompt)
        self.assertIn("report_intent", prompt)
        self.assertIn("analysis_goal", prompt)
        self.assertIn("source_results", prompt)
        self.assertIn("get_graph_info", prompt)
        self.assertIn("visualize_subgraph", prompt)
        self.assertIn("回退为普通文本总结", prompt)
        self.assertIn("1-3 句简短结论", prompt)
        self.assertIn("不在聊天区重复完整表格、完整图表数据或报告 JSON", prompt)

    def test_native_role_system_prompt_contains_graph_analysis_reporting_rules(self) -> None:
        prompt = build_role_system_prompt("direct", protocol="native")
        self.assertIn("## 图分析报告原则", prompt)
        self.assertIn("生成报告前必须先取得可靠工具结果", prompt)
        self.assertIn("create_analysis_report", prompt)
        self.assertIn("source_results", prompt)
        self.assertIn("overview、analysis、diagnosis、comparison、custom", prompt)
        self.assertIn("报告规则不覆盖图可视化规则", prompt)
        self.assertIn("点击查看报告", prompt)

    def test_driver_is_none_without_provider_gateway(self) -> None:
        cm = ContextManager(session_id="s1")
        self.assertIsNone(cm._driver)

    def test_update_messages(self) -> None:
        cm = ContextManager(session_id="s1")
        new_msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        cm.update_messages(new_msgs)
        self.assertEqual(cm.messages, new_msgs)

    def test_build_model_messages_accepts_working_messages_override(self) -> None:
        class FakeDriver:
            def build_model_messages(self, working_messages, tool_specs, **kwargs):
                return [*working_messages, {"role": "tool_specs", "content": str(len(tool_specs))}]

        cm = ContextManager(session_id="s1")
        cm._driver = FakeDriver()
        cm.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "main"},
        ]
        isolated = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "step-only"},
        ]

        result = cm.build_model_messages([], working_messages=isolated)

        self.assertEqual(result[1]["content"], "step-only")
        self.assertNotIn("main", [m.get("content") for m in result])

    def test_token_count_excludes_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "system prompt " * 100},
            {"role": "user", "content": "请分析这个图"},
        ]
        without_system = count_message_tokens(messages, include_system=False)
        with_system = count_message_tokens(messages, include_system=True)
        self.assertGreater(with_system, without_system)

    def test_context_usage_reports_limit_percent(self) -> None:
        usage = context_usage(
            [{"role": "user", "content": "hello world"}],
            limit=100,
        )
        self.assertEqual(usage["token_limit"], 100)
        self.assertIn("percent", usage)
        self.assertIn(usage["status"], {"ok", "warning", "critical"})

    def test_compaction_preserves_visible_history_and_recent_user_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            sid = "s1"
            store.append_message_record(
                sid,
                {
                    "message": {"role": "system", "content": "sys"},
                    "visible": False,
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "old-1"},
                    "visible": True,
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "answer-1"},
                    "visible": True,
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "old-2"},
                    "visible": True,
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "answer-2"},
                    "visible": True,
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "latest"},
                    "visible": True,
                    "include_in_context": True,
                },
            )
            store.compact_context_records(
                sid,
                {"role": "user", "content": "summary"},
                stats={"after_tokens": 10},
                preserve_recent_user_turns=1,
            )
            context = store.load_context_messages(sid)
            chat = store.load_chat_messages(sid)
            # System message must be preserved in context after compaction.
            context_roles = [m["role"] for m in context]
            self.assertEqual(context_roles[0], "system", "system message must survive compaction")
            context_contents = [m["content"] for m in context]
            self.assertEqual(context_contents, ["sys", "summary", "old-2", "answer-2", "latest"])
            self.assertEqual(
                [m["content"] for m in chat],
                ["old-1", "answer-1", "old-2", "answer-2", "latest"],
            )

    def test_load_chat_messages_restores_tool_call_arguments_from_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            sid = "s1"
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "path"},
                    "visible": True,
                    "display_content": "path",
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "answer"},
                    "visible": True,
                    "display_content": "answer",
                    "include_in_context": False,
                    "tools": [{"tool": "shortest_path", "done": True}],
                },
            )
            store.append_trace_record(
                sid,
                {"event": "TOOL_CALL", "tool": "shortest_path", "args": {"source": "A", "target": "B"}},
            )
            store.append_trace_record(
                sid,
                {
                    "event": "TOOL_EXEC_RAW",
                    "tool": "shortest_path",
                    "call_id": "call-1",
                    "arguments": {"source": "A", "target": "B"},
                    "result": "{}",
                },
            )
            store.append_trace_record(
                sid,
                {"event": "TOOL_RESULT", "tool": "shortest_path"},
            )
            store.append_trace_record(
                sid,
                {"event": "MODEL_RESPONSE_PARSED", "final_answer": "answer"},
            )

            chat = store.load_chat_messages(sid)

            self.assertEqual(chat[1]["tools"][0]["tool"], "shortest_path")
            self.assertEqual(chat[1]["tools"][0]["args"], {"source": "A", "target": "B"})
            self.assertEqual(chat[1]["tools"][0]["call_id"], "call-1")

    def test_load_chat_messages_restores_visible_graphs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            sid = "s1"
            graph = {
                "id": "ig-call-1",
                "nodes": [{"id": "A", "label": "A"}, {"id": "B", "label": "B"}],
                "edges": [{"src": "A", "dst": "B", "source": "A", "target": "B", "weight": 1.0}],
                "directed": True,
                "num_nodes": 2,
                "num_edges": 1,
                "activeHighlights": {},
            }
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "可视化图"},
                    "visible": True,
                    "display_content": "可视化图",
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "已生成图"},
                    "visible": True,
                    "display_content": "已生成图",
                    "include_in_context": False,
                    "graphs": [graph],
                },
            )

            chat = store.load_chat_messages(sid)

            self.assertEqual(chat[1]["graphs"], [graph])

    def test_load_chat_messages_merges_visible_graphs_on_assistant_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            sid = "s1"
            graph_a = {"id": "ig-a", "nodes": [{"id": "A"}], "edges": [], "directed": True}
            graph_b = {"id": "ig-b", "nodes": [{"id": "B"}], "edges": [], "directed": True}
            store.append_message_record(
                sid,
                {
                    "message": {"role": "user", "content": "可视化两张图"},
                    "visible": True,
                    "display_content": "可视化两张图",
                    "include_in_context": True,
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "第一张"},
                    "visible": True,
                    "display_content": "第一张",
                    "include_in_context": False,
                    "graphs": [graph_a],
                },
            )
            store.append_message_record(
                sid,
                {
                    "message": {"role": "assistant", "content": "第二张"},
                    "visible": True,
                    "display_content": "第二张",
                    "include_in_context": False,
                    "graphs": [graph_b],
                },
            )

            chat = store.load_chat_messages(sid)

            self.assertEqual(len(chat), 2)
            self.assertEqual(chat[1]["content"], "第一张\n\n第二张")
            self.assertEqual(chat[1]["graphs"], [graph_a, graph_b])

    def test_skill_prompt_loads_matching_skill_files(self) -> None:
        prompt = build_skills_system_prompt(
            [
                {"name": "get_graph_info"},
                {"name": "load_graph_from_upload"},
                {"name": "train"},
                {"name": "infer"},
            ],
            graph_state={"has_graph": True},
        )

        self.assertIn("## Available Skills", prompt)
        self.assertIn("graph_state_check", prompt)
        self.assertIn("data_ingestion_table", prompt)
        self.assertIn("graph_ml_training", prompt)
        self.assertIn("graph_ml_inference", prompt)
        self.assertNotIn("当用户请求与 GraphML 相关", prompt)

    def test_skill_prompt_loads_statistical_reporting_skill_when_report_tool_available(self) -> None:
        prompt = build_skills_system_prompt(
            [
                {"name": "create_analysis_report"},
                {"name": "get_graph_info"},
                {"name": "community_detection"},
                {"name": "degree_analysis"},
            ],
            graph_state={"loaded": True},
        )

        self.assertIn("graph_statistical_reporting", prompt)
        self.assertIn("图分析报告生成流程", prompt)

        skill_body = handle_skill_tool_call("skill_view", {"name": "graph_statistical_reporting"})
        self.assertIsInstance(skill_body, str)
        self.assertIn("analysis_goal", skill_body)
        self.assertIn("report_intent", skill_body)
        self.assertIn("`report_intent` 选择策略", skill_body)
        self.assertIn("`source_results` 组织规则", skill_body)
        self.assertIn("`options` 建议", skill_body)
        self.assertIn("失败回退", skill_body)
        self.assertIn("overview", skill_body)
        self.assertIn("analysis", skill_body)
        self.assertIn("diagnosis", skill_body)
        self.assertIn("comparison", skill_body)
        self.assertIn("custom", skill_body)
        self.assertIn("top_k: 10", skill_body)
        self.assertIn("max_sections: 6", skill_body)
        self.assertIn("include_narrative: true", skill_body)
        self.assertIn("visualize_subgraph", skill_body)

    def test_reporting_skill_is_not_active_without_report_tool(self) -> None:
        prompt = build_skills_system_prompt(
            [
                {"name": "get_graph_info"},
                {"name": "degree_analysis"},
                {"name": "community_detection"},
            ],
            graph_state={"loaded": True},
        )

        self.assertNotIn("graph_statistical_reporting", prompt)
        role_prompt = build_role_system_prompt("direct", protocol="text")
        self.assertIn("create_analysis_report", role_prompt)
        self.assertIn("回退为普通文本总结", role_prompt)

    def test_skill_view_loads_full_skill_body(self) -> None:
        result = handle_skill_tool_call("skill_view", {"name": "graph_ml_training"})
        self.assertIsInstance(result, str)
        self.assertIn("GraphML 训练", result)
        self.assertIn("`train` 工具 schema", result)
        self.assertIn("高风险确认流程", result)


if __name__ == "__main__":
    unittest.main()
