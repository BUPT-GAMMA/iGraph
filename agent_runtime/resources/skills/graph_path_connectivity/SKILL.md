---
name: graph_path_connectivity
description: shortest path、connected components、可达性和连通性分析流程。
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [shortest_path, connected_components]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# 路径与连通性

## 适用场景
用于最短路径、可达性、非连通图、连通分量或桥接类问题。

## 操作步骤
1. 确认图状态和有向性。
2. 根据问题选择路径或连通分量工具。
3. 执行最小范围的相关分析。
4. 明确解释不可达的情况。

## 验证
检查边权重是否应影响路径长度。
