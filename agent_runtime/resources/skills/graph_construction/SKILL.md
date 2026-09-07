---
name: graph_construction
description: SQL/CSV/TXT 数据集构图状态、保存图复用，以及六类图结构优化的预览、确认和持久化流程。
metadata:
  version: 1.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    requires_tools: [get_graph_construction_status, get_constructed_graph, load_constructed_graph]
    any_tools: [get_graph_construction_capabilities, preview_graph_optimization, commit_graph_optimization, visualize_subgraph]
    requires_graph_state: none
    activates_on_intents: [data_ingestion, graph_construction, dataset_management, graph_optimization]
---

# 数据集构图与优化

SQL、CSV、TXT 文件作为原始数据源保存到数据集。构图算法有独立扩展入口，不能把旧边表解析或文本抽取当成这些算法的实现。

## 状态与复用

1. 从 `session_state.data_state.active_dataset_id` 或已核验的数据集列表取得目标 ID，不猜测 ID、文件路径或所属用户。
   当前消息含附件时优先查看 `session_state.data_state.construction_sources`，它给出这些附件对应的
   `dataset_id`、`name`、`source_filename`、`source_format`、`status`。用户针对某附件提出请求时，
   按该条记录选择数据集，不能直接使用另一个 `active_dataset_id`；多个附件按文件名和用户意图区分。
   `available_constructed_datasets` 列出当前用户和当前项目/个人范围内可加载、优化的已构图数据集。
   加载及预览/提交只接受该列表或当前活动数据集的 ID；缺少有效会话范围时先通过前端选定数据集。
2. 调用 `get_graph_construction_status(dataset_id)`。需要了解算法支持时调用 `get_graph_construction_capabilities()`。
3. `ready`：通过 `load_constructed_graph(dataset_id)` 加载已保存的版本，再使用现有图分析和可视化工具。切换会话也可以复用同一数据集，无需重复构图。
4. `not_built`：引导用户在数据集面板中点击“构图”。`algorithm_unavailable`：说明该格式的构图算法尚未接入，原文件仍保留，不得描述成成功。`failed`：报告实际错误。
5. 不能用 `load_graph_from_upload` 处理这些源文件，也不能读取后复制到 `load_graph_from_text`、`load_graph` 或三元组工具绕开构图入口。用户直接粘贴的独立内联数据按已有导入流程处理。

## 六类编辑

先调用 `get_constructed_graph` 读取版本与实际节点、边及属性。每个操作含 `op` 字段：

| op | 必需参数 | 效果 |
| --- | --- | --- |
| collapse_node | node_id, edge_type | 将只承担连接作用的中间节点压缩为边 |
| node_property_to_edge | node_id, property, edge_ids | 将节点属性放到指定关联边；可指定 target_property、remove_from_node |
| extract_property_node | node_ids, property, node_type, edge_type | 将属性提取为独立节点；可指定 remove_from_node |
| delete | target=node/edge, ids | 删除指定元素；property 指定时只删该属性，cascade 需显式指定 |
| reverse_edges | edge_ids | 反转指定边方向 |
| rename_type | target=node/edge, old_type, new_type | 修改业务类型名称 |

1. 用读取到的 `base_revision` 与完整 `operations` 调用 `preview_graph_optimization`。
2. 展示预览差异，解释节点、边、属性的影响，保留用户意图之外的内容。属性覆盖或不符合要求的压缩可能被拒绝；根据实际错误调整方案。
3. 只有预览成功后，才以完全相同的版本和操作调用 `commit_graph_optimization`，由现有工具确认流程获取用户批准。不要把预览称为已保存。
4. 冲突时重新读取、预览并确认，不能直接改为最新 revision 重放。提交失败或状态未知时先读取状态核实，不能自动重复提交。
5. 提交成功报告新 revision，再调用 `load_constructed_graph` 更新当前会话，调用 `get_graph_info` 核验，并用 `visualize_subgraph` 展示保存结果。
