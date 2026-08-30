---
name: graph_statistical_reporting
description: 图分析报告生成流程：根据用户分析目标和工具事实组织报告 artifact。
metadata:
  version: 1.0.0
  domain: reporting
  risk_level: low
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
2. 根据 `analysis_goal` 调用必要分析工具取得事实，例如 `get_graph_info`、`degree_analysis`、`node_centrality`、`global_metrics`、`community_detection` 或其他能回答问题的图分析工具。
3. 将工具原始返回按真实工具名汇总为 `source_results`。
4. 调用 `create_analysis_report`，传入 `report_intent`、`analysis_goal`、`title`、`source_results` 和 `options`。
5. 最终回答只给 1-3 句简短结论，并提示用户点击聊天中的报告入口查看完整报告。

## `report_intent` 选择策略

- `overview`：用户要求概览、整体理解、统计总结或快速认识当前图。
- `analysis`：用户要求结构特征、模式解释、关键对象、分布现象或一般图分析。
- `diagnosis`：用户要求异常、瓶颈、风险、薄弱点、问题定位或原因判断。
- `comparison`：用户要求多个节点、子图、分组、算法结果或时间状态之间的对比。
- `custom`：用户明确指定特殊报告目标，且以上意图均不能准确表达。

## `source_results` 组织规则

- key 必须使用真实工具名，例如 `get_graph_info`、`degree_analysis`、`node_centrality`、`global_metrics`、`community_detection`。
- value 使用对应工具的原始返回结果，不改写、不补造字段。
- 优先包含 `get_graph_info` 或等价图基础信息，用于报告标题、摘要和规模上下文。
- 只加入与用户目标直接相关、已经取得且可信的工具结果。
- 图表、指标和洞察必须能追溯到 `source_results`、图状态或用户明确提供的数据。

## `options` 建议

- 默认使用 `top_k: 10`、`max_sections: 6`、`include_narrative: true`。
- 用户要求精简、快速或只看重点时，降低 `top_k` 或 `max_sections`。
- 用户要求详细报告时，可提高展示量，但不要超过工具 schema 允许的上限。
- 用户只要事实表或图表、不需要解释性文字时，可设置 `include_narrative: false`。

## 轻量答复场景

单个节点、边、路径、指标值或明确要求简短回答的请求保持轻量文本答复。

## 失败回退

- 若 `create_analysis_report` 不可用，不要臆造报告入口；基于已取得工具结果输出普通文本总结。
- 若报告工具返回失败或可用事实不足，说明报告未生成的直接原因，并保留已能支持的结论。
- 报告失败不影响已完成的图分析结论；聊天区仍应给用户可读、可追溯的回答。

## 与图可视化配合

若分析结果包含路径、关键节点、局部结构、社区/分组或其他适合展示的对象，可以同时调用 `visualize_subgraph` 生成图 artifact。报告 artifact 和图 artifact 可以并行存在。

## 验证

- 报告中的指标、图表和洞察均能追溯到工具结果、图状态或用户明确提供的数据。
- 聊天区只保留摘要和查看报告提示，不重复完整表格、完整图表数据或报告 JSON。
