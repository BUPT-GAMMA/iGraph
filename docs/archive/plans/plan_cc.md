# iGraph 迭代升级计划

> 基于 compare_cc.md 与 compare_codex.md 综合制定
> 制定日期：2026-04-09

---

## 一、总体目标与原则

### 核心目标

用 **iGraph 的工程底座**，吸收 **YiGraph 的 AAG/DAG/算法知识方法论**，将 iGraph 从"强工程的图工具型 Agent"进化为：

> **既强工程、又强分析规划、还自带图学习能力的完整图智能体平台**

### 必须保持的核心优势（不能破坏）

1. **轻量部署**：零外部数据库强依赖，`python agent_server.py` 一键启动
2. **透明协议**：Text Protocol 双栈设计，SFT/RL 数据采集能力是差异化竞争点
3. **服务边界清晰**：API Gateway / MCP / GraphData 三层分工不变，tool_catalog 单一真相源不变

### 吸收 YiGraph 的方向

| 吸收点 | YiGraph 的做法 | iGraph 的适配方式 |
|--------|--------------|----------------------|
| 显式任务规划 | DAG + WorkflowStep 对象 | 轻量 Planner，不引入完整 DAG 引擎 |
| 算法知识层 | algorithms.yaml（106 条） | algorithm_catalog.py，复用 tool_catalog 体系 |
| 多数据类型建图 | DatasetManager + Text/Table Loader | 新增 data_pipeline 模块，保持服务化 |
| 分析报告化 | 步骤级输出 + LLM 汇总报告 | 结构化输出格式，可选 report 模式 |

---

## 二、三阶段路线图概览

```
Phase 1 ─────────────────────────────────────── 近期
  Tool Agent → Plan-aware Agent
  核心：让 iGraph 能显式规划复杂任务 + 扩展算法广度

Phase 2 ─────────────────────────────────────── 中期
  会话图 → 数据集图平台
  核心：图数据资产管理 + 多模态建图 + 语义记忆

Phase 3 ─────────────────────────────────────── 长期
  图分析平台 → 图分析+图学习+图查询统一平台
  核心：GraphML 编排化 + 图后端可插拔 + 作业系统
```

| 阶段 | 关键里程碑 | 预期收益 |
|------|----------|---------|
| **Phase 1** | Planner 上线；算法库 50+；工具动态注入 | 复杂任务成功率↑；token 成本↓ |
| **Phase 2** | Dataset Registry；Text→Graph；语义记忆 | 从"对话工具"升级为"分析平台" |
| **Phase 3** | GraphML 编排；Neo4j 可选；作业系统 | 产品定位完整，覆盖企业场景 |

---

## 三、Phase 1：规划感知 Agent（近期）

**目标**：在不改动现有 MCP / GraphData / GraphML 结构的前提下，让 iGraph 具备显式任务规划能力，同时补齐算法广度和 token 效率。

---

### 3.1 轻量 Planner 模块

**现状问题**

iGraph 的主循环是 ReAct 风格——LLM 每轮即时决定下一个工具调用。对于复杂多步任务（如"找出最可疑社区并分析内部关键节点"），LLM 容易在中途迷失，且无中间工作流对象可供用户查看或修改。

**目标**

在 `backend/agent/` 下新增 `planner.py`，实现"Plan → Execute → Explain"三段式流程，作为 `iGraph` 主循环的可选前置阶段。

**核心数据结构**

```python
@dataclass
class PlanStep:
    step_id: int
    question: str               # 子问题描述
    candidate_tools: list[str]  # 候选工具名（来自 algorithm_catalog）
    depends_on: list[int]       # 依赖的 step_id 列表
    status: str                 # pending | running | done | failed
    result_summary: str         # 执行后填入

@dataclass
class AnalysisPlan:
    plan_id: str
    original_query: str
    steps: list[PlanStep]
    created_at: str
```

**触发逻辑**（在 `igraph.py` 中）

```
用户消息进入 → _should_plan(message) 判断：
  - 含多个"?"、"先…然后…"、"分析…并…" 等多步骤语义
  - 或用户明确说"帮我规划" / "分步分析"
→ True：触发 Planner.build_plan(query)
    → 调用一次 LLM，生成结构化 JSON 计划
    → 解析为 AnalysisPlan 对象
    → SSE 推送 plan_ready 事件给前端
    → 等待用户确认（3s 超时自动继续）
    → Planner.execute_plan(plan)
        → 按 step 顺序调用现有工具链
        → 每步完成推送 step_done 事件
    → 最终 LLM 汇总所有步骤结果 → 报告
→ False：走原有 ReAct 主循环，不受影响
```

**计划持久化**：将 `AnalysisPlan` 序列化到 `logs/{session_id}/plan_{plan_id}.json`。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/planner.py` | 新增 |
| `backend/agent/igraph.py` | 新增 `_should_plan()` + `_run_with_plan()` 分支 |
| `backend/api_gateway/app.py` | 新增 `plan_ready` / `step_done` SSE 事件类型 |
| `index.html` | 新增步骤列表渲染区域 |

---

### 3.2 Algorithm Catalog（算法知识层）

**现状问题**

`tool_catalog.py` 只有工具调用规格（参数 schema），没有算法的语义描述、适用场景、任务类型映射。Planner 在分配工具时缺乏知识支撑，容易幻觉调用不存在的工具。

**目标**

新增 `algorithm_catalog.py`，为每个工具补充语义元数据，供 Planner 的算法选择阶段消费，同时防止幻觉（只允许 catalog 中存在的算法被规划进 plan）。

**数据结构示例**

```python
# graph_data_service/algorithm_catalog.py

ALGORITHM_CATALOG = [
    {
        "name": "louvain_communities",
        "task_types": ["community_detection", "clustering"],
        "description": "基于模块度优化的社区发现，适合无权重无向图",
        "suitable_graph_types": ["undirected", "weighted_undirected"],
        "output_type": "node_partition",
        "typical_use_cases": ["社交网络群体划分", "生物网络模块识别"],
        "aliases": ["louvain", "community_detection"]
    },
    # ... 每个工具一条记录
]

TASK_TYPE_MAP = {
    "find_important_nodes":  ["pagerank", "betweenness_centrality", "eigenvector_centrality"],
    "detect_communities":    ["louvain_communities", "greedy_modularity_communities"],
    "find_path":             ["shortest_path", "all_pairs_shortest_path"],
    "analyze_structure":     ["connected_components", "bridges", "articulation_points"],
    "ml_node_classification":["train", "infer"],
}
```

Planner 构建计划时将 `ALGORITHM_CATALOG` 和 `TASK_TYPE_MAP` 注入 LLM context，约束其只选已知算法名。

---

### 3.3 扩展算法库（19 → 50+）

**现状问题**

`GRAPH_TOOLS` 仅 19 个，用户遇到 ego_graph、harmonic centrality、k-core、Leiden 等需求时直接失败。

**扩展清单**

| 类别 | 新增算法 |
|------|---------|
| **中心性** | harmonic_centrality, voterank, percolation_centrality, load_centrality |
| **子图/过滤** | ego_graph, k_core, k_truss, k_corona |
| **社区** | leiden（via igraph）, girvan_newman, asyn_fluidc |
| **路径** | yen_k_shortest_paths, all_simple_paths（限深度）, has_path |
| **相似性** | jaccard_coefficient, adamic_adar_index, resource_allocation_index |
| **结构** | graph_clique_number, find_cliques（限规模）, graph_number_of_cliques |
| **可达性** | transitive_closure, descendants, ancestors |
| **度/聚类** | degree_assortativity, average_clustering, transitivity |

**扩展步骤**（每个算法固定三步）：
1. `graph_data_service/compute.py` — 加实现方法
2. `graph_data_service/tool_catalog.py` — 加一条 spec
3. `graph_data_service/algorithm_catalog.py` — 加一条知识元数据

MCP 自动注册，system prompt 自动更新，无需改动其他文件。

---

### 3.4 工具目录动态语义注入

**现状问题**

Text Protocol 模式下，每轮对话将全部工具的完整 spec 注入 system prompt，单次约 2000 token。扩展到 50+ 工具后问题更突出，长对话 token 消耗极高。

**方案**

新增 `backend/agent/tool_selector.py`，启动时对所有工具 description 做 embedding，每轮对话前语义检索 top-K 相关工具注入：

```python
class ToolSelector:
    def __init__(self, all_tools: list[dict]):
        # 启动时一次性 embed 所有工具描述
        self._matrix = embed_batch([t["description"] for t in all_tools])
        self._tools = all_tools

    def select(self, user_query: str, k: int = 10) -> list[dict]:
        q_emb = embed(user_query)
        scores = cosine_similarity(q_emb, self._matrix)
        top_idx = scores.argsort()[-k:][::-1]
        # 置信度兜底：max_score < 0.3 则返回全集，确保不漏工具
        if scores[top_idx[0]] < 0.3:
            return self._tools
        return [self._tools[i] for i in top_idx]
```

embedding 模型：`sentence-transformers/all-MiniLM-L6-v2`（本地，内存 <200MB，推理 <5ms），iGraph 启动时一次性加载。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/tool_selector.py` | 新增 |
| `backend/agent/context_manager.py` | 调用 ToolSelector，替换全量注入 |
| `backend/agent/igraph.py` | 初始化时构建 ToolSelector 实例 |

**预期收益**：Text 协议模式 token 消耗降低约 50%。

---

## 四、Phase 2：数据集图平台（中期）

**目标**：将 iGraph 从"对话时加载一张图"升级为"对某个数据资产持续分析"，同时提升记忆系统的语义能力，补充轻量 RAG 和图数据持久化。

---

### 4.1 Dataset Registry + 图持久化

**现状问题**

- 图数据只在内存中，服务重启即丢失
- 没有"数据集"概念，用户每次重新上传才能使用历史图
- `session_meta.json` 不记录图的基本统计信息

**方案**

在 `graph_data_service/` 下新增 `dataset_registry.py`，管理"数据资产"与"会话图"两层概念：

```
dataset_registry/
  {dataset_id}/
    meta.json          # 数据集名称、描述、创建时间、节点数、边数
    graph.npz          # 序列化图数据（原始资产）
    schema.json        # 字段定义（可选）

uploads/{session_id}/
  graph_snapshot.npz   # 会话图当前快照（含版本号）
  snapshots/
    v1.npz, v2.npz … v5.npz  # 最近 5 个历史版本
```

**关键行为**

- 每次 `load_graph` / `to_undirected` 等变更操作后，自动将当前 `GraphSession` 图序列化为快照
- `GraphDataService` 启动时扫描 `uploads/` 目录，自动恢复各 session 最新快照到内存
- 新增工具 `undo_graph`：将当前图回滚到上一个版本快照
- `session_meta.json` 补充字段：`node_count`, `edge_count`, `is_directed`, `has_weights`

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/dataset_registry.py` | 新增 |
| `graph_data_service/session.py` | 新增快照写入逻辑 |
| `graph_data_service/app.py` | 启动时自动恢复快照 |
| `graph_data_service/tool_catalog.py` | 新增 `undo_graph`、`list_datasets` 工具 |
| `backend/logging/session_store.py` | 补充 meta 字段写入 |

---

### 4.2 Text / Table → Graph 建图管线

**现状问题**

iGraph 当前只支持手动输入边列表或上传 `.txt/.json/.npz` 文件。用户有大量"从 CSV 关系表建图"、"从文本提取实体关系"的需求无法满足。

**方案**

新增 `graph_data_service/data_pipeline/` 子模块，两阶段建图：

```
阶段一：结构提取
  TableExtractor：CSV/Excel → (source_col, target_col, weight_col) → 边列表
  TextExtractor：文本 → LLM 抽取三元组 (entity1, relation, entity2) → 边列表

阶段二：图构建与验证
  GraphBuilder：边列表 + schema → GraphData → 写入 GraphSession
  验证：节点/边数量、孤立节点比例、图是否连通
```

**表格建图**（优先实现，无 LLM 依赖）

```python
# 用户上传 CSV，指定字段
{
  "source_field": "from_account",
  "target_field": "to_account",
  "weight_field": "amount",       # 可选
  "directed": true
}
→ TableExtractor 读取 CSV，生成标准边列表
→ 直接调用现有 load_graph 逻辑
```

**文本建图**（依赖 LLM，第二步实现）

```python
# 用户上传文档，LLM 抽取三元组
TextExtractor.extract(text) →
  LLM prompt: "从以下文本抽取实体关系三元组，返回 JSON..."
  → [(entity1, relation, entity2), ...]
  → 以 entity 为节点、relation 为边属性构建图
```

**新增工具**：`build_graph_from_table`、`build_graph_from_text`

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/data_pipeline/table_extractor.py` | 新增 |
| `graph_data_service/data_pipeline/text_extractor.py` | 新增 |
| `graph_data_service/data_pipeline/graph_builder.py` | 新增 |
| `graph_data_service/tool_catalog.py` | 新增两条工具 spec |
| `backend/api_gateway/app.py` | 文件上传支持 `.csv` / `.xlsx` |

---

### 4.3 记忆召回语义化

**现状问题**

`memory_engine.py` 的 `build_memory_prompt()` 使用关键词字符串匹配评分，无法处理同义词和语义相似（如用户说"关键人物"，记忆里存的是"核心节点"，无法匹配）。

**方案**

复用 Phase 1 中已引入的 `ToolSelector` embedding 模型，为记忆条目构建向量索引：

```python
class SemanticMemoryIndex:
    def __init__(self, memory_items: list[dict]):
        texts = [f"{m['key']}: {m['value']}" for m in memory_items]
        self._matrix = embed_batch(texts)
        self._items = memory_items

    def recall(self, query: str, top_k: int = 8) -> list[dict]:
        scores = cosine_similarity(embed(query), self._matrix)
        return [self._items[i] for i in scores.argsort()[-top_k:][::-1]]
```

索引在 `MemoryEngine` 初始化时构建，每次 memory update 后增量更新（追加新向量，不重建全量）。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/memory_engine.py` | 新增 `SemanticMemoryIndex`，替换关键词评分逻辑 |
| `backend/agent/igraph.py` | 共享 Phase 1 的 embedding 模型实例（避免重复加载） |

**预期收益**：记忆召回相关性显著提升，尤其对多轮对话中跨语义表达的问题。

---

### 4.4 轻量 RAG 支持

**现状问题**

Agent 无法利用外部文档知识（图数据说明、行业报告、算法论文）。引入 Milvus 太重，不符合 iGraph 轻量化原则。

**方案**

在 GraphData Service 中集成基于文件系统 + numpy 的轻量向量检索，无外部数据库依赖：

```
uploads/{session_id}/
  docs/
    {doc_id}.txt        # 原始文档（分块后）
    {doc_id}.npy        # 对应 chunk embeddings
  doc_index.json        # chunk → doc_id + offset 映射
```

**新增工具**：`upload_document`、`retrieve_context`

```
Agent 可主动调用 retrieve_context(query) →
  → 对 query 做 embedding
  → 与 doc_index 中所有 chunk embeddings 做 cosine 检索
  → 返回 top-5 相关 chunk 文本
  → Agent 将其作为 tool_observation 注入下一轮推理
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/rag.py` | 新增 |
| `graph_data_service/tool_catalog.py` | 新增 `upload_document`、`retrieve_context` |
| `backend/api_gateway/app.py` | 文档上传路由 |

---

### 4.5 多用户记忆隔离

**现状问题**

记忆系统固定使用 `default_user`，所有会话共享同一份长期记忆，多用户场景下互相污染。

**方案**

- API Gateway 新增简单 token 认证中间件，从 `Authorization` header 解析 `user_id`（可降级为 session 级隔离）
- 记忆目录改为 `backend/agent/memory/{user_id}/`
- `iGraph` 构造时接收 `user_id`，`MemoryEngine` 按 `user_id` 加载对应记忆
- 工具调用的 session 注册也携带 `user_id`，GraphData Service 按 `{user_id}:{session_id}` 隔离图状态

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/api_gateway/app.py` | 新增认证中间件，请求注入 `user_id` |
| `backend/agent/igraph.py` | 构造函数接收 `user_id` |
| `backend/agent/memory_engine.py` | 按 `user_id` 路由记忆目录 |
| `mcp_service/session_registry.py` | session key 带 `user_id` 前缀 |

---

## 五、Phase 3：统一图智能体平台（长期）

**目标**：完成 GraphML 编排化、图后端可插拔、异步作业系统、结构化报告和 SFT 数据导出，使 iGraph 具备完整的企业级图分析 + 图学习平台能力。

---

### 5.1 GraphML 编排化（从工具到规划节点）

**现状问题**

GraphML 目前作为独立工具集存在（`train`/`infer` 等），LLM 无法主动将其嵌入分析规划流程。例如"先训练节点嵌入，再用嵌入结果辅助社区检测"这类组合任务无法自动完成。

**方案**

在 Phase 1 的 Planner 基础上，扩展 `algorithm_catalog.py` 的 `TASK_TYPE_MAP`，将 GraphML 任务纳入规划体系：

```python
TASK_TYPE_MAP = {
    # 新增 ML 相关任务类型
    "node_embedding":         ["pre_train"],
    "node_classification":    ["train(task=node_classification)", "infer"],
    "link_prediction":        ["train(task=link_prediction)", "infer"],
    "ml_assisted_analysis":   ["pre_train", "louvain_communities"],  # 组合模板
}
```

同时在 Planner 中新增 `MLStep` 类型，识别当 step 涉及 GraphML 时：
- 自动检查 `artifact_store` 中是否已有可复用模型
- 若有，跳过训练直接推理
- 若无，自动插入 `train` step 作为前置依赖

新增**模型版本与图版本一致性检查**：训练时记录 `(session_id, graph_version)`，推理时校验当前图版本是否匹配，不匹配则提示重新训练。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/algorithm_catalog.py` | 补充 ML 任务类型 |
| `backend/agent/planner.py` | 新增 `MLStep` 类型和依赖插入逻辑 |
| `graph_data_service/graph_ml/artifact_store.py` | 新增版本一致性校验接口 |

---

### 5.2 图后端可插拔（Neo4j Adapter）

**现状问题**

图数据完全内存 + NetworkX，无法处理百万级节点的大规模图，也不支持持久化图查询。

**方案**

在 `GraphDataService` 内部引入存储后端抽象，对上层 Agent 和 MCP 完全透明：

```python
# graph_data_service/storage/base.py
class GraphStorageBackend(ABC):
    @abstractmethod
    def load(self, session_id: str) -> GraphData: ...
    @abstractmethod
    def save(self, session_id: str, graph: GraphData): ...
    @abstractmethod
    def query_subgraph(self, session_id: str, node_ids: list) -> GraphData: ...

# graph_data_service/storage/memory_backend.py  （当前逻辑迁入，默认）
class MemoryBackend(GraphStorageBackend): ...

# graph_data_service/storage/neo4j_backend.py   （可选，配置启用）
class Neo4jBackend(GraphStorageBackend): ...
```

算法执行层（`compute.py`）保持 NetworkX 不变，Neo4j 后端在执行前按需拉取子图到内存。通过 `.env` 配置切换：

```
GRAPH_STORAGE_BACKEND=memory   # 默认
GRAPH_STORAGE_BACKEND=neo4j    # 需配置 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/storage/` | 新增目录 + base.py + memory_backend.py + neo4j_backend.py |
| `graph_data_service/session.py` | 持有 `StorageBackend` 实例而非直接持有 GraphData |
| `graph_data_service/app.py` | 启动时按配置实例化 backend |
| `backend/runtime/env_required.py` | 新增 Neo4j 配置项的条件校验 |

---

### 5.3 异步作业管理系统

**现状问题**

GraphML 训练、大图社区检测等长时间操作当前在 HTTP 请求中同步执行，无法中断、无进度反馈、超时风险高。

**方案**

在 `graph_data_service/` 中新增轻量作业系统（不引入 Celery，用 asyncio + 内存队列实现）：

```
JobManager:
  submit(fn, args) → job_id
  status(job_id)   → {status, progress, message, result}
  cancel(job_id)
  list_jobs(session_id)
```

**进度推送**：长时间工具（`train`、`girvan_newman` 等）执行时，通过回调更新 `JobManager` 中的 progress 字段，API Gateway 轮询并通过 SSE 推送 `tool_progress` 事件：

```json
{"event": "tool_progress", "job_id": "xxx", "progress": 0.42, "message": "Epoch 42/100, loss=0.234"}
```

前端在工具执行期间显示进度条，大幅改善训练等待体验。

**新增工具**：`get_job_status`、`cancel_job`（供 Agent 主动查询和取消长任务）

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/job_manager.py` | 新增 |
| `graph_data_service/graph_ml/trainer/` | 各 trainer 新增 progress_callback 参数 |
| `graph_data_service/tool_catalog.py` | 新增 `get_job_status`、`cancel_job` |
| `backend/api_gateway/app.py` | SSE 新增 `tool_progress` 事件类型 |
| `index.html` | 进度条 UI |

---

### 5.4 结构化分析报告

**现状问题**

Agent 输出为即时 Markdown 文本，对于复杂多步分析，结果散落在多轮对话中，难以回溯和下游消费。

**方案**

在 Phase 1 Planner 的基础上，Plan 执行完毕后由 LLM 生成标准化报告对象：

```python
@dataclass
class AnalysisReport:
    session_id: str
    query: str
    plan_summary: str       # 分析步骤概述
    key_findings: list[str] # 关键发现（每步一条）
    algorithm_used: list[str]
    conclusion: str         # 最终结论
    limitations: str        # 分析局限性
    next_steps: list[str]   # 建议的后续分析方向
    created_at: str
```

报告持久化到 `logs/{session_id}/report_{plan_id}.json`，同时在前端新增"报告视图"Tab，以结构化卡片形式展示。

同时新增 `output_format` 参数支持：
- `"chat"`（默认）：当前流式对话行为
- `"report"`：触发 Planner + 报告生成
- `"json"`：返回 `AnalysisReport` JSON，供下游系统消费

---

### 5.5 SFT 数据集导出

**现状问题**

Text Protocol 模式下所有上下文已完整记录在 `logs/{session_id}/message_list.jsonl`，是宝贵的训练数据来源，但没有工具将其转换为标准格式。

**方案**

新增 `/api/export/sft` 接口：

```
GET /api/export/sft
  ?format=alpaca|sharegpt|raw
  &from=2026-01-01
  &to=2026-04-09
  &min_turns=2              # 过滤过短会话
  &require_tool_calls=true  # 只导出含工具调用的轮次
→ 返回 .jsonl 文件下载
```

转换逻辑：

```
message_list.jsonl 中每个 (user_turn, assistant_protocol, tool_observation..., answer) 序列
→ 提取完整工具调用链路
→ 按格式输出：
   Alpaca: {"instruction": user, "input": graph_context, "output": answer}
   ShareGPT: {"conversations": [{role, value}, ...]}  含完整 tool_calls
```

这是 iGraph **独有的竞争优势**，值得作为核心功能对外宣传。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/api_gateway/app.py` | 新增 `/api/export/sft` 路由 |
| `backend/logging/sft_exporter.py` | 新增，负责格式转换 |
| `index.html` | 新增导出入口 UI |

---

## 六、横切关注点

### 6.1 Cytoscape.js 图可视化

**现状**：前端用 mermaid.js 渲染图，只能显示小图，无交互。

**方案**：引入 `Cytoscape.js`（纯前端，零后端依赖），替换 mermaid 图渲染：
- 支持节点高亮（算法结果着色：最短路径红色、社区不同颜色、高中心性节点放大）
- 支持拖拽布局、缩放、点击节点弹出属性面板
- 工具结果新增 `highlight` 字段：`{"nodes": [...], "edges": [...], "color": "red"}`
- 前端按 highlight 字段渲染覆盖层

**涉及文件**：`index.html`（引入 Cytoscape.js CDN）、`graph_data_service/codec.py`（结果序列化带 highlight）

---

### 6.2 测试与评测体系

**现状**：有 11 个测试文件，覆盖服务集成层，但缺少 Agent 行为级评测。

**补充方向**：

| 评测维度 | 实现方式 |
|---------|---------|
| 工具选择正确率 | 构建"问题 → 期望工具序列"基准集，回放对比 |
| 规划质量 | 人工标注 20 个复杂问题的参考 Plan，与 Planner 输出做结构对比 |
| GraphML 稳定性 | 固定随机种子 + 合成图，确保 train/infer 结果可复现 |
| 端到端 session replay | 录制标准会话 → 重放 → 对比工具调用链路和最终答案 |

基准集维护在 `tests/benchmarks/` 目录下。

---

## 七、优先级汇总

| 阶段 | 编号 | 优化项 | 涉及文件数 | 预期收益 |
|------|------|-------|-----------|---------|
| **Phase 1** | 3.1 | 轻量 Planner 模块 | 4 | 复杂任务成功率↑ |
| **Phase 1** | 3.2 | Algorithm Catalog | 1 | 防幻觉，规划有依据 |
| **Phase 1** | 3.3 | 算法库扩展 19→50+ | 2 | 覆盖更多用户需求 |
| **Phase 1** | 3.4 | 工具动态语义注入 | 3 | token 成本降低 ~50% |
| **Phase 2** | 4.1 | Dataset Registry + 持久化 | 5 | 重启不丢数据，支持撤销 |
| **Phase 2** | 4.2 | Text/Table→Graph 建图 | 5 | 大幅扩展数据接入场景 |
| **Phase 2** | 4.3 | 记忆召回语义化 | 2 | 跨语义记忆匹配准确率↑ |
| **Phase 2** | 4.4 | 轻量 RAG | 3 | 外部知识融合，零重依赖 |
| **Phase 2** | 4.5 | 多用户记忆隔离 | 4 | 生产多用户可用 |
| **Phase 3** | 5.1 | GraphML 编排化 | 3 | ML 步骤进入规划流程 |
| **Phase 3** | 5.2 | 图后端可插拔 | 4 | 大规模图支持 |
| **Phase 3** | 5.3 | 异步作业系统 | 5 | 长任务体验质变 |
| **Phase 3** | 5.4 | 结构化分析报告 | 3 | 复杂分析可读性大幅提升 |
| **Phase 3** | 5.5 | SFT 数据集导出 | 3 | 强化独有竞争优势 |
| **横切** | 6.1 | Cytoscape.js 可视化 | 2 | 交互体验大幅提升 |
| **横切** | 6.2 | 测试与评测体系 | N/A | 回归保障，规划质量可量化 |

---

*核心原则：iGraph 的差异化价值在于**透明协议 + 自动记忆 + GNN 一体化 + 轻量部署**。三个阶段的迭代始终围绕这四点展开，不引入 Milvus / Neo4j 等强依赖（Neo4j 为可选），不破坏现有服务边界，每个优化项均可独立交付。*
---
