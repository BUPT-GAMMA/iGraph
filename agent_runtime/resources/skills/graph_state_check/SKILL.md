---
name: graph_state_check
description: 加载状态、规模、有向/无向、权重和属性检查流程。
metadata:
  version: 1.0.0
  domain: graph_analysis
  risk_level: low
  igraph:
    any_tools: [get_graph_info]
    requires_graph_state: none
---

# 图状态检查

## 适用场景
在分析、训练、推理、路径搜索之前，或用户询问当前加载了哪些数据时使用。

## 操作步骤
1. 调用 `get_graph_info`。
2. 确认图是否已加载。
3. 报告节点数、边数、有向性、权重可用性及相关属性。
4. 如未加载图且 `session_state.data_state.active_dataset_id` 已指定，先检查构图状态；
   ready 时使用 `load_constructed_graph` 复用保存图，SQL/CSV/TXT 算法未就绪时按 `graph_construction` 流程说明。
   没有可用数据集时再询问用户数据。

## 验证
在加载状态明确之前，不要运行下游算法。
