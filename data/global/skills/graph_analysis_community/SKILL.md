---
name: graph_analysis_community
description: 社区检测算法选择、社区解释和子图联动分析流程。
version: 1.0.0
domain: graph_analysis
risk_level: low
metadata:
  igraph:
    any_tools: [community_detection]
    requires_graph_state: loaded
    activates_on_intents: [graph_analysis]
---

# 社区分析

## 适用场景
用于聚类、群组、模块、社区、可疑群体或分割类问题。

## 操作步骤
1. 确认图状态。
2. 执行社区检测。
3. 解释社区的数量和规模。
4. 针对后续问题，在相关社区内检查关键节点或代表性成员。

## 验证
说明算法假设，以及有向边或权重边对结果的影响。
