# iGraph 工具文档

本文档汇总当前 Agent 系统支持的工具。工具来源包括 GraphDataService 公开图工具、GraphML 工具，以及 iGraph 注入的虚拟工具。除"内部会话工具"外，其他工具均可被 Agent 在推理过程中使用。

## 工具与算法总览

| 分类 | 工具数 | 算法数 |
|------|--------|--------|
| 数据与图状态管理 | 13 | 13 |
| 图可视化 | 1 | 1 |
| 节点、边与实体搜索 | 5 | 5 |
| 路径、流与生成树 | 5 | 7 |
| 全局结构分析 | 7 | 27 |
| 子图与高级分析 | 5 | 18 |
| 图机器学习 | 4 | 16 |
| 作业管理 | 2 | 2 |
| Agent 虚拟工具 | 10 | 10 |
| 内部会话工具（不对 LLM 暴露） | 2 | 2 |
| **合计** | **54** | **101** |

> **算法计数规则**：一个工具下若提供多个可选方法（method / algorithm / centrality_type / metric 等枚举参数），每个枚举值计为一个独立算法。`shortest_path` 按 BFS / Dijkstra / Yen-K 计 3 种；`train` 按 GCN/GAT/SAGE×node_classification + GCN/GAT/SAGE×link_prediction + GCN/GAT/SAGE×graph_classification + Node2Vec pretrain 计 10 种；`infer` 按 4 种任务类型计 4 种。

## 复杂度符号

- `V`: 当前图节点数。
- `E`: 当前图边数。
- `deg(v)`: 节点 `v` 的关联边规模。
- `P`: 枚举得到的路径数。
- `K`: 用户请求或系统截断后的返回条数。
- `D`: 已注册数据集数量。
- `C`: RAG 文档分块数量。
- `L`: 输入文本长度或上传文档长度。
- `S`: skill 数量。
- `M`: 记忆条目数量。
- `N_tool`: Agent 可见工具数量。
- `dim`: embedding 或模型隐藏向量维度。
- `epoch`: GraphML 训练轮数。
- 图计算工具首次调用通常会把当前 `GraphData` 转为 NetworkX 图，额外产生一次 `O(V+E)` 时间和空间成本；同一图版本后续调用会复用缓存。
- 大多数图分析工具会截断大列表输出：通用节点/边预览最多 50 条，路径最多 20 条，社区最多 20 个、每个社区最多 10 个节点，连通分量最多 10 个、每个分量最多 20 个节点。

## 工具分类概览

- 数据与图状态管理（13）：`load_graph`、`load_graph_from_upload`、`clear_graph`、`undo_graph`、`to_undirected`、`get_graph_info`、`list_datasets`、`load_dataset`、`save_as_dataset`、`load_graph_from_text`、`load_graph_from_triples`、`upload_document`、`retrieve_context`
- 图可视化（1）：`visualize_subgraph`
- 节点、边与实体搜索（5）：`get_node_info`、`get_edge_info`、`check_edge_exists`、`search_node`、`search_edge`
- 路径、流与生成树（5，7 算法）：`shortest_path`、`all_shortest_paths`、`maximum_flow`、`minimum_cut`、`minimum_spanning_tree`
- 全局结构分析（7，27 算法）：`community_detection`、`node_centrality`、`graph_connectivity`、`find_cycles`、`topological_sort`、`degree_analysis`、`global_metrics`
- 子图与高级分析（5，18 算法）：`subgraph_extraction`、`link_prediction_score`、`path_analysis`、`clique_analysis`、`reachability`
- 图机器学习（4，16 算法）：`load_graph_classification_dataset`、`train`、`infer`、`visualize_model_architecture`
- 作业管理（2）：`get_job_status`、`cancel_job`
- Agent 虚拟工具（10）：`tool_select`、`tool_expand`、`request_user_feedback`、`skills_list`、`skill_view`、`skill_manage`、`memory_read`、`memory_write`、`memory_update`、`memory_delete`
- 内部会话工具（2，不对 LLM 暴露）：`register_session`、`delete_graph_session`

## 逐个工具说明

### 数据与图状态管理

#### `load_graph`

- 功能：从边列表文本加载图到当前会话。每行格式为 `src dst [weight]`；`append=false` 覆盖当前图，`append=true` 追加到当前图。
- 时间复杂度：`O(L+E_in)`，其中 `E_in` 是输入边数。
- 空间复杂度：`O(V_in+E_in)`，保存解析后的图结构。
- 返回内容：`success`、`session_id`、`graph_version`、提示信息。图规模和预览需继续调用 `get_graph_info`。

#### `load_graph_from_upload`

- 功能：从当前会话已注册上传文件加载图，自动检测格式并路由：二进制/JSON 图文件（.npz/.pt/.json）直接加载；边列表或三元组文本直接解析；自然语言文本走 LLM 异步抽取并返回 `job_id`。支持覆盖或追加。
- 时间复杂度：`O(file_size+E_in)`。
- 空间复杂度：`O(V_in+E_in)`。
- 返回内容：同步格式返回 `success`、`session_id`、`graph_version`、`filename`、检测格式；异步格式返回 `async=true`、`job_id`、`status=queued`。

#### `clear_graph`

- 功能：清空当前会话图数据。
- 时间复杂度：`O(1)` 到 `O(V+E)`，取决于底层对象释放成本。
- 空间复杂度：`O(1)`，释放当前图引用。
- 返回内容：`success`、`session_id`、新的 `graph_version`、提示信息。

#### `undo_graph`

- 功能：回滚到上一版本图快照。
- 时间复杂度：通常 `O(V+E)`，需要恢复上一版图对象。
- 空间复杂度：`O(V+E)`。
- 返回内容：成功时返回 `success`、`session_id`、`graph_version`、`num_nodes`、`num_edges`；无历史版本时返回错误。

#### `to_undirected`

- 功能：将当前有向图转换为无向图。
- 时间复杂度：`O(V+E)`。
- 空间复杂度：`O(V+E)`。
- 返回内容：`success`、`session_id`、`graph_version`、`num_nodes`、`num_edges`、提示信息；若已是无向图或未加载图则返回失败原因。

#### `get_graph_info`

- 功能：查询当前图全局摘要、节点/边预览、标签/类型/特征元信息和 GraphML 可行性。
- 时间复杂度：`O(V+E)`，会扫描图以生成摘要、标签统计和预览。
- 空间复杂度：`O(V_label+T+R)`，主要来自标签/类型去重统计和固定规模预览；返回预览是常量级。
- 返回内容：`loaded`、`session_id`、`graph_version`、`num_nodes`、`num_edges`、`directed`、节点/边预览、`graph_info`、`graph_ml_readiness`、截断标记。

#### `list_datasets`

- 功能：列出本地已注册数据集。
- 时间复杂度：`O(D)`。
- 空间复杂度：`O(D)`。
- 返回内容：`success` 和 `datasets`，每个数据集包含 ID、名称、描述、节点数、边数、方向性、创建/更新时间和版本列表。

#### `load_dataset`

- 功能：从数据集 registry 加载指定数据集和版本到当前会话。
- 时间复杂度：`O(V+E)`。
- 空间复杂度：`O(V+E)`。
- 返回内容：`success`、`session_id`、`dataset_id`、`dataset_version`、`graph_version`、`num_nodes`、`num_edges`。

#### `save_as_dataset`

- 功能：把当前会话图保存为一个新数据集，并生成初始版本。
- 时间复杂度：`O(V+E)`。
- 空间复杂度：`O(V+E)` 磁盘写入；内存额外开销取决于序列化实现。
- 返回内容：`success` 和 `dataset` 元数据；无图时返回错误。

#### `load_graph_from_text`

- 功能：从自然语言文本抽取实体关系三元组，并构建图。合并后的实现支持长文本分块、逐块 LLM 抽取、去重、规范化、`append` 追加和 `extraction_goal` 抽取目标约束。可传入内联文本（`text`）或已上传文件名（`filename`）之一。
- 时间复杂度：`O(L)` 加 LLM 抽取成本；构图部分为 `O(T)`，`T` 为抽取三元组数。
- 空间复杂度：`O(T)`。
- 返回内容：`success`、`session_id`、`graph_version`、处理分块数、原始/去重三元组数、`num_nodes`、`num_edges`、采样三元组和提示；不返回完整中间三元组。

#### `load_graph_from_triples`

- 功能：从结构化实体关系三元组构建图并加载到当前会话。`relation` 会写入边类型，`evidence` 写入边文本，`confidence` 写入边特征；支持覆盖或追加。
- 时间复杂度：`O(T)`，`T` 为三元组数量；追加到已有图时还需要合并当前图，整体约 `O(V+E+T)`。
- 空间复杂度：`O(V_T+E_T)`；追加时为 `O(V+E+V_T+E_T)`。
- 返回内容：`success`、`session_id`、`graph_version`、`num_nodes`、`num_edges`、`directed`、`raw_triples`、`valid_triples`、`materialized_edges`、`skipped`、`sample_triples` 和提示信息。

#### `upload_document`

- 功能：把上传文件或直接传入的文本加入当前会话轻量知识库。
- 时间复杂度：`O(L + C*dim)`，当前哈希向量维度 `dim=384`，整体近似 `O(L+C)`。
- 空间复杂度：`O(C*dim + L)`，保存分块、向量和原文。
- 返回内容：`success`、`doc_id`、`chunks`。

#### `retrieve_context`

- 功能：在当前会话已上传文档中做轻量语义检索。
- 时间复杂度：`O(C*dim + C log C)`，当前实现计算全部分块相似度并排序。
- 空间复杂度：`O(C)` 用于分数和索引。
- 返回内容：`success` 和 `chunks`，每个分块包含 `chunk_id`、`doc_id`、`offset`、`text`、`score`。

### 图可视化

#### `visualize_subgraph`

- 功能：获取图的可视化数据，用于在前端渲染图结构。支持指定节点子集、高亮路径/节点/子图；超出 `max_nodes` 时优先保留高亮节点，其余随机采样；支持附带社区信息。Agent 在回答路径、子图、中心节点等问题后，可同步调用此工具生成可视化。
- 算法数：1
- 时间复杂度：`O(V_sub+E_sub)` 用于子图构建和采样；附加社区检测约 `O((V+E)*iter)`。
- 空间复杂度：`O(V_sub+E_sub)`，按 `max_nodes` 截断后返回。
- 返回内容：`nodes`（含坐标、标签、类型）、`edges`、`highlights`、`truncated`、`total_nodes`、`truncated_to`。

### 节点、边与实体搜索

#### `get_node_info`

- 功能：查询节点 ID/名称、标签、类型、文本、入度、出度、邻居、前驱和关联边。
- 时间复杂度：`O(deg(v))`。
- 空间复杂度：`O(deg(v))` 生成完整邻居/关联边后截断返回。
- 返回内容：`success`、`node`、`node_id`、`directed`、`label`、`type`、`text`、度数、邻居/前驱/关联边预览、总数和截断标记。

#### `get_edge_info`

- 功能：查询指定有向边是否存在及其属性。
- 时间复杂度：平均 `O(1)`。
- 空间复杂度：`O(1)`。
- 返回内容：存在时返回 `exists=true`、`src`、`dst`、`weight`、`edge_name`、`edge_label`、`edge_type`；不存在时返回 `exists=false` 和提示。

#### `check_edge_exists`

- 功能：检查指定有向边是否存在。
- 时间复杂度：平均 `O(1)`。
- 空间复杂度：`O(1)`。
- 返回内容：`success`、`src`、`dst`、`exists`、`directed`。

#### `search_node`

- 功能：根据节点名称、文本内容、自然语言描述、节点类型、标签、入度/出度/总度范围搜索节点，返回可继续传给 `get_node_info`、`shortest_path`、`subgraph_extraction` 等工具的精确 `node_name`。
- 时间复杂度：首次搜索需要构建实体索引，约 `O(V+E + (V+E)*embed_cost)`；同一图版本复用索引后，精确命中平均 `O(1)`，语义搜索约 `O(V*dim + V log V)`，过滤会额外扫描候选节点。
- 空间复杂度：索引约 `O((V+E)*dim + V+E)`；单次返回最多 50 个候选。
- 返回内容：`success`、`query`、`semantic_enabled`、`retrieval_mode`、`rerank_enabled`、`best`、`candidates`、`ambiguous`；候选包含 `node_name`、`node_id`、`score`、类型/标签、度数、邻居预览、证据和文本预览。

#### `search_edge`

- 功能：根据边名称、边文本、关系描述、起点/终点描述、边类型、标签、权重范围搜索边，返回可继续传给 `get_edge_info` 或路径类工具的精确 `src_name` / `dst_name`。
- 时间复杂度：首次搜索同 `search_node` 需要构建索引；复用索引后，仅边 query 约 `O(E*dim + E log E)`；带 `src_query` / `dst_query` 时还会搜索端点候选并扫描边，约 `O((V+E)*dim + E)`。
- 空间复杂度：索引约 `O((V+E)*dim + V+E)`；单次返回最多 50 个候选。
- 返回内容：`success`、`query`、`src_query`、`dst_query`、`semantic_enabled`、`retrieval_mode`、`best`、`candidates`、`ambiguous`；候选包含 `src_name`、`dst_name`、`edge_idx`、`edge_name`、`edge_type`、`label`、`weight`、端点摘要、证据和文本预览。

### 路径、流与生成树

#### `shortest_path`

- 功能：计算 `source` 到 `target` 的最短路径；`target="all"` 时计算单源到所有可达点；`algorithm="yen_k_shortest"` 时返回前 `k` 条最短简单路径。
- 算法数：3（BFS 跳数最短路、Dijkstra 加权最短路、Yen-K 最短简单路径）
- 时间复杂度：BFS 单目标约 `O(V+E)`；Dijkstra 单目标或 `target="all"` 约 `O((V+E)log V)`；Yen-K 近似 `O(k*(V+E)log V)`。
- 空间复杂度：单目标 `O(V)`；`target="all"` 为 `O(V+E_path)`；K 最短路径为 `O(k*path_len)` 加内部候选结构。
- 返回内容：单目标返回 `path`、`hop_count`、`total_length`、路径边；`target="all"` 返回可达统计和最近 20 条路径；K 最短路径返回 `paths`、`k`、`weighted`。

#### `all_shortest_paths`

- 功能：计算两个节点之间所有等长最短路径，最多返回 20 条。
- 算法数：1
- 时间复杂度：`O((V+E) + P*path_len)`，其中 `P` 最多计算到返回截断为止。
- 空间复杂度：`O(20*path_len)` 返回路径，加最短路径算法内部空间。
- 返回内容：`success`、`count_returned`、`length`、`paths`、`truncated`、可选 `note`。

#### `maximum_flow`

- 功能：计算源点到汇点最大流，边权作为容量。
- 算法数：1
- 时间复杂度：取决于 NetworkX 最大流实现，常见界可按 `O(V*E^2)` 估计；实际与容量和算法选择有关。
- 空间复杂度：`O(V+E)`，会复制一份容量图并保存流字典。
- 返回内容：`success`、`max_flow_value`、`total_nonzero_edges`、前 50 条非零流边、`truncated`。

#### `minimum_cut`

- 功能：计算源点到汇点最小割，边权作为容量。
- 算法数：1
- 时间复杂度：与最大流同阶，常见估计 `O(V*E^2)`。
- 空间复杂度：`O(V+E)`。
- 返回内容：`success`、`min_cut_value`、源/汇分区大小、较小分区预览、割边预览和截断标记。

#### `minimum_spanning_tree`

- 功能：计算无向视图上的最小生成树；非连通图返回最小生成森林摘要。
- 算法数：1
- 时间复杂度：`O(E log V)`。
- 空间复杂度：`O(V+E)`，包括无向图和 MST/森林。
- 返回内容：连通图返回 `total_weight`、`num_edges`、`mst_edges_preview`、`truncated`；非连通图返回 `num_components`、`total_weight`、`forest_summary`。

### 全局结构分析

#### `community_detection`

- 功能：执行社区检测，支持 7 种算法。
- 算法数：7（`louvain`、`greedy_modularity`、`label_propagation`、`label_propagation_asyn`、`leiden`、`girvan_newman`、`asyn_fluidc`）
- 时间复杂度：标签传播约 `O(V+E)` 到多轮迭代；Louvain/Leiden/贪心模块度约 `O((V+E)*iter)`；Girvan-Newman 通常可达 `O(V*E^2)`，适合小图。
- 空间复杂度：`O(V+E)`。
- 返回内容：`algorithm`、`num_communities`、`modularity`、社区大小统计、最多 20 个社区预览及截断标记。

| 算法 | 适用场景 | 时间特征 |
|------|----------|----------|
| `louvain` | 大图通用，模块度贪心合并 | 快 |
| `greedy_modularity` | 中小图，贪心模块度（实现不同于 louvain） | 中 |
| `label_propagation` | 超大图，同步标签传播 | 极快，结果不稳定 |
| `label_propagation_asyn` | 加权图，异步标签传播 | 快 |
| `leiden` | 高质量社区，Leiden 算法 | 中 |
| `girvan_newman` | 小图精确分析，边介数分裂 | 慢 |
| `asyn_fluidc` | 小图，异步流体社区发现 | 中 |

#### `node_centrality`

- 功能：计算中心性排名，支持 10 种中心性指标。
- 算法数：10（`degree`、`betweenness`、`closeness`、`pagerank`、`eigenvector`、`harmonic`、`katz`、`load`、`voterank`、`percolation`）
- 时间复杂度：`degree` 为 `O(V+E)`；`pagerank/eigenvector/katz` 约 `O(iter*(V+E))`；`betweenness/load` 常见为 `O(V*E)`；`closeness/harmonic` 通常需要多源最短路，约 `O(V*(V+E))` 无权或 `O(V*(E+V log V))` 加权。
- 空间复杂度：`O(V+E)` 到 `O(V)` 结果字典，具体由算法决定。
- 返回内容：`centrality_type`、`total_nodes`、`value_stats`、top 50 节点、bottom 10 节点、`truncated`。

| 算法 | 度量含义 | 时间特征 |
|------|----------|----------|
| `degree` | 连接数 | `O(V+E)`，极快 |
| `betweenness` | 最短路桥接作用 | `O(V*E)`，慢 |
| `closeness` | 到其他节点平均距离 | `O(V*(V+E))`，慢 |
| `pagerank` | 随机游走全局重要性 | `O(iter*(V+E))`，中 |
| `eigenvector` | 邻居重要性加权 | `O(iter*(V+E))`，中 |
| `harmonic` | 非连通图可达性 | `O(V*(V+E))`，慢 |
| `katz` | 长路径衰减影响力 | `O(iter*(V+E))`，中 |
| `load` | 流量负载（betweenness 变体） | `O(V*E)`，慢 |
| `voterank` | 迭代投票关键传播节点 | `O(V*iter)`，中 |
| `percolation` | 信息扩散能力 | `O(V+E)`，快 |

#### `graph_connectivity`

- 功能：分析连通性。有向图返回强/弱连通分量；无向图返回连通分量。
- 算法数：1
- 时间复杂度：`O(V+E)`。
- 空间复杂度：`O(V)`。
- 返回内容：有向图返回 `is_strongly_connected`、`is_weakly_connected`、强/弱连通分量摘要；无向图返回 `is_connected` 和连通分量摘要。

#### `find_cycles`

- 功能：检测简单环，最多展示 20 个。
- 算法数：1
- 时间复杂度：Johnson 类枚举复杂度约 `O((V+E)*(cycles+1))`，环很多时非常昂贵。
- 空间复杂度：当前实现会先物化全部环，最坏可达 `O(cycles*avg_cycle_len)`。
- 返回内容：`has_cycles`、`total_cycles`、最多 20 个 `cycles`、`truncated`。

#### `topological_sort`

- 功能：对有向无环图做拓扑排序；非 DAG 返回环示例。
- 算法数：1
- 时间复杂度：DAG 检查和排序为 `O(V+E)`；非 DAG 时采样环可能产生额外枚举成本。
- 空间复杂度：`O(V+E)`，排序结果为 `O(V)`，返回截断为 50 个节点。
- 返回内容：DAG 返回 `topological_order_preview`、`total_nodes`、`truncated`；非 DAG 返回失败原因和 `sample_cycles`。

#### `degree_analysis`

- 功能：统计入度/出度/总度分布及高低度节点。
- 算法数：1
- 时间复杂度：`O(V+E)`。
- 空间复杂度：`O(V)`。
- 返回内容：有向图返回入度/出度/总度统计、平均度、top 50 和 low 10 节点；无向图返回度统计、平均度和节点预览。

#### `global_metrics`

- 功能：计算图级指标，支持 6 种度量。
- 算法数：6（`density`、`diameter`、`radius`、`transitivity`、`average_clustering`、`degree_assortativity`）
- 时间复杂度：`density` 为 `O(1)` 或 `O(V+E)` 取决于图对象；`transitivity/average_clustering/degree_assortativity` 通常 `O(V+E)` 到三角形枚举成本；`diameter/radius` 需要最大连通分量上的全源最短路，最坏约 `O(V*(V+E))`。
- 空间复杂度：`O(V+E)` 用于无向图和最大连通分量；标量结果为 `O(1)`。
- 返回内容：`success`、`metric`、`value`。

| 指标 | 含义 | 时间特征 |
|------|------|----------|
| `density` | 实际边数 / 最大可能边数 | `O(1)` |
| `diameter` | 最大连通分量最长最短路径 | `O(V*(V+E))`，慢 |
| `radius` | 最大连通分量最小离心率 | `O(V*(V+E))`，慢 |
| `transitivity` | 全局聚类系数（三角形密度） | `O(V+E)` 到三角形枚举 |
| `average_clustering` | 平均局部聚类系数 | `O(V+E)` 到三角形枚举 |
| `degree_assortativity` | 相连节点度数相关系数 | `O(V+E)` |

### 子图与高级分析

#### `subgraph_extraction`

- 功能：提取子图，支持 5 种方法。
- 算法数：5（`ego_graph`、`k_core`、`k_truss`、`k_corona`、`node_subset`）
- 时间复杂度：`ego_graph` 与半径内规模相关，最坏 `O(V+E)`；`k_core/k_corona` 约 `O(V+E)`；`k_truss` 受三角形计数影响，可能显著高于线性；`node_subset` 约 `O(|nodes|+E_sub)`。
- 空间复杂度：`O(V_sub+E_sub)`。
- 返回内容：`method`、`num_nodes`、`num_edges`、节点预览、边预览和截断标记。

| 方法 | 含义 |
|------|------|
| `ego_graph` | 指定节点 k 跳邻域子图 |
| `k_core` | 所有度 ≥ k 的节点子图 |
| `k_truss` | 所有边至少在 k-2 个三角形中的子图 |
| `k_corona` | k-core 中恰好有 k 个核内邻居的节点子图 |
| `node_subset` | 指定节点集合的诱导子图 |

#### `link_prediction_score`

- 功能：对潜在边打分，支持 4 种传统链路预测算法；可针对单个节点对或全图非边 top-K。
- 算法数：4（`jaccard`、`adamic_adar`、`resource_allocation`、`preferential_attachment`）
- 时间复杂度：单节点对约 `O(deg(u)+deg(v))`；全图非边约 `O((V^2-E)*avg_deg)`，排序还需 `O(N log N)`。
- 空间复杂度：当前实现会物化全图非边和打分结果，最坏 `O(V^2)`；返回最多 50 条。
- 返回内容：`success`、`method`、`scores`，每项含 `source`、`target`、`score`。

| 算法 | 打分公式 |
|------|----------|
| `jaccard` | 共同邻居数 / 邻居并集数 |
| `adamic_adar` | 共同邻居按度倒数对数加权之和 |
| `resource_allocation` | 共同邻居度数倒数之和 |
| `preferential_attachment` | 两节点度数之积 |

#### `path_analysis`

- 功能：路径分析，支持 3 种方法。
- 算法数：3（`all_simple_paths`、`has_path`、`average_shortest_path_length`）
- 时间复杂度：`has_path` 为 `O(V+E)`；`all_simple_paths` 为 `O(P*V)`，受 `cutoff` 限制但仍可能指数级；`average_shortest_path_length` 在最大连通分量上做全源最短路，约 `O(V*(V+E))`。
- 空间复杂度：`has_path` 为 `O(V)`；`all_simple_paths` 返回最多 20 条但枚举内部有路径栈；平均最短路需要 `O(V+E)` 图结构和算法空间。
- 返回内容：路径列表、可达性布尔值，或平均最短路径长度标量。

#### `clique_analysis`

- 功能：团分析，支持 3 种方法。
- 算法数：3（`find_cliques`、`clique_number`、`number_of_cliques`）
- 时间复杂度：极大团枚举最坏 `O(3^(V/3))`。
- 空间复杂度：当前 `find_cliques` 会物化全部团，最坏 `O(number_of_cliques*clique_size)`；返回最多 50 个预览。
- 返回内容：`find_cliques` 返回团数量和预览；`clique_number` 返回最大团大小；`number_of_cliques` 返回单节点团数或 top 50 节点团计数。

#### `reachability`

- 功能：可达性分析，支持 3 种方法。
- 算法数：3（`transitive_closure`、`descendants`、`ancestors`）
- 时间复杂度：`descendants/ancestors` 为 `O(V+E)`；`transitive_closure` 约 `O(V*(V+E))`。
- 空间复杂度：`descendants/ancestors` 为 `O(V)`；传递闭包最坏 `O(V^2)`。
- 返回内容：传递闭包返回边数和最多 50 条边预览；后代/祖先返回总数、节点预览和截断标记。

### 图机器学习

> GraphML 工具依赖 PyTorch 和 PyTorch Geometric，需可选安装。支持 3 种 GNN 架构（GCN / GAT / GraphSAGE）和 1 种预训练方法（Node2Vec）。

#### `load_graph_classification_dataset`

- 功能：从当前会话已注册上传中安全加载 HIV/PCBA 多图 `.pt`，生成 fingerprint、绑定独立 DatasetStore，并返回 dataset readiness。多图文件不能使用单图 `load_graph_from_upload`。
- 算法数：1。
- 时间复杂度：`O(file_size + total_nodes + total_edges + total_labels)`。
- 空间复杂度：`O(dataset_size)`；受 `GRAPH_ML_MAX_RESIDENT_DATASETS` 常驻上限约束。
- 返回内容：异步 `job_id`；完成结果含 `dataset_id`、fingerprint、图/任务数、split、metric、缺失标签率和词表大小。

#### `train`

- 功能：发起图机器学习训练，支持节点分类、链路预测、整图分类，以及 Node2Vec 预训练。模型/方法和超参由 `model`、`method`、`data`、`train_config`、`algo_config` 指定；可异步执行。
- 算法数：10（GCN/GAT/SAGE × node_classification、link_prediction、graph_classification，以及 Node2Vec pretrain）
- 时间复杂度：GNN 类训练通常约 `O(epoch*(V+E)*hidden_dim*num_layers)`；Node2Vec 预训练约 `O(V*walks_per_node*walk_length)` 加优化训练成本。
- 空间复杂度：`O(V*feature_dim + E + model_params + optimizer_state)`。
- 返回内容：同步训练返回 `success`、`task`、`model_id`、`model`、`method`、`graph_version`、训练指标、模型元信息和 artifact 路径摘要；异步训练返回 `async=true`、`job_id`、`status=queued` 和提示。

| 任务 | 可选模型 | 说明 |
|------|----------|------|
| `node_classification` | GCN / GAT / GraphSAGE | 有监督节点分类 |
| `link_prediction` | GCN / GAT / GraphSAGE | 有监督链路预测 |
| `graph_classification` | GCN / GAT / GraphSAGE | HIV ROC-AUC / PCBA 128 任务 AP |
| `pretrain` | Node2Vec | 无监督节点 embedding 预训练 |

#### `infer`

- 功能：使用已训练模型执行节点分类、链接预测、整图分类或 embedding/pretrain 推理。图分类支持完整 split 指标和指定 `graph_indices`；PCBA 默认只返回 top-k 任务概率。
- 算法数：4（node_classification、link_prediction、graph_classification、pretrain，由模型决定）
- 时间复杂度：GNN 前向约 `O(V+E)`；指定少量节点/边时仍可能需要整图 embedding；链接预测候选边打分约 `O(K*embedding_dim)`。
- 空间复杂度：`O(V*embedding_dim + model_params)`。
- 返回内容：包含 `success`、`task`、`model_id` 和 `mode`；图分类返回 dataset 信息、完整集合 `evaluation` 和分页 `predictions`，其他任务保持原有返回。

#### `visualize_model_architecture`

- 功能：基于已训练模型元数据生成模型结构可视化（Mermaid 图 + 层级摘要）。
- 算法数：1
- 时间复杂度：`O(1)` 到 `O(model_layers)`。
- 空间复杂度：`O(model_layers)`。
- 返回内容：`success`、`task`、`model`、`method`、`model_id`、Mermaid 图文本、层级摘要和关键超参。

### 作业管理

#### `get_job_status`

- 功能：查询异步作业状态，并取出待消费的进度更新。
- 时间复杂度：`O(P_pending)`，通常近似 `O(1)`。
- 空间复杂度：`O(P_pending)`。
- 返回内容：`success`、`job`、`progress_updates`。`job` 通常含作业 ID、状态、工具名、创建/更新时间、结果或错误。

#### `cancel_job`

- 功能：取消异步作业。
- 时间复杂度：`O(1)`。
- 空间复杂度：`O(1)`。
- 返回内容：`success` 和 `job_id`。

### Agent 虚拟工具

#### `tool_select`

- 功能：按当前任务的自然语言描述检索并加载相关非核心工具 schema，让后续模型轮次可以调用这些工具。
- 时间复杂度：约 `O(N_tool*score_cost)`，由工具选择器对工具目录打分/筛选；若使用 LLM 选择，还包含一次模型调用成本。
- 空间复杂度：`O(top_k*schema_size)`。
- 返回内容：选中的工具名、加载的工具 schema、选择原因或错误信息。

#### `tool_expand`

- 功能：按名称加载未注入上下文的工具完整 schema。
- 时间复杂度：`O(N_tool)` 查找工具名。
- 空间复杂度：`O(schema_size)`。
- 返回内容：`expanded` 工具名和 `tool_schema`；未知或隐藏工具返回错误。

#### `request_user_feedback`

- 功能：向用户发起确认或修改请求，并暂停当前执行流程，等待反馈后继续。
- 时间复杂度：`O(1)`。
- 空间复杂度：`O(size(proposed_args))`。
- 返回内容：反馈请求对象，包含 `request_id`、`call_id`、`request_type`、`message`、`proposed_tool`、`proposed_args`、`editable_fields`、`step_id` 等；流式事件会发出 `await_user_feedback`。

#### `skills_list`

- 功能：列出可用 iGraph skills 的 compact index，可按 `domain` 过滤。
- 时间复杂度：`O(S*file_read)`，registry 会扫描 skill 目录并解析 `SKILL.md`。
- 空间复杂度：`O(S)`。
- 返回内容：`skills` 列表，每项含 `name`、`description`、`domain`、`version`、`risk_level`、`source`。

#### `skill_view`

- 功能：查看指定 skill 的完整指引，或读取其 supporting file。
- 时间复杂度：`O(S + file_size)`。
- 空间复杂度：`O(file_size)`，返回内容最多截断到约 12000 字符。
- 返回内容：skill 标题、描述、domain、版本和正文；读取 supporting file 时返回文件文本；未知 skill 返回错误。

#### `skill_manage`

- 功能：创建、维护、审批或拒绝用户/候选 skill；不能修改 bundled skills。支持 `create`、`write_file`、`remove_file`、`approve`、`reject`。
- 时间复杂度：`create/write_file` 约 `O(file_size)`；`approve/reject/remove_file` 约 `O(skill_dir_size)` 或 `O(1)`，取决于文件移动/删除规模。
- 空间复杂度：`O(file_size)`；磁盘空间受单文件 64KB、skill 目录 256KB 限制。
- 返回内容：JSON 结果，成功时含 `ok=true`、`action`、`name`、路径或文件名；失败时含 `ok=false`、`error`、`message`。

#### `memory_read`

- 功能：查询长期记忆、题型经验或用户档案，按关键词相关度排序。
- 时间复杂度：`O(M*term_count)`。
- 空间复杂度：`O(M)` 用于载入和排序。
- 返回内容：`scope`、`query`、`results`；结果结构取决于 scope，通常含 memory/playbook/profile ID、key/value、置信度、状态和时间戳。

#### `memory_write`

- 功能：手动写入长期记忆或用户档案；`project`/`dataset` scope 当前映射为长期记忆并保留 category。
- 时间复杂度：`O(M)`，需要查重/更新并写回快照。
- 空间复杂度：`O(M)`。
- 返回内容：`written` 和 `result`；成功结果含插入或更新操作、memory/profile ID、key/value、confidence；安全扫描失败或参数缺失时返回错误。

#### `memory_update`

- 功能：编辑已有长期记忆、用户档案或题型经验，可按 ID 或 key 定位。
- 时间复杂度：`O(M)`。
- 空间复杂度：`O(M)`。
- 返回内容：`updated` 和 `result`；成功结果含 `op`、memory/profile/playbook ID 和 key。

#### `memory_delete`

- 功能：软删除长期记忆或题型经验，将其标记为 archived。
- 时间复杂度：`O(M)`。
- 空间复杂度：`O(M)`。
- 返回内容：删除成功返回 `deleted=true` 及删除摘要；未找到返回 `deleted=false` 和 `reason=not_found`。

### 内部会话工具

#### `register_session`

- 功能：内部工具，将 MCP 会话绑定到业务会话 ID，并在 GraphData 侧创建会话。该工具不对 LLM 暴露。
- 时间复杂度：`O(1)`。
- 空间复杂度：`O(1)`。
- 返回内容：会话绑定/创建状态。

#### `delete_graph_session`

- 功能：内部工具，删除当前 MCP 会话关联的 GraphData 会话并解除绑定。该工具不对 LLM 暴露。
- 时间复杂度：`O(1)` 到 `O(V+E)`，取决于释放会话图数据和缓存规模。
- 空间复杂度：`O(1)`。
- 返回内容：删除/解绑状态。

## 完整工具表

| 分类 | 工具 | 算法数 | 功能摘要 | 时间复杂度 | 空间复杂度 | 返回内容摘要 |
|------|------|--------|----------|------------|------------|--------------|
| 数据与图状态 | `load_graph` | 1 | 从边列表文本加载图 | `O(L+E_in)` | `O(V_in+E_in)` | 状态、会话、图版本、提示 |
| 数据与图状态 | `load_graph_from_upload` | 1 | 从上传文件加载图（自动检测格式） | `O(file_size+E_in)` | `O(V_in+E_in)` | 状态、会话、图版本、文件名；或异步 job_id |
| 数据与图状态 | `clear_graph` | 1 | 清空当前图 | `O(1)` 到 `O(V+E)` | `O(1)` | 状态、会话、图版本 |
| 数据与图状态 | `undo_graph` | 1 | 回滚上一版本图 | `O(V+E)` | `O(V+E)` | 状态、图版本、节点/边数 |
| 数据与图状态 | `to_undirected` | 1 | 转为无向图 | `O(V+E)` | `O(V+E)` | 状态、图版本、节点/边数 |
| 数据与图状态 | `get_graph_info` | 1 | 图摘要和预览 | `O(V+E)` | `O(V_label+T+R)` | 规模、预览、元信息、ML 可行性 |
| 数据与图状态 | `list_datasets` | 1 | 列出数据集 | `O(D)` | `O(D)` | 数据集元数据列表 |
| 数据与图状态 | `load_dataset` | 1 | 加载数据集版本 | `O(V+E)` | `O(V+E)` | 数据集 ID/版本、图版本、规模 |
| 数据与图状态 | `save_as_dataset` | 1 | 当前图保存为数据集 | `O(V+E)` | `O(V+E)` | 新数据集元数据 |
| 数据与图状态 | `load_graph_from_text` | 1 | 文本抽三元组建图 | `O(L+T)` 加 LLM 成本 | `O(T)` | 图版本、规模、抽取统计、样例 |
| 数据与图状态 | `load_graph_from_triples` | 1 | 结构化三元组建图 | `O(T)` 或追加时 `O(V+E+T)` | `O(V_T+E_T)` | 图版本、规模、有效/跳过三元组、样例 |
| 数据与图状态 | `upload_document` | 1 | 文档入轻量知识库 | `O(L+C)` | `O(C*384+L)` | 文档 ID、分块数 |
| 数据与图状态 | `retrieve_context` | 1 | 文档语义检索 | `O(C*384+C log C)` | `O(C)` | top-K 分块和分数 |
| 图可视化 | `visualize_subgraph` | 1 | 图可视化数据（支持高亮路径/节点/子图） | `O(V_sub+E_sub)` | `O(V_sub+E_sub)` | 节点、边、高亮信息、截断标记 |
| 节点与边 | `get_node_info` | 1 | 节点详情 | `O(deg(v))` | `O(deg(v))` | 度数、属性、邻居/边预览 |
| 节点与边 | `get_edge_info` | 1 | 边详情 | 平均 `O(1)` | `O(1)` | 是否存在、权重、标签、类型 |
| 节点与边 | `check_edge_exists` | 1 | 检查边存在 | 平均 `O(1)` | `O(1)` | exists、directed |
| 节点与边 | `search_node` | 1 | 节点实体搜索 | 首次 `O((V+E)*embed_cost)`；复用后 `O(V*dim+V log V)` | `O((V+E)*dim)` | best、候选节点、分数、歧义标记 |
| 节点与边 | `search_edge` | 1 | 边实体搜索 | 首次同上；复用后 `O((V+E)*dim+E log E)` | `O((V+E)*dim)` | best、候选边、分数、端点、歧义标记 |
| 路径流树 | `shortest_path` | **3** | BFS/Dijkstra 最短路径 + Yen K 最短路 | `O(V+E)` 到 `O(k*(V+E)logV)` | `O(V+k*path_len)` | 路径、距离、边、可达统计 |
| 路径流树 | `all_shortest_paths` | 1 | 所有等长最短路径 | `O((V+E)+P*path_len)` | `O(20*path_len)` | 最多 20 条路径、长度、截断 |
| 路径流树 | `maximum_flow` | 1 | 最大流 | 常见估计 `O(V*E^2)` | `O(V+E)` | 最大流值、top 非零流边 |
| 路径流树 | `minimum_cut` | 1 | 最小割 | 常见估计 `O(V*E^2)` | `O(V+E)` | 割值、分区大小、割边预览 |
| 路径流树 | `minimum_spanning_tree` | 1 | 最小生成树/森林 | `O(E log V)` | `O(V+E)` | 总权重、MST 边预览或森林摘要 |
| 全局结构 | `community_detection` | **7** | 社区检测（louvain/greedy/LP/LP-asyn/leiden/girvan_newman/fluidc） | `O(V+E)` 到 `O(V*E^2)` | `O(V+E)` | 社区数、模块度、社区预览 |
| 全局结构 | `node_centrality` | **10** | 中心性排名（degree/betweenness/closeness/pagerank/eigenvector/harmonic/katz/load/voterank/percolation） | `O(V+E)` 到 `O(V*(V+E))` | `O(V+E)` | 统计、top/bottom 节点 |
| 全局结构 | `graph_connectivity` | 1 | 连通性分析 | `O(V+E)` | `O(V)` | 连通标志、分量摘要 |
| 全局结构 | `find_cycles` | 1 | 环检测 | `O((V+E)*(cycles+1))` | 最坏 `O(cycles*len)` | 环数量、最多 20 个环 |
| 全局结构 | `topological_sort` | 1 | 拓扑排序 | `O(V+E)` | `O(V+E)` | 排序预览或环示例 |
| 全局结构 | `degree_analysis` | 1 | 度分布统计 | `O(V+E)` | `O(V)` | 度统计、top/low 节点 |
| 全局结构 | `global_metrics` | **6** | 图级指标（density/diameter/radius/transitivity/avg_clustering/assortativity） | `O(1)` 到 `O(V*(V+E))` | `O(V+E)` | 单个指标值 |
| 高级分析 | `subgraph_extraction` | **5** | 提取 ego/core/truss/corona/subset 子图 | `O(V+E)` 到高于线性 | `O(V_sub+E_sub)` | 子图规模、节点/边预览 |
| 高级分析 | `link_prediction_score` | **4** | 链路预测打分（jaccard/adamic_adar/resource_allocation/preferential_attachment） | 单对 `O(deg²)`；全图最坏 `O(V^2*avg_deg)` | 最坏 `O(V^2)` | top-K 节点对分数 |
| 高级分析 | `path_analysis` | **3** | 简单路径/可达/平均最短路 | `O(V+E)` 到指数级 | `O(V+E)` | 路径、布尔值或标量 |
| 高级分析 | `clique_analysis` | **3** | 团分析（find_cliques/clique_number/number_of_cliques） | 最坏 `O(3^(V/3))` | 最坏与团数量同阶 | 团预览、最大团或团计数 |
| 高级分析 | `reachability` | **3** | 后代/祖先/传递闭包 | `O(V+E)` 或 `O(V*(V+E))` | `O(V)` 或 `O(V^2)` | 节点/边预览和总数 |
| GraphML | `load_graph_classification_dataset` | 1 | 安全加载并绑定 HIV/PCBA 多图数据 | `O(file_size+Σ(V+E)+labels)` | `O(dataset_size)` | 异步 job、dataset 摘要和 readiness |
| GraphML | `train` | **10** | 图机器学习训练（GCN/GAT/SAGE × 节点/链路/整图分类，Node2Vec 预训练） | `O(epoch*(V+E)*model_cost)` | `O(V*dim+E+params)` | 模型 ID、指标、元信息或异步 job |
| GraphML | `infer` | **4** | 图机器学习推理（节点/链路/整图分类/pretrain） | `O(V+E+K*dim)` | `O(V*dim+params)` | 评估、预测、embedding 样本 |
| GraphML | `visualize_model_architecture` | 1 | 模型结构可视化 | `O(model_layers)` | `O(model_layers)` | Mermaid、层级摘要、超参 |
| 作业管理 | `get_job_status` | 1 | 查询异步作业 | `O(P_pending)` | `O(P_pending)` | job 状态和进度更新 |
| 作业管理 | `cancel_job` | 1 | 取消异步作业 | `O(1)` | `O(1)` | 取消状态、job ID |
| Agent 虚拟 | `tool_select` | 1 | 检索并加载工具 schema | `O(N_tool*score_cost)` 加可选 LLM 成本 | `O(top_k*schema)` | 选中工具和 schema |
| Agent 虚拟 | `tool_expand` | 1 | 按名展开工具 schema | `O(N_tool)` | `O(schema)` | 展开的工具 schema |
| Agent 虚拟 | `request_user_feedback` | 1 | 暂停并请求用户确认/修改 | `O(1)` | `O(args_size)` | feedback request 对象 |
| Agent 虚拟 | `skills_list` | 1 | 列出 skills | `O(S*file_read)` | `O(S)` | skill compact index |
| Agent 虚拟 | `skill_view` | 1 | 查看 skill 或文件 | `O(S+file_size)` | `O(file_size)` | skill 正文或文件文本 |
| Agent 虚拟 | `skill_manage` | 1 | 创建/维护/审批 skill | `O(file_size)` 到 `O(skill_dir_size)` | `O(file_size)` | 管理操作 JSON 结果 |
| Agent 虚拟 | `memory_read` | 1 | 查询记忆 | `O(M*term_count)` | `O(M)` | scope、query、记忆结果 |
| Agent 虚拟 | `memory_write` | 1 | 写入记忆 | `O(M)` | `O(M)` | written 和写入结果 |
| Agent 虚拟 | `memory_update` | 1 | 更新记忆 | `O(M)` | `O(M)` | updated 和更新结果 |
| Agent 虚拟 | `memory_delete` | 1 | 软删除记忆 | `O(M)` | `O(M)` | deleted 和删除摘要 |
| 内部会话 | `register_session` | 1 | 绑定/创建图会话 | `O(1)` | `O(1)` | 会话绑定状态 |
| 内部会话 | `delete_graph_session` | 1 | 删除图会话 | `O(1)` 到 `O(V+E)` | `O(1)` | 删除/解绑状态 |

## 缺失的重要工具

当前 `main` 已合入 `search_node` / `search_edge`，所以"图搜索"不再是缺口。剩余更值得补齐的是以下能力。

### 图编辑工具组

- 建议工具：`add_node`、`add_edge`、`update_node`、`update_edge`、`delete_node`、`delete_edge`、`set_node_attributes`、`set_edge_attributes`。
- 价值：当前系统偏分析和导入，缺少细粒度修改图结构的能力。Agent 在发现数据错误、需要补边、改权重、修正类型或补充文本时，只能重新加载整图或构造新图。
- 设计要点：所有编辑工具都应产生新 `graph_version`，清理 NetworkX/search/GraphML 相关缓存，并返回变更摘要、影响节点/边数和可撤销提示。

### 图导出与报告工具

- 建议工具：`export_graph`、`export_subgraph`、`export_analysis_report`。
- 价值：当前可以导入、分析和保存数据集，但缺少把当前图或分析结果导出为 edge list、JSON、NPZ、GraphML/GEXF、Markdown 报告的统一工具。
- 设计要点：导出工具应支持格式、字段选择、是否包含特征/标签/文本、最大导出规模和文件路径返回。

### 数据集生命周期工具

- 建议工具：公开 `delete_dataset`，并补 `dataset_info`、`save_dataset_version`、`rename_dataset`。
- 价值：服务层已有 `do_delete_dataset`，但公开 catalog 中还没有对应 LLM 工具；数据集管理目前缺少删除、查看详情、增量版本保存和重命名。
- 设计要点：删除应作为高风险工具，需要用户确认；版本保存应返回新版本号和变更摘要。

### 文档知识库生命周期工具

- 建议工具：`list_documents`、`delete_document`、`update_document`、`clear_documents`。
- 价值：当前有 `upload_document` 和 `retrieve_context`，但没有查看、删除、更新已上传文档的工具。用户上传错误文件或切换语料时，Agent 难以管理 RAG 状态。
- 设计要点：返回文档 ID、分块数、embedding provider、维度、创建时间；删除/清空需要同步更新 index 和 embedding 矩阵。

### 分页与游标工具

- 建议工具：为 `get_graph_info`、`search_node`、`search_edge`、路径/社区/连通分量类结果补 `offset`/`cursor`/`page_size`，或新增 `list_nodes`、`list_edges`、`get_result_page`。
- 价值：当前很多工具为了大图安全只返回预览。预览是必要的，但用户有时确实需要继续查看后续结果。
- 设计要点：分页需要稳定排序键和可复用游标，避免图版本变化后游标错位。

### 约束路径与模式查询工具

- 建议工具：`constrained_shortest_path`、`pattern_match`、`neighbors_by_filter`。
- 价值：已有路径工具能回答通用最短路，但不能表达"只走某类边""避开某些节点""找符合 A-[关系]->B-[关系]->C 模式的子图"。
- 设计要点：支持 `allowed_edge_types`、`blocked_nodes`、`max_hops`、`direction`、节点/边属性过滤和结果上限。

## 现有工具优化方向

### 重型图算法异步化

`community_detection`、`node_centrality`、`find_cycles`、`clique_analysis`、`reachability(transitive_closure)`、`link_prediction_score`、`global_metrics(diameter/radius)` 都可能在大图上耗时很长。建议像 GraphML 一样支持 `async_job`，并用 `get_job_status` 读取进度和结果。

### 避免先物化再截断

部分工具虽然返回已截断，但实现仍可能先物化大集合。例如 `find_cycles`、`clique_analysis(find_cliques)`、`link_prediction_score` 全图非边、`reachability(transitive_closure)` 都有最坏内存风险。建议改成生成器式 top-K、流式截断、阈值保护或直接要求用户提供更窄过滤条件。

### 统一返回协议

建议所有工具都固定包含 `success`、`error`、`elapsed_ms`、`graph_version`、`truncated`、`total_count`、`warnings`。现在不同工具返回结构差异较大，Agent 和前端需要写更多兼容逻辑。

### 搜索索引可观测性与生命周期

`search_node` / `search_edge` 已有索引缓存和语义降级，但可以继续优化：增加 `search_index_status`、`rebuild_search_index`，暴露索引是否 stale、embedding provider、维度、构建耗时、节点/边文档数和降级原因。

### 搜索与 RAG 的 embedding provider 更稳健

建议统一 `retrieve_context` 与实体搜索的 provider 行为：维度不一致时明确报错或触发重建；provider 初始化失败时记录 warning 并返回可见原因；SentenceTransformer 维度应从模型实例读取，而不是依赖默认维度假设。

### 更强的过滤与排序参数

`search_node` / `search_edge` 已有基础过滤；路径、中心性、社区、链路预测等工具也可以支持属性过滤、节点类型过滤、边类型过滤、权重阈值、`top_k` 和排序字段。这样 Agent 可以先缩小问题空间，再调用重型算法。

### GraphML 输出和任务控制

`train` / `infer` 已有异步能力和截断返回。后续可以补批量分页、训练中断恢复、模型列表/删除、模型评估对比、预测结果导出，并让 `infer` 对指定节点/边的查询尽量避免不必要的整图返回。

### 前端与日志中的工具元数据

当前已经保留工具参数、`call_id`、server 和步骤进度。可以继续加入 `elapsed_ms`、输入规模、输出截断状态、缓存命中状态，让调试和用户理解更直观。
