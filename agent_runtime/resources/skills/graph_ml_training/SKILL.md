---
name: graph_ml_training
description: GraphML 训练参数构建、工具 schema 展开与训练后指标总结流程。
metadata:
  version: 1.1.0
  domain: graph_ml
  risk_level: medium
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
3. 调用一次 `train`。用户发出的训练请求即为本次任务授权，不要在 epoch、轮询或评估阶段重复请求确认，也不要重复提交同一训练任务。参数校验失败时不得通过删掉用户指定配置来重试。
4. `train` 返回 `job_id` 后，只对该作业调用 `get_job_status`，直到 `completed`、`failed` 或 `cancelled`；已经结束的作业不要调用 `cancel_job`。
5. 总结指标、model/artifact ID 和后续步骤。
