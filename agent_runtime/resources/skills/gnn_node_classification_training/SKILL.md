---
name: gnn_node_classification_training
description: GCN/图神经网络节点分类训练全流程，含环境失败时的邻居多数投票降级方案
metadata:
  version: 1.1.0
  domain: graph_ml
  risk_level: medium
  igraph:
    any_tools: [get_graph_info, get_train_config, train, infer, get_node_info, tool_select]
    requires_graph_state: weighted
    activates_on_intents: [训练GCN, train GCN, 图神经网络, 节点分类训练, GCN训练, GAT训练, SAGE训练, train node_classification, 性别预测, infer, model_id]
---

# GCN/图神经网络节点分类训练全流程（含环境失败降级）

## When to Use
当用户需要在图上训练 GCN、GAT 或 GraphSAGE 等图神经网络模型以完成节点分类任务时触发。适用于节点带有特征向量和标签的图，典型场景包括用户属性预测（如性别、年龄段）、社区角色分类等。当训练环境不可用（如缺少 torch）时，自动降级为邻居多数投票方法提供近似结果。

## Procedure

1. **核验前提条件**：调用 `get_graph_info`，确认以下字段满足要求：
   - `has_node_feat = true`
   - `has_node_label = true`
   - `graph_ml_readiness.node_classification.supported = true`
   - 记录节点数、边数、特征维度、已标注节点数、标签类别列表。
   - 若上述任一条件不满足，立即告知用户缺失内容，终止流程。

2. **获取训练配置模板**：调用 `get_train_config(task=node_classification, model=GCN)`（或 GAT/SAGE），获取默认参数模板，包括：
   - `split`（train/val/test 比例）
   - `epochs`、`lr`、`hidden_dim`、`num_layers`、`dropout`
   - 将模板参数展示给用户，允许用户覆盖任意字段。

3. **启动训练**：调用 `train`，参数示例：
   ```json
   {
     "task": "node_classification",
     "model": "GCN",
     "data": { "feature_source": "graph" },
     "train_config": { <来自步骤2的参数> },
     "algo_config": { <用户自定义或默认> }
   }
   ```
   > `train` 只调用一次，并异步返回 `job_id`。保留用户明确指定的 `epochs`、`hidden_dim` 等参数；参数错误时不得省略配置后重试，以免意外退回默认训练配置。

4. **轮询异步作业**：使用 `get_job_status(job_id=<job_id>)` 查询同一个作业，直到进入 `completed`、`failed` 或 `cancelled` 终态。轮询不得再次调用 `train`；已进入终态的作业不得再调用 `cancel_job`。

5. **分支处理训练结果**：

   **4a — 训练成功**：
   - 记录返回的 `model_id` 及各项 metrics。
   - 调用 `infer(model_id=<model_id>, query={nodes: [目标节点列表]})`，获取每个节点的 `pred_label` 和 `probabilities`。
   - 汇总并向用户展示预测结果。

   **4b — 训练失败（环境问题）**：
   - 明确说明失败原因为**服务端环境问题**（如 `No module named torch`），而非数据问题，用户数据完全正常。
   - 输出可直接复用的完整配置模板（JSON 格式），方便用户在本地或其他环境重试。
   - **降级为邻居多数投票**：
     1. 对目标节点调用 `get_node_info` 获取其全部邻居列表。
     2. 对所有邻居**批量调用** `get_node_info`，收集已标注邻居的标签。
     3. 执行多数投票，统计并报告：有效票数 / 总邻居数 / 各标签票数 / 一致性比例。
     4. 将投票结果作为近似预测输出。

6. **动态工具加载**：若任意步骤所需工具不在当前上下文中，先调用 `tool_select(query=<工具功能描述>, top_k=4)` 动态加载后再执行。

7. **双方法交叉验证**（可选）：若 GCN 推理结果与邻居投票结论一致，向用户说明两种方法互相印证，预测可信度显著提高；若结论不一致，说明可能原因（如标注稀疏、图结构异质性）并建议进一步分析。

## Tool Sequence Patterns

**正常路径**：
`get_graph_info` → `get_train_config` → `train`（一次）→ `get_job_status`（轮询同一 job）→ `infer`

**降级路径**：
`get_graph_info` → `get_train_config` → `train`（失败）→ `get_node_info`（目标节点）→ `get_node_info`（邻居批量）

**工具未加载时插入**：
`tool_select` → `<目标工具>`

## Verification

- **前提核验**：`get_graph_info` 返回中 `has_node_feat`、`has_node_label`、`graph_ml_readiness.node_classification.supported` 均为 `true`。
- **训练质量**：`val_acc` 应高于随机基线（`1 / 标签类别数`），`best_epoch` 不应为第 1 轮（否则提示可能存在数据泄露或配置异常）。
- **推理完整性**：`infer` 返回结果中每个目标节点均有 `pred_label` 和 `probabilities`，概率之和约为 1.0。
- **降级可靠性**：邻居投票有效票数 / 总邻居数 ≥ 0.3 时结果具有参考价值；低于此阈值应提示标注稀疏，结果仅供参考。
- **双方法一致性**：两种方法预测标签相同时，向用户明确标注"高可信度"。

## Notes

- `train` 为异步提交接口；只提交一次，并对返回的同一 `job_id` 调用 `get_job_status`。
- MCP 客户端可能把 `data`、`train_config`、`algo_config`、`query`、`infer_config` 序列化成 JSON 字符串；服务端兼容两种表示，Agent 不应因表示形式报错而删掉用户参数重试。
- 训练失败时务必区分"环境问题"与"数据问题"，避免误导用户认为图数据有误。
- 邻居投票降级方案在图结构稀疏或邻居标注率极低时效果有限，应在报告中明确说明局限性。
- GAT 和 GraphSAGE 的调用方式与 GCN 完全一致，仅需修改 `model` 字段。
- 若图为无向图，邻居投票天然包含双向邻居；若为有向图，建议同时考虑入边和出边邻居以提高覆盖率。
