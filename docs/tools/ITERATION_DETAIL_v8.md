# v8 迭代详情：visualize_subgraph 大图上下文优化

**日期**：2026-05-17  
**状态**：✅ 完成

---

## 背景

`visualize_subgraph` 工具在图节点数量较多时（如数千节点），会将全量节点/边序列化为 JSON 写入 LLM 上下文，导致 token 数量急剧膨胀，超出模型上下文窗口。

之前虽然 `_summarize_visualize_result`（`backend/agent/igraph.py`）会在写入对话历史时做摘要，但根本问题在于 `graph_data_service` 侧的工具返回体本身就已经包含全量数据，无法从源头限制。

---

## 问题分析

| 问题 | 原因 |
|------|------|
| 大图爆上下文 | `visualize_subgraph` 无节点数量上限，全图序列化 |
| 之前的随机采样方案展示效果差 | 随机选取的节点之间无连边，渲染后全是孤立点 |
| `os` 未导入导致 NameError | `compute.py` 使用 `os.environ` 但未 import os |

---

## 修改内容

### `graph_data_service/compute.py`

**新增节点上限常量**
```python
_MAX_VIZ_NODES = int(os.environ.get("VISUALIZE_MAX_NODES", "200"))
```
可通过环境变量 `VISUALIZE_MAX_NODES` 覆盖，默认 200。

**新增 `max_nodes` 参数**

`visualize_subgraph(... , max_nodes: Optional[int] = None)` 支持调用方显式指定上限。

**BFS 连通采样策略**（替代原随机采样）

超出节点上限时，按以下优先级构建连通子图：
1. **Seed 节点**：从 `highlights` 中提取所有高亮节点（路径节点、关键节点、子图节点）作为 BFS 起点，保证高亮内容完整呈现
2. **无高亮时**：自动选择度数最高的节点作为起点（图的"中心"）
3. **BFS 扩展**：逐层向外扩展邻居节点，每层随机 shuffle 避免方向偏移，直到填满 `node_cap`

对比原随机采样：

| | 随机采样 | BFS 连通采样 |
|---|---|---|
| 节点间连边 | 随机，多数无连边 | 保证邻域连通 |
| 高亮节点 | 优先保留但孤立 | 优先保留且连通 |
| 可视化效果 | 大量孤立点 | 连通子图，结构清晰 |

**截断标记**

返回结果新增字段：
```json
{
  "truncated": true,
  "total_nodes": 5000,
  "truncated_to": 200
}
```

**修复 `import os` 缺失**（`NameError: name 'os' is not defined`）

**将 `import random` 移至文件顶部**，避免函数内重复 import。

### `graph_data_service/app.py`

- `_dispatch_graph_tool` 透传 `max_nodes` 参数
- `do_visualize_subgraph` 方法签名增加 `max_nodes` 参数
- `/sessions/{session_id}/graph/visualize` 直接端点同样透传 `max_nodes`

### `graph_data_service/tool_catalog.py`

`visualize_subgraph` 的 `inputSchema` 新增 `max_nodes` 字段：
```json
{
  "max_nodes": {
    "type": "integer",
    "description": "返回的最大节点数，默认 200。超出时优先保留高亮节点，其余随机采样。",
    "default": 200
  }
}
```
LLM 可在调用时显式传入，按需扩大或缩小展示规模。

### `backend/agent/igraph.py`

`_summarize_visualize_result` 增加截断提示，当结果含 `truncated=True` 时，写入上下文的摘要中包含：
```
（图共 5000 个节点，因超出上限已采样展示 200 个，
高亮节点优先保留，其余通过 BFS 邻域扩展；如需更多节点可指定 max_nodes 参数）
```

---

## 涉及文件

| 文件 | 改动类型 |
|------|---------|
| `graph_data_service/compute.py` | 核心逻辑：节点 cap + BFS 采样 + import 修复 |
| `graph_data_service/app.py` | 参数透传 |
| `graph_data_service/tool_catalog.py` | Schema 更新 |
| `backend/agent/igraph.py` | 截断摘要提示 |
