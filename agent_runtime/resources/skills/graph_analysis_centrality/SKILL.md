---
name: graph_analysis_centrality
description: 中心性算法选择、解释、适用边界和结果复核流程。
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [node_centrality]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# 中心性分析

## 适用场景
用于重要节点、有影响力的节点、枢纽节点、桥接节点或排名类问题。

## 操作步骤
1. 确认图状态和有向性。
2. 根据问题选择合适的中心性方法。
3. 执行中心性分析。
4. 用通俗语言解释指标含义，并指出其局限性。

## 注意事项
除非图的语义支持，否则不要将中心性呈现为因果影响力。
