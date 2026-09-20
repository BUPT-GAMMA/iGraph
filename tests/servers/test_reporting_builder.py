from __future__ import annotations

import json
import unittest

from graph_data_service.reporting import build_analysis_report


def _section(report: dict, section_id: str) -> dict:
    for item in report["sections"]:
        if item["id"] == section_id:
            return item
    raise AssertionError(f"missing section {section_id}")


def _visible_report_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(
            _visible_report_text(item)
            for key, item in value.items()
            if key not in {"id", "intent", "version", "data_sources"}
        )
    if isinstance(value, list):
        return " ".join(_visible_report_text(item) for item in value)
    if isinstance(value, str):
        return value
    return ""


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
        self.assertEqual(report["version"], "analysis_report.v2")
        self.assertEqual(report["title"], "图分析报告")
        overview = _section(report, "structure-overview")
        labels = {m["label"]: m["value"] for m in overview["metrics"]}
        self.assertEqual(labels["节点数"], 4)
        self.assertEqual(labels["边数"], 3)
        self.assertEqual(labels["图方向"], "有向")
        self.assertEqual(labels["节点特征维度"], 8)

    def test_invalid_intent_empty_goal_and_options_are_normalized(self) -> None:
        payload = build_analysis_report(
            report_intent="not-a-real-intent",
            analysis_goal="",
            title="",
            source_results={
                "graph_info": {
                    "num_nodes": 2,
                    "num_edges": 1,
                    "directed": False,
                }
            },
            options={"top_k": 100, "max_sections": 100, "include_narrative": "false"},
        )

        self.assertTrue(payload["success"])
        report = payload["report"]
        self.assertEqual(report["intent"], "analysis")
        self.assertEqual(report["title"], "图分析报告")
        self.assertEqual(report["summary"], "2 个节点 · 1 条边。")
        overview = _section(report, "structure-overview")
        self.assertEqual(overview["insights"], [])

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
        section = _section(report, "connections-and-key-nodes")
        self.assertEqual(section["title"], "连接与关键节点")
        self.assertEqual(section["charts"][0]["data"], [{"label": "A", "value": 5}, {"label": "B", "value": 4}])
        self.assertEqual(section["tables"], [])
        self.assertIn("仅展示排名前 2 的节点。", report["limitations"])

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

        section = _section(payload["report"], "connections-and-key-nodes")
        self.assertEqual(section["title"], "连接与关键节点")
        self.assertEqual(section["charts"][0]["data"][0], {"label": "A", "value": 0.4})
        self.assertEqual(section["tables"], [])

    def test_condensed_directed_degree_and_centrality_are_merged(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "分析引文网络",
            "引文网络报告",
            {
                "degree_analysis": {
                    "success": True,
                    "avg_in_degree": 3.8981,
                    "avg_out_degree": 3.8981,
                    "total_degree_mean": 7.7962,
                    "top_degree_nodes": [
                        {"node": "163", "total": 336},
                        {"node": "565", "total": 156},
                    ],
                },
                "node_centrality": {
                    "success": True,
                    "centrality_type": "pagerank",
                    "top_nodes": [
                        {"node": "163", "value": 0.012207},
                        {"node": "565", "value": 0.005058},
                    ],
                },
            },
        )

        report = payload["report"]
        section = _section(report, "connections-and-key-nodes")
        labels = {item["label"]: item["value"] for item in section["metrics"]}
        self.assertEqual(labels["平均总度"], 7.7962)
        self.assertEqual(labels["平均入度"], 3.8981)
        self.assertEqual(section["charts"][0]["data"][0], {
            "label": "163",
            "value": 336,
            "detail": "PageRank 0.012207",
        })
        self.assertNotIn("degree-distribution", {item["id"] for item in report["sections"]})
        self.assertNotIn("centrality-ranking", {item["id"] for item in report["sections"]})

    def test_connectivity_uses_readable_labels_and_nested_summaries(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "查看连通性",
            "连通性报告",
            {
                "graph_connectivity": {
                    "success": True,
                    "summary": {
                        "directed": True,
                        "is_weakly_connected": True,
                        "is_strongly_connected": False,
                        "weakly_connected_components": {"count": 1, "largest_size": 2708},
                        "strongly_connected_components": {"count": 371, "largest_size": 2336},
                    },
                }
            },
        )

        section = _section(payload["report"], "connectivity")
        labels = {item["label"]: item["value"] for item in section["metrics"]}
        self.assertEqual(labels["弱连通"], "是")
        self.assertEqual(labels["强连通"], "否")
        self.assertEqual(labels["最大弱连通分量"], 2708)
        self.assertNotIn("is_weakly_connected", _visible_report_text(payload["report"]))

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
        self.assertEqual(labels["社区数"], 3)
        self.assertEqual(section["title"], "社区结构")
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
        self.assertEqual(section["tables"], [])

    def test_global_metrics_supports_mapping_results(self) -> None:
        payload = build_analysis_report(
            "overview",
            "查看整体指标",
            "整体指标报告",
            {
                "global_metrics": {
                    "density": 0.125,
                    "cluster": {
                        "success": True,
                        "metric": "average_clustering",
                        "value": 0.5,
                    },
                }
            },
        )

        section = _section(payload["report"], "global-metrics")
        labels = {m["label"]: m["value"] for m in section["metrics"]}
        self.assertEqual(labels["密度"], 0.125)
        self.assertEqual(labels["平均聚类系数"], 0.5)

    def test_supplemental_successful_tool_results_are_summarized(self) -> None:
        payload = build_analysis_report(
            "custom",
            "汇总补充事实",
            "补充报告",
            {
                "custom_tool": {
                    "success": True,
                    "score": 0.91,
                    "note": "可展示事实",
                    "nested": {"ignored": True},
                },
                "failed_tool": {
                    "success": False,
                    "score": 1,
                },
            },
            {"top_k": 1},
        )

        section = _section(payload["report"], "supplemental-results")
        rows = section["tables"][0]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["field"], {"score", "note"})
        self.assertNotIn("source", rows[0])
        self.assertNotIn("failed_tool", payload["report"]["data_sources"])

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

    def test_report_id_is_stable_for_same_inputs(self) -> None:
        source_results = {
            "get_graph_info": {
                "graph_info": {
                    "num_nodes": 3,
                    "num_edges": 2,
                    "directed": False,
                }
            }
        }
        first = build_analysis_report("analysis", "稳定性", "稳定报告", source_results)
        second = build_analysis_report("analysis", "稳定性", "稳定报告", source_results)

        self.assertEqual(first["report"]["id"], second["report"]["id"])

    def test_visible_report_copy_hides_internal_tool_language(self) -> None:
        payload = build_analysis_report(
            "analysis",
            "总结当前图",
            "干货报告",
            {
                "get_graph_info": {
                    "graph_info": {
                        "num_nodes": 3,
                        "num_edges": 2,
                        "directed": False,
                    }
                },
                "degree_analysis": {
                    "success": True,
                    "degree_stats": {"mean": 1.3, "max": 2, "min": 1, "median": 1},
                    "top_degree_nodes": [{"node": "A", "degree": 2}],
                },
                "custom_tool": {
                    "success": True,
                    "score": 0.9,
                },
            },
        )

        self.assertTrue(payload["success"])
        visible = _visible_report_text(payload["report"])
        self.assertNotIn("工具结果", visible)
        self.assertNotIn("数据来源", visible)
        self.assertNotIn("基于当前", visible)
        self.assertNotIn("报告基于", visible)
        self.assertNotIn("get_graph_info", visible)
        self.assertNotIn("degree_analysis", visible)
        self.assertNotIn("custom_tool", visible)


if __name__ == "__main__":
    unittest.main()
