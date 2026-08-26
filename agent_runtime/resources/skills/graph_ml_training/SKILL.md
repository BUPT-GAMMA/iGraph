---
name: graph_ml_training
description: GraphML 训练参数构建、工具 schema 展开与训练后指标总结流程。
version: 1.0.0
domain: graph_ml
risk_level: high
metadata:
  igraph:
    requires_tools: [train]
    any_tools: [train, visualize_model_architecture]
    requires_graph_state: loaded
    activates_on_intents: [graph_ml_train]
---

# GraphML 训练

## 必要检查
- 依据 `train` 工具 schema 准备参数。
- 尽可能从 schema 展开完整的 `train` 参数：即使用户未要求修改，也应显式列出所有适用字段及其默认值。

## 操作步骤
1. 确认图状态和任务类型。
2. 根据用户请求和工具 schema 构建完整的 `train` 参数集，包含所有适用的默认参数。
3. 调用 `train`。（高风险确认流程将在执行前向用户展示参数以供审核。）
4. 总结指标、artifact ID 和后续步骤。
