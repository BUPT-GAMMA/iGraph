# GraphML 模块说明（内置于 GraphData）

`graph_ml` 是 `graph_data_service` 的内置子模块（与 `GraphCompute` 并列），不作为独立 HTTP 服务部署。

## 1. 模块职责

1. 执行 `train` / `infer`（GCN / GAT / SAGE 三种 backbone）。
2. 提供 `visualize_model_architecture`；训练/推理参数 schema 直接由 `train` / `infer` 工具暴露。
3. 使用 `GRAPH_ML_ARTIFACT_ROOT` 管理模型产物（权重 + meta + embedding）。

## 2. 关键设计

1. 不通过 `graph_document` 拉取图——训练/推理直接读取 `GraphDataService` 会话内存中的 `GraphData` 与 `graph_version`。
2. MCP 不连接独立 GraphML 服务，统一转发到 GraphData 的工具端点。
3. GraphML 工具是**可选能力**——PyTorch/PyG 未安装时工具仍可注册到 MCP，但调用时返回安装提示错误，不影响其他工具。
4. `train` / `infer` 在 `GraphDataService.dispatch_tool()` 中通过 `asyncio.to_thread()` 异步执行，避免阻塞事件循环。

## 3. 支持的任务与模型

| 任务 | 说明 | 支持的 backbone |
|------|------|----------------|
| `node_classification` | 节点分类 | GCN / GAT / SAGE |
| `link_prediction` | 链接预测 | GCN / GAT / SAGE |
| `pretrain` | 预训练嵌入 | Node2Vec（无需 GNN）/ contrastive_learning / mask_autoencoding |

预训练产生的嵌入可作为下游任务的输入特征（`feature_source="embedding"` + `upstream_model_id`）。

## 4. 调用入口

统一通过 GraphData 工具端点调用：

| 端点 | 功能 |
|------|------|
| `POST /sessions/{sid}/tools/train` | 训练 GNN 模型 |
| `POST /sessions/{sid}/tools/infer` | 模型推理 |
| `POST /sessions/{sid}/tools/visualize_model_architecture` | 模型结构可视化（Mermaid + 层级摘要） |

请求体格式：

```json
{
  "arguments": {
    "task": "node_classification",
    "model": "GCN"
  }
}
```

## 5. 模型产物存储

```
artifacts/graph_ml/
└── {session_id}/
    └── mdl_{task}_{timestamp}_{hex}/
        ├── meta.json           # 模型元信息（task / graph_version / feature_source / metrics 等）
        ├── model.pt            # PyTorch 模型权重
        ├── node_names.json     # 训练时节点名列表
        └── embedding.npy       # 节点嵌入（pretrain 产出）
```

`model_id` 格式：`mdl_{task}_{unix_timestamp}_{6_hex_chars}`

`resolve_model()` 支持跨 session 查找 model_id（用于上游 embedding 模型复用）。

## 6. 与 Planner 的集成

`algorithm_catalog.py` 的 `TASK_TYPE_MAP` 包含 ML 任务类型：

```python
"ml_node_classification": ["train", "infer"],
"ml_link_prediction":     ["train", "infer"],
"ml_embedding":           ["pre_train"],
```

Planner 可将 ML 步骤纳入分析计划（如"先训练节点嵌入，再用嵌入辅助社区检测"）。当前 Planner 的 `_fallback_plan` 识别 `train` / `embedding` 等关键词时会自动生成 ML 步骤。

## 7. Skill 约束

当 MCP 工具集包含 `train` / `infer` 时，GraphML training/inference Skill 自动激活，流程基于工具 schema 起草参数、展示给用户确认或修改，再执行训练/推理。

## 8. 运行与配置

启动服务：

```bash
python -m graph_data_service        # GraphML 随 GraphData 自动可用
python -m mcp_service streamable-http
```

环境变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GRAPH_ML_ARTIFACT_ROOT` | 模型产物存储目录 | `artifacts/graph_ml` |
| `MCP_ENABLE_GRAPH_ML_TOOLS` | 强制启用/禁用 ML 工具暴露 | 自动探测 |

已移除的独立部署变量：`GRAPH_ML_HOST` / `GRAPH_ML_PORT` / `GRAPH_ML_URL` / `GRAPH_ML_SERVER_URL`

## 9. 已知限制

1. 模型产物的 `meta.json` 尚未包含 `dataset_id` / `dataset_version` 字段，跨数据集版本的模型一致性校验需后续补充。
2. `JobManager` 异步作业系统已实现骨架，但 `train` / `infer` 尚未接入（当前通过 `asyncio.to_thread` 同步执行），进度回调和前端进度条待后续开发。
3. Planner 的 ML 步骤编排目前仅通过关键词触发，尚未实现自动检查已有模型产物并跳过训练的智能逻辑。
