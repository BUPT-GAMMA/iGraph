---
name: dataset_management
description: 保存、加载、版本说明和复用 dataset 的流程。
version: 1.0.0
domain: dataset
risk_level: medium
metadata:
  igraph:
    any_tools: [load_dataset, save_dataset, list_datasets]
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
