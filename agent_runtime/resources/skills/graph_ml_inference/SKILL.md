---
name: graph_ml_inference
description: GraphML 推理参数构建、model_id 验证与结果解释流程。
metadata:
  version: 1.2.0
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

## `query` 精确格式
- 节点分类：`{"nodes":["0","1","2"]}`，或使用 `split`。
- 链路预测指定边：`{"edges":[["0","8"],["0","1"],["1","344"]]}`。每条边必须是长度为 2 的字符串数组；不要使用 `{"source":"0","target":"8"}` 对象。
- 链路预测按源节点：`{"source_node":"0","candidate_nodes":["8","1"],"topk":10}`。
- 图分类：`{"graph_indices":[0,1,2]}`，索引必须是整数。
- MCP 运行时可能把整个 `query` 编码为 JSON 字符串；这是允许的，但字符串解码后的内部结构仍必须符合上述格式。

## 操作步骤
1. 确认图状态和推理目标。
2. 根据用户请求和上面的精确格式构建完整的 `infer` 参数集，包含所有适用的默认参数。
3. 调用 `infer`。推理是只读操作，验证参数后直接执行，不重复请求用户确认；必须保留用户指定的 `query`，不得在表示形式报错后改为空查询重试。
4. 解释推理结果及其边界。
