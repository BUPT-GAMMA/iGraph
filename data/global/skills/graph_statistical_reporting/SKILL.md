---
name: graph_statistical_reporting
description: 图分析报告生成流程：根据用户分析目标和工具事实组织报告 artifact。
version: 1.0.0
domain: reporting
risk_level: low
metadata:
  igraph:
    any_tools: [create_analysis_report]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis, graph_report, statistical_summary, structural_explanation, diagnostic_analysis, comparison_analysis]
---

# 图分析报告

## 适用场景

当用户希望获得整体理解、统计总结、结构解释、模式发现、对比分析、诊断判断或生成报告时使用。报告内容应由工具结果、图状态或用户明确提供的数据支撑。

## 操作步骤

1. 确认当前图状态，明确用户的 `analysis_goal`。
2. 根据 `analysis_goal` 调用必要分析工具取得事实，例如 `get_graph_info`、`degree_analysis`、`node_centrality`、`global_metrics` 或其他能回答问题的图分析工具。
3. 汇总工具结果为 `source_results`。
4. 调用 `create_analysis_report`，传入 `report_intent`、`analysis_goal`、`title`、`source_results` 和 `options`。
5. 最终回答只给简短结论，并提示用户查看右侧报告。

## 轻量答复场景

单个节点、边、路径、指标值或明确要求简短回答的请求保持轻量文本答复。

## 验证

- 报告中的指标、图表和洞察均能追溯到工具结果、图状态或用户明确提供的数据。
- 聊天区只保留摘要和查看报告提示，不重复完整表格。
