---
name: graph_ml_inference
description: GraphML 推理参数构建、model_id 验证与结果解释流程。
metadata:
  version: 1.0.0
  domain: graph_ml
  risk_level: medium
  igraph:
    requires_tools: [infer]
    any_tools: [infer, visualize_model_architecture]
    requires_graph_state: loaded
    activates_on_intents: [graph_ml_infer]
---

# GraphML 推理

## 必要检查
- 依据 `infer` 工具 schema 准备参数。
- 验证 `model_id` 与任务的兼容性。
- 尽可能从 schema 展开完整的 `infer` 参数：即使用户未要求修改，也应显式列出所有适用字段及其默认值。

## 操作步骤
1. 确认图状态和推理目标。
2. 根据用户请求和工具 schema 构建完整的 `infer` 参数集，包含所有适用的默认参数。
3. 调用 `infer`。推理是只读操作，验证参数后直接执行，不重复请求用户确认。
4. 解释推理结果及其边界。
