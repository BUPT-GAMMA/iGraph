# GraphML 模块说明（内置于 GraphData）

`graph_ml` 是 `graph_data_service` 的进程内子模块。节点分类、链路预测和预训练直接使用当前会话的单图 `GraphData`；图分类使用独立、会话隔离的多图 `GraphMLDatasetStore`，两种状态互不替代。

## 1. 支持范围

| 任务 | 模型 | 数据和指标 |
|---|---|---|
| `node_classification` | GCN / GAT / SAGE | 当前单图的节点标签 |
| `link_prediction` | GCN / GAT / SAGE | 当前单图的边 |
| `graph_classification` | GCN / GAT / SAGE | HIV：ROC-AUC；PCBA：128 任务宏平均 AP |
| `pretrain` | Node2Vec / GNN 自监督方法 | 当前单图的节点 embedding |

图分类第一版不是新算法，而是统一 baseline：

```text
Categorical Node IDs
→ Node Embedding
→ GCN / GAT / GraphSAGE
→ Global Mean Pool
→ Linear Graph-level Head
→ Sigmoid Probabilities
```

`xe` 边类别会被安全读取、校验和保留，但第一版不会进入模型计算，artifact 中固定记录 `uses_edge_features=false`。暂不支持 GIN/GINE、任意 OGB 数据、可切换 pooling 或自定义 metric/split。

## 2. 图分类调用顺序

多图 `.pt` 不能交给单图工具 `load_graph_from_upload`。调用顺序固定为：

```text
上传并注册 hiv.pt / pcba.pt
→ load_graph_classification_dataset
→ get_job_status（轮询加载作业）
→ train(task="graph_classification")
→ get_job_status（轮询训练作业）
→ infer / visualize_model_architecture
```

加载请求：

```json
{
  "filename": "hiv.pt",
  "dataset_profile": "hiv"
}
```

训练请求：

```json
{
  "task": "graph_classification",
  "model": "GCN",
  "data": {"dataset_id": "gds_0123abcd"},
  "train_config": {
    "epochs": 100,
    "lr": 0.001,
    "weight_decay": 0.0,
    "seed": 42,
    "early_stop_patience": 10,
    "batch_size": 64,
    "num_workers": 0
  },
  "algo_config": {
    "device": "auto",
    "node_embedding_dim": 64,
    "hidden_dim": 64,
    "embedding_dim": 64,
    "num_layers": 2,
    "dropout": 0.2
  }
}
```

图分类不接受 `graph_version`、`feature_source="embedding"`、自定义 split、metric、method 或节点/边目标字段。未显式传 `dataset_id` 时使用当前 session 已绑定的多图数据集。

## 3. 数据安全和生命周期

- 数据文件只能通过当前 `GraphSession` 的上传注册表按文件名解析，不接受任意路径。
- `.pt` 加载前扫描全局类型，反序列化固定使用 `weights_only=True`、`map_location="cpu"` 和 PyG 白名单。
- Loader 严格校验根字段、节点/边类别、标签、词表和 train/valid/test 完整无重叠划分。
- 每个运行时 session 最多绑定一个多图 bundle；服务重启后不会隐式恢复，必须重新调用加载工具。
- `clear_graph` 只清理单图，不影响多图 bundle；删除 session 会释放多图内存。
- 数据加载、训练和推理均在 GraphData 的重型任务限流内执行；MCP/Agent 对数据加载和训练使用异步作业与进度轮询。

`load_graph_classification_dataset` 返回独立 readiness，包括图数、任务数、固定 split、metric、缺失标签率和词表大小；它不会把图分类状态写入单图 `get_graph_info.graph_ml_readiness`。

## 4. 训练与评测语义

- 始终使用文件自带的 train/valid/test，测试集只在最佳验证模型确定后评估。
- HIV：单任务 masked BCE，按 `val_roc_auc` 选择模型。
- PCBA：128 个独立二分类任务，NaN 标签不参与 loss；逐任务计算 AP，跳过全缺失、无正样本或无负样本任务，再对有效任务求宏平均，按 `val_ap` 选择模型。
- 全 NaN batch 会跳过；若整个 train split 没有有效标签则明确失败。
- 相同 seed 会同时设置 Python、NumPy、PyTorch 和 CUDA 随机源。

## 5. 推理

支持完整 split 或指定图索引：

```json
{"model_id": "mdl_graph_classification_xxx", "query": {"split": "test"}}
```

```json
{
  "model_id": "mdl_graph_classification_xxx",
  "query": {"graph_indices": [0, 12, 42]},
  "infer_config": {"result_limit": 20, "result_offset": 0}
}
```

split 指标基于完整集合计算，预测明细才受分页影响。PCBA 默认每张图只返回 `top_k_tasks`；只有 `return_all_task_probs=true` 才返回完整 128 维数组。推理会严格匹配 session 内 bundle 的 dataset fingerprint、任务数、词表、metric 和 split 大小；服务重启后需先重新加载相同数据集。

## 6. Artifact

```text
artifacts/graph_ml/{session_id}/{model_id}/
├── checkpoint.pt
├── meta.json
├── metrics.json
└── per_task_metrics.json
```

图分类 checkpoint 只包含 `{"model": state_dict}`。专用读取器固定 `weights_only=True`，并校验根键、参数键、Tensor 类型、dtype 和 metadata 重建模型的维度；旧 node/link/pretrain artifact loader 的兼容行为不变。

## 7. 工具入口和配置

| 工具 | 说明 |
|---|---|
| `load_graph_classification_dataset` | 异步安全加载并绑定 HIV/PCBA |
| `train` | 训练四类 GraphML 任务；Agent/MCP 默认异步 |
| `infer` | 节点、边、embedding 或整图推理 |
| `visualize_model_architecture` | Mermaid 与结构化层摘要 |
| `get_job_status` | 获取作业状态及增量进度 |

环境变量：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `GRAPH_ML_ARTIFACT_ROOT` | `artifacts/graph_ml` | 模型产物根目录 |
| `GRAPH_ML_MAX_RESIDENT_DATASETS` | `2` | 进程内常驻多图 bundle 上限 |
| `GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS` | `900` | GraphData 重型工具超时 |
| `MCP_HEAVY_TOOL_TIMEOUT_SECONDS` | `900` | Agent 到 MCP 重型工具超时 |

## 8. 测试和真实数据联调

日常测试只使用临时生成的合成 `.pt`。真实测试通过环境变量显式启用：

```bash
GRAPH_ML_TEST_HIV_PT=/path/to/hiv.pt \
GRAPH_ML_TEST_PCBA_PT=/path/to/pcba.pt \
python -m unittest discover -s tests/servers \
  -p 'test_graph_classification_real_data.py' -v
```

真实训练还需显式开启相应开关，避免日常 CI 意外执行大数据训练。详细命令、结果字段和本次验收记录见 [GRAPH_CLASSIFICATION_BENCHMARK.md](../archive/reports/GRAPH_CLASSIFICATION_BENCHMARK.md)。
