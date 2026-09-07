---
name: data_ingestion_table
description: 表格上传与格式检查；SQL/CSV 原始数据源转至 graph_construction，已支持的其他图格式按既有流程加载。
metadata:
  version: 1.0.0
  domain: data_ingestion
  risk_level: medium
  igraph:
    any_tools: [load_graph_from_upload]
    requires_graph_state: none
    activates_on_intents: [data_ingestion]
---

# 表格数据导入

## 适用场景
用于用户上传或引用表格形式的边/节点数据时。SQL、CSV、TXT 文件必须先按 `graph_construction` Skill
检查数据集状态，ready 时加载已保存图；未构图时使用数据集面板入口，算法未实现时如实说明。
不能用本 Skill 的旧上传加载步骤替代构图算法。

## 必要检查
- 加载其他已支持表格格式前确认用户指定的输入格式。
- 保留所指定格式中对有向性和权重的假设。

## 操作步骤
1. 确认上传文件的目标图输入格式。
2. 格式明确后，使用 `load_graph_from_upload` 加载文件。
3. 报告节点数、边数、有向性和权重状态。

## 验证
构建完成后，调用图状态工具或使用工具返回结果确认图已成功加载。
