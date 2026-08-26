---
name: data_ingestion_table
description: 表格上传、输入格式确认、加载和结果验证流程。
version: 1.0.0
domain: data_ingestion
risk_level: medium
metadata:
  igraph:
    any_tools: [load_graph_from_upload]
    requires_graph_state: none
    activates_on_intents: [data_ingestion]
---

# 表格数据导入

## 适用场景
用于用户上传或引用表格形式的边/节点数据时。

## 必要检查
- 加载 CSV 或 Excel 数据前确认用户指定的输入格式。
- 保留所指定格式中对有向性和权重的假设。

## 操作步骤
1. 确认上传文件的目标图输入格式。
2. 格式明确后，使用 `load_graph_from_upload` 加载文件。
3. 报告节点数、边数、有向性和权重状态。

## 验证
构建完成后，调用图状态工具或使用工具返回结果确认图已成功加载。
