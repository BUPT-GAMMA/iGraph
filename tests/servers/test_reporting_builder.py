from __future__ import annotations

import json
import unittest

from graph_data_service.reporting import build_analysis_report


def _section(report: dict, section_id: str) -> dict:
    for item in report["sections"]:
        if item["id"] == section_id:
            return item
    raise AssertionError(f"missing section {section_id}")


class ReportingBuilderTest(unittest.TestCase):
    def test_empty_source_results_returns_error(self) -> None:
        payload = build_analysis_report("analysis", "", "", {}, None)

        self.assertFalse(payload["success"])
        self.assertIn("source_results", payload["error"])

    def test_graph_info_builds_overview_section(self) -> None:
        payload = build_analysis_report(
            report_intent="overview",
            analysis_goal="总结当前图",
            title="",
            source_results={
                "get_graph_info": {
                    "loaded": True,
                    "graph_info": {
                        "num_nodes": 4,
                        "num_edges": 3,
                        "directed": True,
                        "has_edge_weight": False,
                        "has_node_feat": True,
                        "node_feat_dim": 8,
                        "node_label_stats": {"visible": 3, "missing": 1},
                        "node_type_values": ["person"],
                        "edge_type_values": ["knows"],
                    },
                }
            },
        )

        self.assertTrue(payload["success"])
        report = payload["report"]
        self.assertEqual(report["intent"], "overview")
        self.assertEqual(report["title"], "图分析报告")
        overview = _section(report, "structure-overview")
        labels = {m["label"]: m["value"] for m in overview["metrics"]}
        self.assertEqual(labels["节点数"], 4)
        self.assertEqual(labels["边数"], 3)
        self.assertEqual(labels["图方向"], "有向")
        self.assertEqual(labels["节点特征维度"], 8)

    def test_degree_analysis_builds_distribution_section_and_respects_top_k(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "查看连接分布",
            "连接报告",
            {
                "degree_analysis": {
                    "success": True,
                    "directed": False,
                    "degree_stats": {"mean": 2.5, "max": 5, "min": 1, "median": 2},
                    "avg_degree": 2.5,
                    "top_degree_nodes": [
                        {"node": "A", "degree": 5},
                        {"node": "B", "degree": 4},
                        {"node": "C", "degree": 3},
                    ],
                    "truncated": True,
                }
            },
            {"top_k": 2},
        )

        report = payload["report"]
        section = _section(report, "degree-distribution")
        self.assertEqual(section["charts"][0]["data"], [{"label": "A", "value": 5}, {"label": "B", "value": 4}])
        self.assertEqual(len(section["tables"][0]["rows"]), 2)
        self.assertIn("节点列表已按展示上限截断。", report["limitations"])

    def test_node_centrality_builds_ranking_section(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "识别关键对象",
            "关键对象报告",
            {
                "node_centrality": {
                    "success": True,
                    "centrality_type": "pagerank",
                    "value_stats": {"mean": 0.25, "max": 0.4, "min": 0.1, "median": 0.2},
                    "top_nodes": [{"node": "A", "value": 0.4}, {"node": "B", "value": 0.3}],
                    "truncated": False,
                }
            },
        )

        section = _section(payload["report"], "centrality-ranking")
        self.assertEqual(section["title"], "关键对象")
        self.assertEqual(section["charts"][0]["data"][0], {"label": "A", "value": 0.4})
        self.assertEqual(section["tables"][0]["rows"][0]["node"], "A")

    def test_community_detection_builds_structure_section(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "解释结构模式",
            "结构报告",
            {
                "community_detection": {
                    "success": True,
                    "algorithm": "louvain",
                    "num_communities": 3,
                    "modularity": 0.72,
                    "size_stats": {"mean": 10, "max": 20, "min": 3, "median": 7},
                    "communities": [
                        {"community_id": 1, "size": 20, "nodes_truncated": True},
                        {"community_id": 2, "size": 7, "nodes_truncated": False},
                    ],
                    "communities_truncated": True,
                }
            },
        )

        report = payload["report"]
        section = _section(report, "structure-patterns")
        labels = {m["label"]: m["value"] for m in section["metrics"]}
        self.assertEqual(labels["分组数量"], 3)
        self.assertEqual(section["charts"][0]["data"][0], {"label": "1", "value": 20})
        self.assertIn("结构分组列表已按展示上限截断。", report["limitations"])
        self.assertIn("部分结构分组的节点预览已截断。", report["limitations"])

    def test_global_metrics_supports_list_results(self) -> None:
        payload = build_analysis_report(
            "diagnosis",
            "查看整体指标",
            "整体指标报告",
            {
                "global_metrics": [
                    {"success": True, "metric": "density", "value": 0.25},
                    {"success": True, "metric": "average_clustering", "value": 0.6},
                ]
            },
        )

        section = _section(payload["report"], "global-metrics")
        labels = {m["label"]: m["value"] for m in section["metrics"]}
        self.assertEqual(labels["密度"], 0.25)
        self.assertEqual(labels["平均聚类系数"], 0.6)

    def test_mixed_results_respects_max_sections_and_json_serializable(self) -> None:
        payload = build_analysis_report(
            "custom",
            "混合分析",
            "混合报告",
            {
                "graph_info": {"num_nodes": 3, "num_edges": 2, "directed": False},
                "degree_analysis": {
                    "success": True,
                    "directed": False,
                    "degree_stats": {"mean": 1.3, "max": 2, "min": 1, "median": 1},
                    "top_degree_nodes": [{"node": "A", "degree": 2}],
                },
                "node_centrality": {
                    "success": True,
                    "centrality_type": "degree",
                    "value_stats": {"mean": 0.5, "max": 1, "min": 0, "median": 0.5},
                    "top_nodes": [{"node": "A", "value": 1.0}],
                },
                "global_metrics": {"success": True, "metric": "density", "value": 0.3},
            },
            {"max_sections": 2},
        )

        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["report"]["sections"]), 2)
        self.assertIn("报告 section 已限制为前 2 个。", payload["report"]["limitations"])
        json.dumps(payload, ensure_ascii=False)

    def test_missing_fields_returns_partial_report_with_limitations(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "字段缺失测试",
            "字段缺失报告",
            {"get_graph_info": {"graph_info": {"num_nodes": 4}}},
        )

        self.assertTrue(payload["success"])
        self.assertIn("结构概览缺少节点数或边数。", payload["report"]["limitations"])


if __name__ == "__main__":
    unittest.main()
