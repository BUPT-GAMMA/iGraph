---
name: graph_analysis_community
description: 社区检测算法选择、社区解释和子图联动分析流程。
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [community_detection, run_large_graph_algorithm]
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

## 高入度节点的社区归属
当用户询问“高入度/被引用最多节点属于哪些社区”时，必须按以下顺序执行：

1. 对原始图版本运行一次精确 `in_degree`，`top_k` 使用用户要求的数量（未指定时为 20）。
   大图使用 `run_large_graph_algorithm`，不得用按总度排序的
   `degree_analysis.top_degree_nodes` 冒充入度排名。
2. 从精确入度结果的 `summary.top` 中提取节点 ID。
3. 仍在同一个原始图版本上只运行一次 `community_detection`，把上述节点 ID 作为
   `focus_nodes` 传入。千万级节点图默认使用 `algorithm=label_propagation`、
   `max_iterations=5`；必须向用户说明这是可扩展的加权标签传播近似。
4. 使用返回的 `focus_node_communities` 汇总每个高入度节点的入度、社区 ID、社区规模
   和代表节点。

此类问题禁止调用 `subgraph_extraction/ego_graph`，禁止逐节点重复社区计算，也禁止设置
`create_version=true`。如果精确入度失败，应报告错误并停止，不能回退到总度排名。

## 验证
说明算法假设，以及有向边或权重边对结果的影响。
