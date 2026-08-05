from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from backend.agent.igraph import IGraph
from backend.logging.logger import SessionLogger
from backend.logging.session_store import SessionStore


def _report_result() -> str:
    return json.dumps(
        {
            "success": True,
            "report": {
                "version": "analysis_report.v1",
                "id": "rpt-test",
                "intent": "analysis",
                "title": "测试图分析",
                "summary": "连接分布呈现长尾特征。",
                "sections": [
                    {
                        "id": "degree-distribution",
                        "title": "连接分布",
                        "metrics": [{"label": "平均度", "value": 2.5}],
                        "charts": [
                            {
                                "type": "bar",
                                "title": "度数 Top",
                                "data": [
                                    {
                                        "label": "node-should-not-enter-context",
                                        "value": 10,
                                    }
                                ],
                            }
                        ],
                        "tables": [
                            {
                                "title": "节点明细",
                                "columns": ["节点", "度数"],
                                "rows": [["A", 10]],
                            }
                        ],
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


class ReportArtifactAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(IGraph)
        self.agent._visible_reports_for_turn = []
        self.agent._visible_graphs_for_turn = []

    def test_build_report_event_accepts_v1_report(self) -> None:
        event = IGraph._build_report_event(_report_result(), call_id="call-1")
        self.assertIsNotNone(event)
        self.assertEqual(event["type"], "analysis_report")
        self.assertEqual(event["call_id"], "call-1")
        self.assertEqual(event["data"]["report"]["id"], "rpt-test")

    def test_build_report_event_rejects_invalid_results(self) -> None:
        self.assertIsNone(IGraph._build_report_event("not-json"))
        self.assertIsNone(
            IGraph._build_report_event(
                json.dumps({"success": False, "error": "failed"})
            )
        )
        self.assertIsNone(
            IGraph._build_report_event(
                json.dumps(
                    {
                        "success": True,
                        "report": {"version": "analysis_report.v2"},
                    }
                )
            )
        )

    def test_prepare_report_emits_artifact_and_compacts_observation(self) -> None:
        events, observation = self.agent._prepare_tool_result_for_observation(
            "create_analysis_report",
            _report_result(),
            call_id="call-1",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "analysis_report")
        self.assertIn("测试图分析", observation)
        self.assertIn("1 个章节、1 个指标、1 个图表和 1 个表格", observation)
        self.assertNotIn("node-should-not-enter-context", observation)
        self.assertEqual(
            [report["id"] for report in self.agent._visible_reports_for_turn],
            ["rpt-test"],
        )

        self.agent._prepare_tool_result_for_observation(
            "create_analysis_report",
            _report_result(),
            call_id="call-2",
        )
        self.assertEqual(len(self.agent._visible_reports_for_turn), 1)

    def test_failed_report_stays_as_normal_tool_result(self) -> None:
        result = json.dumps({"success": False, "error": "缺少 source_results"})
        events, observation = self.agent._prepare_tool_result_for_observation(
            "create_analysis_report",
            result,
            call_id="call-1",
        )
        self.assertEqual(events, [])
        self.assertEqual(observation, result)

    def test_visualize_subgraph_path_is_unchanged(self) -> None:
        result = json.dumps(
            {
                "success": True,
                "nodes": [{"id": "A"}, {"id": "B"}],
                "edges": [{"src": "A", "dst": "B"}],
                "directed": False,
                "num_nodes": 2,
                "num_edges": 1,
            }
        )
        events, observation = self.agent._prepare_tool_result_for_observation(
            "visualize_subgraph",
            result,
            call_id="graph-call",
        )
        self.assertEqual(events[0]["type"], "graph_visualize")
        self.assertIn("2 个节点，1 条边", observation)
        self.assertEqual(len(self.agent._visible_graphs_for_turn), 1)


class ReportArtifactPersistenceTest(unittest.TestCase):
    def test_logger_persists_reports_and_store_deduplicates_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            logger = SessionLogger(
                session_id="session-report",
                server_logger=logging.getLogger("test.report-artifact"),
                store=store,
            )
            report = json.loads(_report_result())["report"]
            graph = {
                "id": "ig-test",
                "nodes": [{"id": "A"}, {"id": "B"}],
                "edges": [{"src": "A", "dst": "B"}],
            }

            logger.log_assistant_message(
                "第一段",
                graphs=[graph],
                reports=[report],
            )
            logger.log_assistant_message(
                "第二段",
                reports=[report],
            )

            messages = store.load_chat_messages("session-report")
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["content"], "第一段\n\n第二段")
            self.assertEqual(messages[0]["graphs"][0]["id"], "ig-test")
            self.assertEqual(
                [item["id"] for item in messages[0]["reports"]],
                ["rpt-test"],
            )

    def test_old_assistant_record_without_reports_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            store.append_message_record(
                "legacy-session",
                {
                    "message": {"role": "assistant", "content": "旧消息"},
                    "visible": True,
                    "display_content": "旧消息",
                },
            )
            messages = store.load_chat_messages("legacy-session")
            self.assertEqual(messages[0]["content"], "旧消息")
            self.assertNotIn("reports", messages[0])


class ReportArtifactFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frontend = (
            Path(__file__).resolve().parents[1] / "index.html"
        ).read_text(encoding="utf-8")

    def test_frontend_handles_and_renders_report_artifacts(self) -> None:
        self.assertIn("case 'analysis_report':", self.frontend)
        self.assertIn('id="streaming-reports"', self.frontend)
        self.assertIn("lastMsg.reports.push(report)", self.frontend)
        self.assertIn("reportEntryCardHtml(report)", self.frontend)

    def test_frontend_normalizes_and_merges_report_history(self) -> None:
        self.assertIn("function normalizeReportInfo(", self.frontend)
        self.assertIn("function mergeReportArtifacts(", self.frontend)
        self.assertIn("row.reports = Array.isArray(m.reports)", self.frontend)
        self.assertIn("prev.reports = mergeReportArtifacts(", self.frontend)

    def test_artifacts_open_only_from_message_entries(self) -> None:
        self.assertIn("function openArtifactWorkspace(", self.frontend)
        self.assertIn("openArtifactWorkspace('report', normalizedId", self.frontend)
        self.assertIn("openArtifactWorkspace('graph', sourceId", self.frontend)
        self.assertNotIn('id="graph-sidebar-toggle"', self.frontend)
        self.assertNotIn("onNewAnalysisReportSidebar(", self.frontend)
        self.assertNotIn("if (state.graphSidebarOpen)", self.frontend)

    def test_report_preview_and_source_share_deterministic_document(self) -> None:
        self.assertIn("function buildReportDocument(", self.frontend)
        self.assertIn("const source = artifactReportSource(value);", self.frontend)
        self.assertIn("frame.srcdoc = source;", self.frontend)
        self.assertIn("buildReportCodeViewHtml(source)", self.frontend)
        self.assertIn('sandbox=""', self.frontend)
        self.assertIn('referrerpolicy="no-referrer"', self.frontend)
        self.assertIn("default-src 'none'", self.frontend)

        document_builder = self.frontend.split(
            "function buildReportDocument(", 1
        )[1].split("function buildReportCodeViewHtml(", 1)[0]
        public_text_helper = self.frontend.split(
            "function reportPublicText(", 1
        )[1].split("function reportChartRows(", 1)[0]
        self.assertNotIn("http://", document_builder)
        self.assertNotIn("https://", document_builder)
        self.assertNotIn("<script", document_builder)
        self.assertNotIn("<form", document_builder)
        self.assertNotIn("Date.now", document_builder)
        self.assertNotIn("Math.random", document_builder)
        self.assertNotIn("normalized.data_sources", document_builder)
        self.assertNotIn('class="sources"', document_builder)
        self.assertNotIn("数据来源", document_builder)
        self.assertNotIn("由 iGraph 基于", document_builder)
        self.assertIn("function reportPublicText(", self.frontend)
        self.assertIn("reportPublicText(normalized.summary", self.frontend)
        self.assertIn("reportPublicText(normalized.subtitle", self.frontend)
        self.assertIn("filter(key => key !== 'source')", self.frontend)
        self.assertIn("/工具结果/", public_text_helper)
        self.assertIn("/报告基于/", public_text_helper)

    def test_artifact_workspace_supports_responsive_fullscreen_and_resize(self) -> None:
        self.assertIn("_ARTIFACT_SPLIT_BREAKPOINT = 1200", self.frontend)
        self.assertIn("_ARTIFACT_WORKSPACE_MIN = 480", self.frontend)
        self.assertIn("_ARTIFACT_WORKSPACE_MAX = 960", self.frontend)
        self.assertIn("igraph_artifact_workspace_width", self.frontend)
        self.assertIn("igraph_graph_sidebar_width", self.frontend)
        self.assertIn("function toggleArtifactWorkspaceFullscreen(", self.frontend)
        self.assertIn("function refreshArtifactPreview(", self.frontend)


if __name__ == "__main__":
    unittest.main()
