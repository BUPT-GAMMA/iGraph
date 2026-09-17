---
name: dataset_management
description: 保存、加载、版本说明和复用 dataset 的流程。
metadata:
  version: 1.0.0
  domain: dataset
  risk_level: medium
  igraph:
    any_tools: [load_dataset, save_as_dataset, list_datasets, get_graph_construction_status, load_constructed_graph]
    requires_graph_state: none
---

# 数据集管理

## 适用场景
用于用户需要保存、重新加载、列举、版本管理或复用图数据集时。

## 操作步骤
1. 加载时：列出或确认数据集和版本。
2. 保存时：确认当前图是目标产物。
3. 保留名称、描述和复用意图。
4. 成功后报告数据集 ID 和版本。

SQL/CSV/TXT 数据集使用 `graph_construction` 流程：先查询构图状态，`ready` 时调用
`load_constructed_graph` 加载持久化结果；无需重复构图，也不用另存一份数据集。
构图优化通过预览与确认提交保存在原数据集的新 revision，后续会话可以直接加载最新版本。
