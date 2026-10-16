# GraphData JSON 格式说明（按当前实现）

当前系统里有两类“JSON 图文档”，容易混淆：

1. **存储文件格式**：`graph_data.v1`（`graph_data_service/io.py`）
2. **服务交换格式**：`graph/document` API（`graph_data_service/codec.py`）

两者都能表达图，但字段名不完全一致。

---

## 1. 存储文件格式：`graph_data.v1`

来源：`GraphData.save("*.json")` / `GraphData.load("*.json")`

顶层约束：

- `schema` 必须为 `"graph_data.v1"`
- 必填字段：
  - `num_nodes`
  - `edge_index`（shape 必须是 `[2, E]`）

常见可选字段：

- `directed`（默认 true）
- `name`
- `graph_desc`
- `id2node`（key 是字符串化 node_id）
- `edge_weight`
- `edge_names`
- `node_feat`
- `edge_feat`
- `node_label`
- `node_type`
- `edge_label`
- `edge_type`
- `graph_label`

示例：

```json
{
  "schema": "graph_data.v1",
  "num_nodes": 3,
  "directed": true,
  "name": "demo_graph",
  "graph_desc": "A simple directed graph",
  "id2node": {
    "0": "A",
    "1": "B",
    "2": "C"
  },
  "edge_index": [
    [0, 1, 0],
    [1, 2, 2]
  ],
  "edge_weight": [1.0, 2.0, 3.0],
  "node_feat": [[1, 0], [0, 1], [1, 1]],
  "node_label": [0, 1, 0],
  "node_type": ["user", "item", "item"],
  "edge_type": ["follow", "follow", "click"],
  "graph_label": "demo"
}
```

---

## 2. 服务交换格式：`graph/document` 返回

来源：`GET /sessions/{session_id}/graph/document`

由 `graph_data_to_document(...)` 生成，关键字段：

- `schema_version`（当前是 `"1.0"`）
- `num_nodes`
- `directed`
- `name`
- `graph_desc`
- `id2node`
- `node2id`
- `edge_index`
- `edge_names`
- `edge_weight`
- `node_feat`
- `edge_feat`
- `node_label`
- `node_label_stats`（标签可见性统计：`total/visible/missing/partially_labeled`）
- `node_type`
- `edge_label`
- `edge_type`
- `graph_label`

`graph/document` 包裹层结构示例：

```json
{
  "loaded": true,
  "session_id": "demo",
  "graph_version": 3,
  "graph": {
    "schema_version": "1.0",
    "num_nodes": 3,
    "directed": true,
    "name": "demo_graph",
    "graph_desc": "A simple directed graph",
    "id2node": {"0": "A", "1": "B", "2": "C"},
    "node2id": {"A": 0, "B": 1, "C": 2},
    "edge_index": [[0, 1, 0], [1, 2, 2]],
    "edge_weight": [1.0, 2.0, 3.0]
  }
}
```

---

## 3. 关键一致性规则

1. `edge_index` 第一维必须是 2（源节点数组、目标节点数组）。
2. `edge_weight` 长度应等于边数 `E`。
3. `node_feat` 第一维应等于节点数 `N`。
4. `node_label` / `edge_label` 可为：
   - 一维离散标签
   - 二维 one-hot / soft label
5. `id2node` 和 `node2id` 必须互为双射（运行期由 `GraphData` 校验）。

---

## 4. 上传文件加载行为

### 4.1 `load_graph_from_upload_path`（直接文件加载）

GraphData Server 的 `load_graph_from_upload_path(...)` 支持：

- `.json`：先尝试 `graph_data.v1` schema 加载；若格式不匹配（ValueError/KeyError），fallback 到文本边列表解析
- `.npz`：按 numpy 存储格式加载（强制要求 schema）
- 其他文本（`.txt` / `.edgelist` / `.dat` / `.csv`）：按边列表文本解析（每行 `src dst [weight]`）

说明：

- `.json/.npz` 在 `append=true` 时会先反序列化，再转文本合并。
- 路径解析包含多重兜底（相对路径、项目根 uploads、`API_UPLOAD_DIR` / `GRAPH_UPLOAD_DIR` / `GRAPH_DATA_UPLOAD_DIR` 环境变量目录）。

### 4.2 CSV / Excel 上传加载

CSV / Excel 文件不再通过独立的表格建图工具推断字段映射；使用方需先指定输入格式，再通过上传加载流程导入：

- `.csv` 可按边列表文本解析（每行 `src dst [weight]`）
- 其他表格格式应先转换为系统支持的图输入格式
- 统一通过 `load_graph_from_upload` 写入 `GraphSession`

### 4.3 `build_graph_from_text`（文本建图）

通过 `data_pipeline/text_extractor.py` 从自然语言文本抽取三元组并建图：

- 启发式抽取：匹配 50+ 种中英文关系模式（如"A 认识 B"、"A works at B"、"A → B"）
- 可选 LLM 辅助抽取（当前 GraphDataService 未注入 LLM，仅走启发式）
- 输出三元组列表，由 `GraphBuilder.build_from_triples()` 转为 `GraphData`

### 4.4 图快照格式

`GraphSession` 的快照持久化使用 `.npz` 格式：

```
uploads/graph_data/{session_id}/
├── graph_current.npz       # 当前图快照（服务启动时自动恢复）
├── session_meta.json       # 会话元数据（node_count/edge_count/is_directed/has_weights/graph_version）
└── snapshots/
    ├── v001.npz            # 历史版本（最多保留 5 个，自动轮转）
    ├── v002.npz
    └── ...
```

`undo_graph` 工具从 snapshots 目录加载倒数第二个版本恢复。

---

## 5. 与算法/训练层的关系

1. `GraphCompute` 图算法在 GraphData Service 内部直接访问 `GraphData` 对象（通过 `resolve_graph()` 构建 NetworkX 缓存），**不依赖** `graph/document` 端点。
2. `GraphMLModule` 训练/推理同样直接读取会话内存中的 `GraphData`，**不跨进程传输图文档**。
3. `graph/document` 端点仍保留，供外部调试和 CLI 模式使用。
4. `graph_version` 用于 NetworkX 缓存失效与 GraphML 模型图版本一致性校验。

---

## 6. 与数据集层的关系

`DatasetRegistry`（`graph_data_service/dataset_registry.py`）使用 `.npz` 格式持久化数据集：

```
datasets/{dataset_id}/
├── meta.json               # 数据集元数据（name/description/source_type/node_count/edge_count/...）
├── graph.npz               # 当前版本图数据
└── versions/
    ├── v001.npz            # 历史版本
    └── v002.npz
```

工具 `save_as_dataset` 将当前 `GraphSession` 的 `GraphData` 保存为数据集；`load_dataset` 从数据集加载图到会话。

---

## 7. 何时用哪种格式

| 场景 | 格式 | 说明 |
|------|------|------|
| 长期存盘 / 人工编辑 | `graph_data.v1` JSON | `schema` 字段标识，人类可读 |
| 高效二进制存储 | `.npz` | 快照 / 数据集 / 大图持久化 |
| 外部调试 / CLI | `graph/document` API | `schema_version` 文档，含完整图数据 |
| 表格导入 | CSV / Excel | 指定输入格式后，通过 `load_graph_from_upload` 加载 |
| 文本导入 | 自然语言文本 | 通过 `build_graph_from_text` 三元组抽取 |
