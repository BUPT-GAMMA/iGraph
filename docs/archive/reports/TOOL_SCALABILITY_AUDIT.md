# 工具可扩展性审计报告

> 审计目标：识别所有工具在超大图（节点数 > 10 万、边数 > 100 万）场景下的时间复杂度、空间复杂度和输出规模风险。
>
> **当前状态**：所有 P0/P1 输出规模风险已在 `compute.py` 中修复，通过全局截断常量统一管控。

---

## 一、输出截断常量（`compute.py`）

| 常量 | 值 | 适用场景 |
|------|----|---------|
| `_MAX_LIST_ITEMS` | 50 | 节点/边列表最大条数（通用上限） |
| `_MAX_PATHS` | 20 | 路径列表最大条数 |
| `_MAX_COMMUNITIES_SHOWN` | 20 | 最多展示的社区数 |
| `_MAX_NODES_PER_COMMUNITY` | 10 | 每个社区节点预览数 |
| `_MAX_COMPONENTS_SHOWN` | 10 | 最多展示的连通分量数 |
| `_MAX_NODES_PER_COMPONENT` | 20 | 每个分量节点预览数 |
| `_MAX_CYCLES_DISPLAY` | 20 | 最多展示的环数 |

所有受限字段均附带 `_truncated: bool` 和 `_total: int` 字段，调用方可判断是否需要进一步查询。

---

## 二、全工具复杂度 & 风险总表

### 数据操作类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `load_graph` | O(E) | 加载状态、版本号 | O(1) | ⬜ 无 |
| `load_graph_from_upload` | O(E) | 加载状态、版本号 | O(1) | ⬜ 无 |
| `clear_graph` | O(1) | 成功标志、新版本号 | O(1) | ⬜ 无 |
| `undo_graph` | O(1) | 节点数、边数 | O(1) | ⬜ 无 |
| `to_undirected` | O(V+E) | 摘要 + 指引调用 `get_graph_info` | O(1) | ✅ 已修复 |
| `get_graph_info` | O(V+E) | 摘要 + 前 10 节点 + 前 30 边 | O(1) | ⬜ 无 |
| `list_datasets` | O(D) | 数据集元数据列表 | O(D)，D=数据集数 | ⬜ 无 |
| `load_dataset` | O(V+E) | 图版本、节点数、边数 | O(1) | ⬜ 无 |
| `save_as_dataset` | O(V+E) | 数据集元数据 | O(1) | ⬜ 无 |
| `build_graph_from_text` | O(T) | 抽取三元组列表 | O(T)，T=抽取实体数 | 🟡 低 |
| `upload_document` | O(C) | 文档 ID、分块数 | O(1) | ⬜ 无 |
| `retrieve_context` | O(C·D) | 检索结果 top_k 条 | O(k) | ⬜ 无 |
| `get_job_status` | O(1) | 作业状态字典 | O(1) | ⬜ 无 |
| `cancel_job` | O(1) | 取消状态 | O(1) | ⬜ 无 |

### 节点 / 边查询类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `get_node_info` | O(deg(v)) | 度数、邻居预览（≤50）、关联边预览（≤50） | O(50) | ✅ 已修复 |
| `get_edge_info` | O(1) | 权重、标签、类型 | O(1) | ⬜ 无 |
| `check_edge_exists` | O(1) | 布尔值 | O(1) | ⬜ 无 |

### 路径算法类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `shortest_path`（单 target） | O((V+E)logV) | 一条路径 + 边详情 + `hop_count` | O(path_len) | ⬜ 无 |
| `shortest_path`（target="all"） | O((V+E)logV) | 统计摘要 + 最近 20 条路径 + `truncated` | O(20·path_len) | ✅ 已修复 |
| `shortest_path`（k 条候选） | O(k·(V+E)logV) | k 条路径列表 | O(k·path_len) | 🟡 中 |
| `all_shortest_paths` | O(k·(V+E)) | 最多 20 条路径 + `count_returned` + `truncated` | O(20·path_len) | ✅ 已修复 |
| `maximum_flow` | O(E·max_flow) | `max_flow_value` + 前 50 条非零流边 + `truncated` | O(50) | ✅ 已修复 |
| `minimum_cut` | O(E·max_flow) | 分区大小 + 较小分区预览（≤50）+ 割边预览（≤50） | O(50) | ✅ 已修复 |
| `minimum_spanning_tree` | O(E·logV) | `total_weight` + MST 边预览（≤50）+ `truncated` | O(50) | ✅ 已修复 |

### 全局分析类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `community_detection` | O(V+E)～O((V+E)·iter) | `num_communities` + `size_stats` + 最多 20 个社区（每个 ≤10 节点预览） | O(200) | ✅ 已修复 |
| `node_centrality` | O(V²)～O(V·E) | `value_stats` + top 50 节点 + bottom 10 节点 + `truncated` | O(60) | ✅ 已修复 |
| `graph_connectivity` | O(V+E) | 连通性标志 + 最多 10 个分量（每个 ≤20 节点预览）+ `truncated` | O(200) | ✅ 已修复 |
| `find_cycles` | O(V+E) | 最多 20 个环（硬限制） | O(20·avg_len) | ✅ 有限制 |
| `topological_sort` | O(V+E) | `total_nodes` + 前 50 个节点预览 + `truncated` | O(50) | ✅ 已修复 |
| `degree_analysis` | O(V+E) | 度数统计摘要 + top 50 高度节点 + low 10 节点 + `truncated` | O(60) | ✅ 已修复 |
| `global_metrics` | O(V²)～O(E) | 单个标量指标 | O(1) | ⬜ 无（计算慢） |

### 子图 / 高级分析类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `subgraph_extraction` | O(V+E)～O(V·E) | `num_nodes`/`num_edges` + 节点预览（≤50）+ 边预览（≤50） | O(100) | ✅ 已修复 |
| `path_analysis`（all_simple_paths） | O(paths·V) | 最多 20 条路径 + `truncated` | O(20·path_len) | ✅ 已修复 |
| `path_analysis`（其他模式） | O(V+E) | 平均最短路径长度等标量 | O(1) | ⬜ 无 |
| `link_prediction_score` | O(E'·deg) | top_k 预测边（上限 50，默认 10） | O(50) | ✅ 有限制 |
| `clique_analysis`（find_cliques） | O(3^(V/3)) | 极大团预览（≤50）+ `total_cliques` | O(50) | ✅ 已修复 |
| `clique_analysis`（其他模式） | O(3^(V/3))～O(V) | 标量或 top 50 节点 | O(50) | ✅ 有限制 |
| `reachability`（后代/祖先） | O(V+E) | `total_count` + 节点预览（≤50）+ `truncated` | O(50) | ✅ 已修复 |
| `reachability`（传递闭包） | O(V·(V+E)) | `num_edges` + 边预览（≤50）+ `truncated` | O(50) | ✅ 已修复 |

### GraphML 类

| 工具名 | 时间复杂度 | 返回内容 | 返回大小量级 | 超大图风险 |
|--------|-----------|---------|------------|---------|
| `train` | O(epoch·(V+E)) | 任务提交状态、模型信息 | O(1) | ⬜ 异步，不阻塞 |
| `infer` | O(V+E) | 最多 20 条预测结果（硬限制） | O(20) | ✅ 有限制 |
| `visualize_model_architecture` | O(1) | Mermaid 图文本 | O(1) | ⬜ 无 |

---

## 三、各工具修复详情

### `to_undirected`（已修复）

**原问题**：返回 `list(G.nodes(...))` + `list(G.edges(...))`，无截断。

**当前行为**：不再返回节点/边列表，改为返回摘要信息并提示调用 `get_graph_info`：
```json
{
  "success": true,
  "session_id": "...",
  "graph_version": 3,
  "message": "图已转换为无向图。如需查看结构摘要，请调用 get_graph_info。",
  "num_nodes": 10000,
  "num_edges": 50000
}
```

---

### `get_node_info`（已修复）

**原问题**：邻居/关联边列表大小 O(deg(v))，Hub 节点有风险。

**当前行为**：邻居、前驱、关联边均截断至 50 条：
```json
{
  "neighbors_preview": [...],
  "neighbors_total": 100000,
  "neighbors_truncated": true,
  "predecessors_preview": [...],
  "predecessors_total": 80000,
  "predecessors_truncated": true,
  "incident_edges_preview": [...],
  "incident_edges_total": 180000,
  "incident_edges_truncated": true
}
```

---

### `shortest_path`（target="all"，已修复）

**原问题**：返回所有可达节点的路径，最坏 O(V²)。

**当前行为**：返回统计摘要 + 最近 20 个目标的路径：
```json
{
  "reachable_count": 50000,
  "avg_distance": 3.14,
  "max_distance": 12,
  "min_distance": 1,
  "nearest_paths": [{"target": "B", "distance": 1, "path": ["A", "B"]}, ...],
  "truncated": true
}
```

---

### `all_shortest_paths`（已修复）

**原问题**：`list(nx.all_shortest_paths(...))` 无路径数限制。

**当前行为**：使用生成器，最多返回 20 条路径：
```json
{
  "count_returned": 20,
  "length": 3,
  "paths": [...],
  "truncated": true,
  "note": "最多返回 20 条，实际路径数可能更多"
}
```

---

### `maximum_flow`（已修复）

**原问题**：返回所有非零流边，O(E)。

**当前行为**：按流量降序返回前 50 条边：
```json
{
  "max_flow_value": 42,
  "total_nonzero_edges": 5000,
  "top_flow_edges": [{"from": "A", "to": "B", "flow": 10.0}, ...],
  "truncated": true
}
```

---

### `minimum_cut`（已修复）

**原问题**：返回两个分区节点完整集合 + 所有割边，O(V+E)。

**当前行为**：返回分区大小 + 较小分区预览（≤50）+ 割边预览（≤50）：
```json
{
  "min_cut_value": 3,
  "source_partition_size": 100,
  "sink_partition_size": 49900,
  "smaller_partition_preview": [...],
  "smaller_partition_total": 100,
  "smaller_partition_truncated": true,
  "cut_edges_preview": [...],
  "cut_edges_total": 300,
  "cut_edges_truncated": true
}
```

---

### `minimum_spanning_tree`（已修复）

**原问题**：返回所有 MST 边，O(V)。

**当前行为**：返回 `total_weight` + 按权重排序的前 50 条边预览；不连通图返回 `forest_summary`（每个分量只含规模和权重，不含完整边列表）。

---

### `community_detection`（已修复）

**原问题**：每个社区返回完整节点集，O(V) 输出。

**当前行为**：最多展示 20 个社区，每个社区最多 10 个节点预览：
```json
{
  "num_communities": 500,
  "modularity": 0.72,
  "size_stats": {"mean": 200.0, "max": 5000, "min": 1, "median": 150.0},
  "communities_shown": 20,
  "communities_truncated": true,
  "communities": [{"community_id": 0, "size": 5000, "nodes_preview": [...], "nodes_truncated": true}, ...]
}
```

---

### `node_centrality`（已修复）

**原问题**：返回所有节点中心性排名，O(V)。

**当前行为**：返回统计摘要 + top 50 节点 + bottom 10 节点：
```json
{
  "centrality_type": "betweenness",
  "total_nodes": 100000,
  "value_stats": {"mean": 0.0001, "max": 0.85, "min": 0.0, "median": 0.00005},
  "top_nodes": [{"node": "hub_A", "value": 0.85}, ...],
  "bottom_nodes": [...],
  "truncated": true
}
```

---

### `graph_connectivity`（已修复）

**原问题**：返回所有连通分量完整节点集，O(V)。

**当前行为**：最多展示 10 个分量，每个最多 20 个节点预览：
```json
{
  "strongly_connected_components": {
    "count": 5000,
    "largest_size": 95000,
    "components": [{"size": 95000, "nodes_preview": [...], "nodes_truncated": true}, ...],
    "truncated": true
  }
}
```

---

### `topological_sort`（已修复）

**原问题**：返回全量拓扑序列，O(V)。

**当前行为**：返回前 50 个节点预览 + 总数：
```json
{
  "is_dag": true,
  "total_nodes": 100000,
  "topological_order_preview": [...],
  "truncated": true,
  "note": "仅展示前 50 个节点，共 100000 个"
}
```

---

### `degree_analysis`（已修复）

**原问题**：返回所有节点度数字典，O(V)。

**当前行为**：返回度数统计 + top 50 高度节点 + low 10 节点：
```json
{
  "total_nodes": 100000,
  "in_degree_stats": {"mean": 5.0, "max": 50000, "min": 0, "median": 3.0},
  "top_degree_nodes": [{"node": "hub", "in": 50000, "out": 100, "total": 50100}, ...],
  "low_degree_nodes": [...],
  "truncated": true
}
```

---

### `subgraph_extraction`（已修复）

**原问题**：返回子图完整节点 + 边列表，子图可能接近原图大小。

**当前行为**：节点和边均截断至 50 条预览：
```json
{
  "num_nodes": 50000,
  "num_edges": 200000,
  "nodes_preview": [...],
  "nodes_truncated": true,
  "edges_preview": [...],
  "edges_truncated": true
}
```

---

### `path_analysis`（all_simple_paths，已修复）

**原问题**：无路径数限制，可能产生指数级输出。

**当前行为**：最多返回 20 条路径：
```json
{
  "method": "all_simple_paths",
  "count": 20,
  "paths": [...],
  "truncated": true,
  "note": "最多返回 20 条路径"
}
```

---

### `reachability`（已修复）

**原问题**：`descendants`/`ancestors` 返回全量可达节点；`transitive_closure` 原限制 200 条边。

**当前行为**：后代/祖先截断至 50 条预览；传递闭包边预览同样截断至 50：
```json
{
  "method": "descendants",
  "node": "A",
  "total_count": 80000,
  "nodes_preview": [...],
  "truncated": true
}
```

---

## 四、仍需关注的风险

### 计算慢但输出小的工具

这类工具输出已受控，但在超大图上计算时间不可接受，目前没有超时保护：

| 工具名 | 计算复杂度 | 建议 |
|--------|-----------|------|
| `global_metrics`（diameter） | O(V·(V+E)) | V > 10,000 时建议拒绝或估算 |
| `global_metrics`（avg_clustering） | O(V·deg²) | 稠密图极慢，建议采样 |
| `node_centrality`（betweenness） | O(V·E) | V > 50,000 时建议切换为近似算法 |
| `clique_analysis`（find_cliques / clique_number） | O(3^(V/3)) | V > 100 时有超时风险 |

### `shortest_path`（k 条候选）

Yen's K-Shortest 在稠密图中 k 较大时仍可能较慢，但输出大小受 k 控制，不属于输出规模问题。

---

## 五、修复状态总结

| 优先级 | 工具 | 原问题 | 状态 |
|--------|------|--------|------|
| P0 | `to_undirected` | 返回全量节点+边 | ✅ 已修复：不再返回列表 |
| P0 | `all_shortest_paths` | 路径数无上限 | ✅ 已修复：上限 20 |
| P0 | `path_analysis`（all_simple_paths） | 路径数无上限 | ✅ 已修复：上限 20 |
| P0 | `shortest_path`（target="all"） | 返回 O(V²) 数据 | ✅ 已修复：最近 20 条 + 统计摘要 |
| P1 | `subgraph_extraction` | 子图无大小上限 | ✅ 已修复：节点/边各上限 50 |
| P1 | `node_centrality` | 返回全量节点排名 | ✅ 已修复：top 50 + 统计摘要 |
| P1 | `community_detection` | 每社区返回完整节点集 | ✅ 已修复：20 社区 × 10 节点 |
| P2 | `degree_analysis` | 返回全量节点度数 | ✅ 已修复：top 50 + 统计摘要 |
| P2 | `graph_connectivity` | 所有分量完整节点集 | ✅ 已修复：10 分量 × 20 节点 |
| P2 | `maximum_flow` / `minimum_cut` | O(E) / O(V+E) 输出 | ✅ 已修复：各上限 50 |
| P3 | `get_node_info` | Hub 节点邻居列表过大 | ✅ 已修复：上限 50 |
| P3 | `topological_sort` | O(V) 输出 | ✅ 已修复：上限 50 |
| P3 | `reachability` | 全量可达节点集 | ✅ 已修复：上限 50 |
| — | `global_metrics`（diameter/clustering） | 计算复杂度 O(V²) | 🟡 未修复：无超时保护 |
| — | `node_centrality`（betweenness） | 计算复杂度 O(V·E) | 🟡 未修复：大图无近似算法 |
| — | `clique_analysis` | 计算复杂度 O(3^(V/3)) | 🟡 未修复：无超时保护 |
