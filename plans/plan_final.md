# iGraph 最终迭代计划

> 综合 compare_cc.md / compare_codex.md / compare_by_table_chart.md / plan_cc.md / plan_codex.md 五份分析文件，  
> 以当前代码实际状态为基准制定。  
> 制定日期：2026-04-10

---

## 一、项目现状与代码基线

### 1.1 当前架构

```
Browser / CLI / Claude Desktop
         ↓ HTTP / SSE / MCP stdio
┌──────────────────────────────────┐
│  API Gateway          :1109      │  会话路由、文件上传、Agent 生命周期
│  backend/api_gateway/app.py      │  695 行
└─────────┬────────────────────────┘
          │ MCP JSON-RPC
┌─────────┴────────────────────────┐
│  MCP Service          :1110      │  工具语义层、动态注册、Session key 映射
│  mcp_service/server.py           │  266 行
└─────────┬────────────────────────┘
          │ HTTP REST
┌─────────┴────────────────────────┐
│  GraphData Service    :1111      │  图状态持有、NetworkX 算法、GraphML 模块
│  graph_data_service/app.py       │  579 行
└──────────────────────────────────┘
```

进程管理：API Gateway 通过 `ProcessSupervisor` 自动 spawn GraphData + MCP 子进程；生产模式三进程独立启动。

### 1.2 核心模块文件清单

| 层 | 文件 | 行数 | 职责 |
|---|---|---|---|
| **Agent** | `backend/agent/igraph.py` | 733 | 会话编排、ReAct 主循环、工具调用 |
| | `backend/agent/memory_engine.py` | 1043 | 长期记忆 + Playbook + 异步更新 |
| | `backend/agent/context_manager.py` | 394 | System prompt 组装、消息历史管理 |
| | `backend/agent/text_protocol_driver.py` | 358 | 文本协议解析（StatefulStreamParser） |
| | `backend/agent/native_fc_driver.py` | 240 | 原生 Function Calling 驱动 |
| | `backend/agent/provider_gateway.py` | 288 | 多 LLM 后端路由（OpenAI/Anthropic/Qwen） |
| | `backend/agent/skill_loader.py` | ~50 | 按工具集条件注入 Skill prompt |
| **API** | `backend/api_gateway/app.py` | 695 | FastAPI 路由、会话管理、文件上传 |
| **日志** | `backend/logging/logger.py` | 425 | 服务日志 + 会话日志 |
| | `backend/logging/session_store.py` | 341 | 消息持久化（jsonl） |
| **运行时** | `backend/runtime/process_supervisor.py` | 172 | 子进程生命周期 |
| **MCP** | `mcp_service/server.py` | 266 | FastMCP 动态工具注册 + 转发 |
| | `mcp_service/raw_client.py` | 162 | Agent 侧 MCP 客户端 |
| | `mcp_service/graph_data_client.py` | ~100 | GraphData HTTP 客户端 |
| **图服务** | `graph_data_service/tool_catalog.py` | 582 | **单一真相源**：所有工具规格定义 |
| | `graph_data_service/compute.py` | 462 | NetworkX 算法执行层 |
| | `graph_data_service/graph_data.py` | 1005 | GraphData 通用图数据容器 |
| | `graph_data_service/session.py` | 283 | GraphSession 图状态容器 |
| | `graph_data_service/codec.py` | 286 | 序列化/反序列化 |
| | `graph_data_service/io.py` | 270 | 图文件 I/O |
| **GraphML** | `graph_data_service/graph_ml/module.py` | ~150 | GraphML 模块入口 |
| | `graph_data_service/graph_ml/config_catalog.py` | 704 | 训练/推理参数配置 |
| | `graph_data_service/graph_ml/models/` | 3 文件 | GCN / GAT / SAGE 模型 |
| | `graph_data_service/graph_ml/trainer/` | 3 文件 | 三种训练器 |
| | `graph_data_service/graph_ml/infer/` | 2 文件 | 推理执行器 |
| **前端** | `index.html` | ~1000 | Tailwind CSS + marked.js + mermaid.js |

### 1.3 现有能力盘点

| 能力 | 状态 | 详情 |
|------|------|------|
| **图工具** | 26 个 | 19 图算法 + 5 GraphML + 2 内部 |
| **Agent 推理** | ReAct 16 步 | 无显式规划，边推理边调工具 |
| **双协议** | 已完成 | Text Protocol（SFT 数据采集）+ Native FC（生产部署） |
| **长期记忆** | 已完成 | 知识条目 + Playbook + 异步更新 + 归档 |
| **Skill 注入** | 基础 | 仅 1 个 GraphML Skill，按工具集条件触发 |
| **GraphML** | 已内建 | GCN/GAT/SAGE，node_classification / link_prediction / pre_train |
| **会话管理** | 成熟 | 惰性恢复、日志持久化、idle eviction |
| **SSE 流式** | 已完成 | thinking / token / tool_call / tool_result / done / error |
| **多模型** | 已完成 | OpenAI / Anthropic / Qwen，会话内可切换 |

### 1.4 明确短板

| 短板 | 影响 |
|------|------|
| **无显式任务规划** | 复杂多步任务依赖 LLM 即时推理，容易迷失 |
| **算法覆盖不足**（19 个） | 用户遇到 Leiden / ego_graph / harmonic centrality 等需求时失败 |
| **记忆召回是关键词匹配** | 无法处理同义词和语义相似 |
| **工具全量注入** | Text 协议每轮 ~2000 token 工具描述，扩展后更严重 |
| **图数据不持久** | 服务重启丢失，无版本回退 |
| **无数据集概念** | 只有"会话图"，无法持续分析数据资产 |
| **无 RAG** | 无法利用外部文档知识 |
| **单用户** | default_user，记忆互相污染 |
| **前端图渲染弱** | mermaid.js 只能小图，无交互 |
| **无结构化报告** | 输出为即时 Markdown，难以回溯 |

---

## 二、核心目标与升级原则

### 2.1 总体目标

> 用 iGraph 的工程底座，吸收 YiGraph 的 AAG/DAG/算法知识方法论，将 iGraph 从"强工程的图工具型 Agent"进化为：
> **既强工程、又强分析规划、还自带图学习能力的完整图智能体平台。**

### 2.2 四条不可破坏的核心优势

| # | 核心优势 | 约束 |
|---|---------|------|
| 1 | **轻量部署** | 零外部数据库强依赖，`python agent_server.py` 一键启动 |
| 2 | **透明协议** | Text Protocol 双栈设计，SFT/RL 数据采集能力是差异化竞争点 |
| 3 | **服务边界清晰** | API Gateway / MCP / GraphData 三层分工不变，tool_catalog 单一真相源不变 |
| 4 | **GraphML 一体化** | 图分析 + 图学习在同一执行栈内，无序列化开销 |

### 2.3 吸收 YiGraph 的方向与边界

| 吸收点 | YiGraph 做法 | iGraph 适配方式 | 不做的事 |
|--------|-------------|-------------------|---------|
| 显式任务规划 | DAG + WorkflowStep | 轻量 Planner + Step List | 不引入完整 DAG 引擎 |
| 算法知识层 | algorithms.yaml（106 条） | algorithm_catalog.py，复用 tool_catalog 体系 | 不引入 Milvus 向量库 |
| 多数据类型建图 | DatasetManager + Text/Table Loader | 新增 data_pipeline 模块，保持服务化 | 不引入 Neo4j 作为默认依赖 |
| 分析报告化 | 步骤级输出 + LLM 汇总报告 | 结构化输出格式，可选 report 模式 | 不做重型报告模板引擎 |
| 算法防幻觉 | ExpertSearchEngine 向量索引 | Catalog 白名单约束 + 语义检索 | 不引入独立知识库服务 |

---

## 三、四阶段路线图总览

```
Phase 0 ───────────────────────────────────── 近期（1-2 周）
  基础增强：算法扩展 + 语义记忆 + 工具动态注入 + 图快照
  关键词：低成本、高收益、不改架构

Phase 1 ───────────────────────────────────── 中近期（2-4 周）
  Tool Agent → Plan-aware Agent
  关键词：显式规划、复杂任务成功率、前端可视

Phase 2 ───────────────────────────────────── 中期（3-5 周）
  会话图 → 数据集图平台
  关键词：数据资产、多模态建图、RAG

Phase 3 ───────────────────────────────────── 长期（按需排期）
  图分析平台 → 图分析 + 图学习 + 图查询统一平台
  关键词：GraphML 编排、可插拔后端、作业系统、SFT 导出
```

| 阶段 | 关键里程碑 | 预期收益 | 新增文件数 | 主要修改文件数 |
|------|----------|---------|-----------|-------------|
| **Phase 0** | 算法 50+；工具动态注入；语义记忆；图快照 | 覆盖面↑ token↓ 记忆↑ 持久化 | 3 | 5 |
| **Phase 1** | Planner 上线；计划可视；步骤追踪 | 复杂任务成功率↑ | 4 | 3 |
| **Phase 2** | Dataset Registry；Text/Table→Graph；RAG | 从"对话工具"→"分析平台" | 8 | 5 |
| **Phase 3** | GraphML 编排；Neo4j 可选；作业系统；SFT 导出 | 产品定位完整 | 10+ | 6+ |

### 依赖顺序

```
Phase 0.1 算法扩展 ─┐
Phase 0.2 算法目录 ─┼→ Phase 1 规划层（依赖 Catalog 做工具约束）
Phase 0.3 工具注入 ─┘       │
Phase 0.4 语义记忆 ──────────┤
Phase 0.5 图快照 ────────────┼→ Phase 2 数据集层（依赖快照机制）
                             │       │
                             └───────┼→ Phase 3 平台化
                                     │
                                     └→ Phase 3 大图/协作
```

---

## 四、Phase 0 — 基础增强（低成本高收益）

**目标**：在不改变主架构的前提下，补齐最明显的短板，为后续 Phase 1 规划层奠定基础。

---

### 0.1 算法库扩展

**现状问题**

当前图算法覆盖不足，用户遇到 harmonic centrality、k-core、Leiden 等常见需求时直接失败。算法覆盖度是用户第一印象的核心指标。

**核心组织原则：按功能归类，算法作为参数区分**

新增算法**不是每个算法一个工具**，而是按照工具功能进行归类组织。功能相同、只是算法实现不同的方法，统一归入同一个工具，具体采用哪一种算法则通过工具参数（`enum`）进行区分和传递。

这是当前 `tool_catalog.py` 已有的设计模式：

```
现有示例：
  node_centrality 工具
    └─ centrality_type: enum["degree", "betweenness", "closeness", "pagerank", "eigenvector"]

  community_detection 工具
    └─ algorithm: enum["louvain", "greedy_modularity", "label_propagation"]
```

新增算法优先通过**扩展已有工具的 enum 参数**来实现，只有在功能类型确实全新时才新建工具。

**扩展清单**

#### A. 扩展已有工具的 enum 参数（新增算法，不新增工具）

| 已有工具 | 当前 enum 值 | 新增 enum 值 | 新增数量 |
|---------|-------------|-------------|---------|
| `node_centrality` | degree, betweenness, closeness, pagerank, eigenvector | **harmonic, katz, load, voterank, percolation** | 5 |
| `community_detection` | louvain, greedy_modularity, label_propagation | **leiden, girvan_newman, asyn_fluidc, label_propagation_asyn** | 4 |
| `shortest_path` | （当前无 algorithm 参数） | 考虑新增 `algorithm` 参数支持 **yen_k_shortest** 等变体 | 1 |

#### B. 新增工具（功能类型全新）

| 新增工具名 | 功能 | 参数设计 | 包含算法数 |
|-----------|------|---------|-----------|
| `subgraph_extraction` | 子图提取与过滤 | `method: enum["ego_graph", "k_core", "k_truss", "k_corona", "node_subset"]` + 对应参数 | 5 |
| `link_prediction_score` | 链路预测相似性评分 | `method: enum["jaccard", "adamic_adar", "resource_allocation", "preferential_attachment"]` | 4 |
| `path_analysis` | 路径分析（补充 shortest_path 未覆盖的场景） | `method: enum["all_simple_paths", "has_path", "average_shortest_path_length"]` | 3 |
| `clique_analysis` | 团分析 | `method: enum["find_cliques", "clique_number", "number_of_cliques"]` | 3 |
| `reachability` | 可达性分析 | `method: enum["transitive_closure", "descendants", "ancestors"]` | 3 |
| `global_metrics` | 全局图指标 | `metric: enum["density", "diameter", "radius", "transitivity", "average_clustering", "degree_assortativity"]` | 6 |

#### C. 扩展汇总

| 类别 | 方式 | 新增算法数 |
|------|------|-----------|
| 扩展已有工具 enum | 修改 `node_centrality` / `community_detection` / `shortest_path` | **10** |
| 新增工具 | 6 个新工具 | **24** |
| **合计** | | **34 个新增算法** |

最终工具数：19（已有）+ 6（新增工具）= **25 个图工具**，但算法覆盖从 ~19 种扩展到 **53 种**。工具数保持精简，算法覆盖大幅提升——这正是参数化归类的优势。

**扩展步骤**

对于**扩展已有工具 enum**：
1. `graph_data_service/compute.py` — 在已有方法中增加分支逻辑
2. `graph_data_service/tool_catalog.py` — 修改对应工具的 `enum` 列表 + 更新 `description`

对于**新增工具**：
1. `graph_data_service/compute.py` — 新增实现方法
2. `graph_data_service/tool_catalog.py` — 新增一条工具 spec（含 `method` enum 参数）

两种方式的共同步骤：
3. `tests/servers/test_graph_data_app.py` — 新增对应测试用例

MCP 自动注册、system prompt 自动更新，**无需改动其他文件**。

**工具设计示例**

以新增工具 `subgraph_extraction` 为例：

```python
# tool_catalog.py 中新增
{
    "name": "subgraph_extraction",
    "description": (
        "从当前图中提取子图。\n\n"
        "参数:\n"
        '- method:\n'
        '  - "ego_graph": 提取某节点的 k 阶邻域子图；\n'
        '  - "k_core": 提取 k-核子图；\n'
        '  - "k_truss": 提取 k-桁架子图；\n'
        '  - "k_corona": 提取 k-冠子图；\n'
        '  - "node_subset": 按给定节点列表提取诱导子图。\n'
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["ego_graph", "k_core", "k_truss", "k_corona", "node_subset"],
                "description": "子图提取方法",
            },
            "node": {"type": "string", "description": "中心节点（ego_graph 用）"},
            "k": {"type": "integer", "description": "k 值参数", "default": 2},
            "nodes": {"type": "array", "items": {"type": "string"}, "description": "节点列表（node_subset 用）"},
        },
        "required": ["method"],
    },
    "category": "compute",
}
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/compute.py` | 修改：已有方法扩展分支 + 新增 6 个方法 |
| `graph_data_service/tool_catalog.py` | 修改：3 个已有工具扩展 enum + 新增 6 条工具 spec |
| `tests/servers/test_graph_data_app.py` | 修改：新增至少 20 个测试用例 |

**验收标准**

- [ ] 算法覆盖 ≥ 50 种（含 enum 参数变体）
- [ ] 工具总数保持精简（图工具 ≤ 30）
- [ ] 所有新增/扩展工具可被 MCP 自动注册
- [ ] Text Protocol 与 Native FC 均可调用
- [ ] 新增算法自动化测试覆盖率 ≥ 60%

---

### 0.2 Algorithm Catalog（算法知识层）

**现状问题**

`tool_catalog.py` 只有工具调用规格（参数 schema + 描述），没有算法的语义元数据（适用场景、任务类型映射、输出类型）。Phase 1 Planner 在分配工具时缺乏知识支撑，容易幻觉调用不存在的工具或参数。

**方案**

新增 `graph_data_service/algorithm_catalog.py`，为每个**具体算法**补充语义元数据，供 Planner 和 ToolSelector 消费。

**与 tool_catalog 的关系**

由于 0.1 中采用了"按功能归类、算法作为参数"的工具组织模式，Algorithm Catalog 的粒度需要比 tool_catalog 更细——**每条记录对应一个具体算法（即工具 + 参数值的组合），而非一个工具**。

```
tool_catalog（粗粒度）         algorithm_catalog（细粒度）
─────────────────────         ──────────────────────────
node_centrality 工具    ──→   degree 算法条目
                              betweenness 算法条目
                              closeness 算法条目
                              pagerank 算法条目
                              eigenvector 算法条目
                              harmonic 算法条目（新增）
                              katz 算法条目（新增）
                              ...

community_detection 工具 ──→  louvain 算法条目
                              greedy_modularity 算法条目
                              leiden 算法条目（新增）
                              ...
```

**数据结构**

```python
# graph_data_service/algorithm_catalog.py

@dataclass
class AlgorithmMeta:
    algorithm_id: str                # 算法唯一标识，如 "harmonic"
    tool_name: str                   # 所属工具名，如 "node_centrality"
    param_key: str                   # 工具中的参数名，如 "centrality_type"
    param_value: str                 # 参数值，如 "harmonic"
    task_types: list[str]            # 任务分类标签
    description_short: str           # 一句话描述（用于 Planner context）
    suitable_graph_types: list[str]  # undirected / directed / weighted / ...
    output_type: str                 # node_ranking / node_partition / scalar / path / subgraph
    typical_use_cases: list[str]     # 典型场景描述
    aliases: list[str]               # 别名（用于语义匹配和 Planner 约束）
    complexity: str                  # O(n) / O(n*m) / O(n^2) 等

    def to_tool_call(self) -> dict:
        """生成 Planner 可直接使用的工具调用建议"""
        return {
            "tool": self.tool_name,
            "params": {self.param_key: self.param_value}
        }

ALGORITHM_CATALOG: list[AlgorithmMeta] = [
    # ─── node_centrality 工具下的算法 ───
    AlgorithmMeta(
        algorithm_id="pagerank",
        tool_name="node_centrality",
        param_key="centrality_type",
        param_value="pagerank",
        task_types=["find_important_nodes", "ranking"],
        description_short="基于随机游走的节点重要性排序",
        suitable_graph_types=["directed", "undirected"],
        output_type="node_ranking",
        typical_use_cases=["网页排名", "关键人物发现", "影响力传播分析"],
        aliases=["pagerank", "pr", "页面排名"],
        complexity="O(n + m)"
    ),
    AlgorithmMeta(
        algorithm_id="harmonic",
        tool_name="node_centrality",
        param_key="centrality_type",
        param_value="harmonic",
        task_types=["find_important_nodes", "accessibility"],
        description_short="基于调和距离的节点中心性，适合非连通图",
        suitable_graph_types=["undirected", "directed"],
        output_type="node_ranking",
        typical_use_cases=["非连通网络的关键节点", "信息可达性分析"],
        aliases=["harmonic", "调和中心性"],
        complexity="O(n * m)"
    ),
    # ─── community_detection 工具下的算法 ───
    AlgorithmMeta(
        algorithm_id="louvain",
        tool_name="community_detection",
        param_key="algorithm",
        param_value="louvain",
        task_types=["community_detection", "clustering"],
        description_short="基于模块度优化的社区发现",
        suitable_graph_types=["undirected", "weighted_undirected"],
        output_type="node_partition",
        typical_use_cases=["社交网络群体划分", "生物网络模块识别"],
        aliases=["louvain", "社区发现", "模块度"],
        complexity="O(n*log(n))"
    ),
    # ─── 独立工具（无参数区分）───
    AlgorithmMeta(
        algorithm_id="shortest_path",
        tool_name="shortest_path",
        param_key="",                # 独立工具，无算法参数
        param_value="",
        task_types=["find_path", "routing"],
        description_short="计算两节点间的最短路径",
        suitable_graph_types=["directed", "undirected", "weighted"],
        output_type="path",
        typical_use_cases=["路径规划", "社交距离计算"],
        aliases=["最短路径", "dijkstra"],
        complexity="O(n + m)"
    ),
    # ... 每个具体算法一条
]
```

**TASK_TYPE_MAP（任务类型 → 推荐算法调用）**

与 Algorithm Catalog 对齐，映射值使用 `algorithm_id`（而非工具名），Planner 通过 `AlgorithmMeta.to_tool_call()` 转换为实际工具调用。

```python
TASK_TYPE_MAP: dict[str, list[str]] = {
    "find_important_nodes":   ["pagerank", "betweenness", "eigenvector", "harmonic", "katz"],
    "detect_communities":     ["louvain", "greedy_modularity", "leiden", "girvan_newman"],
    "find_path":              ["shortest_path", "all_shortest_paths", "has_path", "all_simple_paths"],
    "analyze_structure":      ["graph_connectivity", "k_core", "find_cycles"],
    "predict_links":          ["jaccard", "adamic_adar", "resource_allocation", "preferential_attachment"],
    "extract_subgraph":       ["ego_graph", "k_core", "k_truss", "node_subset"],
    "global_statistics":      ["density", "diameter", "transitivity", "average_clustering", "degree_assortativity"],
    "analyze_cliques":        ["find_cliques", "clique_number"],
    "ml_node_classification": ["train", "infer"],
    "ml_link_prediction":     ["train", "infer"],
    "ml_embedding":           ["pre_train"],
}

# 辅助函数：从 algorithm_id 获取完整调用建议
def resolve_tool_call(algorithm_id: str) -> dict:
    """Planner 用：algorithm_id → {tool, params}"""
    for meta in ALGORITHM_CATALOG:
        if meta.algorithm_id == algorithm_id:
            return meta.to_tool_call()
    raise ValueError(f"Unknown algorithm: {algorithm_id}")
```

**约束机制**

Planner 构建计划时：
1. 将 `TASK_TYPE_MAP` 注入 LLM context，约束其只使用已知的 `algorithm_id`
2. Plan 中每步的 `candidate_algorithms` 字段填写 `algorithm_id`（如 `"pagerank"`）
3. Plan Executor 通过 `resolve_tool_call()` 将 `algorithm_id` 转换为实际工具调用（如 `node_centrality(centrality_type="pagerank")`）
4. 若 `algorithm_id` 不在 catalog 中，直接拒绝，**从源头防止幻觉**

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/algorithm_catalog.py` | **新增**：AlgorithmMeta + ALGORITHM_CATALOG + TASK_TYPE_MAP + resolve_tool_call() |
| `graph_data_service/tool_catalog.py` | 不改动，保持单一真相源 |

**验收标准**

- [ ] 每个具体算法（含 enum 参数变体）在 catalog 中均有独立条目
- [ ] `resolve_tool_call()` 对所有 algorithm_id 返回正确的 `{tool, params}`
- [ ] TASK_TYPE_MAP 覆盖 ≥ 10 种任务类型
- [ ] 导入无报错，数据结构可被 JSON 序列化

---

### 0.3 工具动态语义注入（ToolSelector）

**现状问题**

Text Protocol 模式下，每轮对话将全部工具的完整 spec 注入 system prompt。当前 19 个工具约 ~2000 token，扩展到 50+ 后问题更突出，长对话 token 消耗极高。

**方案**

新增 `backend/agent/tool_selector.py`，启动时对所有工具 description 做 embedding，每轮对话前语义检索 top-K 相关工具注入。

```python
# backend/agent/tool_selector.py

class ToolSelector:
    """基于语义相似度的工具检索器"""

    def __init__(self, all_tools: list[dict]):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [t["name"] + ": " + t["description"] for t in all_tools]
        self._matrix = self._model.encode(texts, normalize_embeddings=True)
        self._tools = all_tools

    def select(self, user_query: str, k: int = 12) -> list[dict]:
        q_emb = self._model.encode([user_query], normalize_embeddings=True)
        scores = (q_emb @ self._matrix.T).flatten()
        top_idx = scores.argsort()[-k:][::-1]
        # 置信度兜底：max_score < 0.25 则返回全集
        if scores[top_idx[0]] < 0.25:
            return self._tools
        return [self._tools[i] for i in top_idx]
```

Embedding 模型：`sentence-transformers/all-MiniLM-L6-v2`（本地，内存 <200MB，推理 <5ms）。iGraph 启动时一次性加载，所有 session 共享同一实例。

**降级策略**

- 若 `sentence-transformers` 未安装，自动回退到全量注入（当前行为）
- 若 max_score < 0.25（低置信度），回退到全量注入
- Native FC 模式不受影响（工具通过 `tools` 参数传递，无 prompt 注入）

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/tool_selector.py` | **新增**：语义工具检索器 |
| `backend/agent/context_manager.py` | 修改：调用 ToolSelector 替换全量注入 |
| `backend/agent/igraph.py` | 修改：初始化时构建 ToolSelector 实例 |

**验收标准**

- [ ] Text 协议模式平均工具 prompt token 降低 ≥ 40%
- [ ] 不影响现有核心任务成功率（用现有测试验证）
- [ ] 未安装 sentence-transformers 时自动降级，不报错

---

### 0.4 记忆召回语义化

**现状问题**

`memory_engine.py` 的 `build_memory_prompt()` 使用关键词字符串匹配评分（confidence × category_weight × keyword_overlap），无法处理同义词和语义相似（如用户说"关键人物"，记忆里存的是"核心节点"，无法匹配）。

**方案**

复用 0.3 中已引入的 embedding 模型，为记忆条目构建向量索引。

```python
# backend/agent/memory_engine.py 中新增

class SemanticMemoryIndex:
    """语义记忆索引，复用 ToolSelector 的 embedding 模型"""

    def __init__(self, model, memory_items: list[dict]):
        texts = [f"{m['key']}: {m['value']}" for m in memory_items]
        self._matrix = model.encode(texts, normalize_embeddings=True) if texts else np.empty((0, 384))
        self._items = memory_items

    def recall(self, query: str, model, top_k: int = 8) -> list[dict]:
        if len(self._items) == 0:
            return []
        q_emb = model.encode([query], normalize_embeddings=True)
        scores = (q_emb @ self._matrix.T).flatten()
        top_idx = scores.argsort()[-top_k:][::-1]
        return [self._items[i] for i in top_idx if scores[i] > 0.15]

    def add(self, item: dict, model):
        """增量添加，不重建全量"""
        text = f"{item['key']}: {item['value']}"
        new_emb = model.encode([text], normalize_embeddings=True)
        self._matrix = np.vstack([self._matrix, new_emb]) if len(self._matrix) > 0 else new_emb
        self._items.append(item)
```

索引在 `MemoryEngine` 初始化时构建，每次 memory update 后增量更新（追加新向量，不重建全量）。保留关键词打分作为 fallback。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/memory_engine.py` | 修改：新增 `SemanticMemoryIndex`，`build_memory_prompt()` 使用语义召回 |
| `backend/agent/igraph.py` | 修改：共享 embedding 模型实例给 MemoryEngine |

**验收标准**

- [ ] "关键人物" 能召回含"核心节点"的记忆条目
- [ ] 记忆召回延迟 < 20ms
- [ ] 未安装 sentence-transformers 时回退到关键词匹配

---

### 0.5 图快照持久化与版本回退

**现状问题**

图数据只在内存中（GraphSession），服务重启即丢失。没有版本历史，误操作（如 `to_undirected` / `clear_graph`）不可撤销。

**方案**

在 GraphData Service 中增加图快照写入与恢复机制。

**存储结构**

```
uploads/{session_id}/
  graph_current.npz          # 当前图快照
  snapshots/
    v001.npz                 # 历史版本（最多保留 5 个）
    v002.npz
    ...
```

**关键行为**

1. **自动快照**：每次 `load_graph` / `to_undirected` / `clear_graph` 等变更操作后，将当前 GraphSession 图序列化为 `.npz` 快照
2. **启动恢复**：GraphData Service 启动时扫描 `uploads/` 目录，自动恢复各 session 最新快照到内存
3. **版本回退**：新增工具 `undo_graph`，将当前图回滚到上一个版本快照
4. **元数据增强**：`session_meta.json` 补充 `node_count`, `edge_count`, `is_directed`, `has_weights`, `graph_version`

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/session.py` | 修改：新增 `save_snapshot()` / `load_snapshot()` / `undo()` |
| `graph_data_service/app.py` | 修改：变更操作后触发快照；启动时扫描恢复 |
| `graph_data_service/tool_catalog.py` | 修改：新增 `undo_graph` 工具 spec |
| `graph_data_service/compute.py` | 修改：新增 `undo_graph` 实现 |
| `backend/logging/session_store.py` | 修改：meta 补充图统计字段 |

**验收标准**

- [ ] 服务重启后图可自动恢复
- [ ] `undo_graph` 可回退到上一版本
- [ ] 快照数量不超过 5 个（自动轮转）
- [ ] `session_meta.json` 包含图统计信息

---

### Phase 0 里程碑

| 里程碑 | 包含项 | 验收条件 |
|--------|-------|---------|
| **M0.1** | 算法扩展 + Algorithm Catalog | 工具数 ≥ 50，catalog 完整 |
| **M0.2** | 工具动态注入 + 语义记忆 | token 降低 ≥ 40%，语义召回生效 |
| **M0.3** | 图快照恢复 + undo | 重启后恢复，undo 可用 |

---

## 五、Phase 1 — 规划层落地

**目标**：在不改动现有 MCP / GraphData / GraphML 结构的前提下，让 iGraph 具备显式任务规划能力，从纯 ReAct Agent 升级为 Plan-aware Agent。这是整个升级计划中**最关键的一阶段**。

**设计思路**：不直接上重型 DAG 引擎，而是分两步走——先实现轻量 Step Plan（本阶段），稳定后再扩展为轻量 DAG（后续按需）。

---

### 1.1 Query Complexity Detector（子 Agent 方案）

**现状问题**

所有用户输入都走同一个 ReAct 主循环。简单问题（"这个图有多少节点？"）不需要规划，复杂问题（"找出最可疑社区并分析内部关键节点"）却缺乏结构化编排。需要一个判断机制决定是否进入 Plan 模式。

**为什么不用启发式规则**

基于关键词/正则的启发式方案存在根本缺陷：
- **语义理解不足**：规则无法判断"分析这个图的风险"是单步还是多步——取决于图的复杂度和"风险"的定义
- **多语言脆弱**：中英文混用、口语化表达、领域术语都会击穿规则
- **维护成本高**：每遇到新的漏判/误判都需要手工加规则，无法泛化

真正需要判断的核心问题是：**完成这个任务是否需要多轮连续工具调用？** 这是一个语义判断，应交给 LLM。

**方案**

新增 `backend/agent/query_analyzer.py`，使用一个轻量**子 Agent**（单次 LLM 调用）判断查询是否需要多轮连续工具调用。

```python
# backend/agent/query_analyzer.py

COMPLEXITY_JUDGE_PROMPT = """你是一个图分析任务复杂度判断器。

用户将提出一个关于图数据的分析请求。你需要判断完成这个请求是否需要**多轮连续工具调用**（即前一个工具的结果是后一个工具的输入）。

判断依据：
- 单轮工具调用：只需调用一个工具即可直接回答（如"图有多少节点"→ get_graph_info），或单轮可并列执行的多工具调用
- 多轮连续调用：需要先调工具 A 获取中间结果，再将结果传给工具 B（如"找到最大社区中最关键的节点"→ 先社区检测，再对社区子图做中心性分析）

当前图状态：
{graph_context}

可用工具列表：
{tool_names}

用户请求：
{user_query}

请严格按以下 JSON 格式回答，不要输出其他内容：
{{"needs_plan": true/false, "reason": "一句话解释", "estimated_tool_calls": 数字}}
"""

class QueryAnalyzer:
    """基于子 Agent 的查询复杂度分析器"""

    def __init__(self, llm_factory):
        """
        Args:
            llm_factory: 创建轻量 LLM 实例的工厂函数
                         优先使用小模型（如 haiku / qwen-flash）降低延迟和成本
        """
        self._llm = llm_factory(purpose="complexity_judge")

    async def should_plan(
        self,
        message: str,
        graph_context: str,
        tool_names: list[str],
    ) -> tuple[bool, str]:
        """
        判断是否应进入规划模式。

        Returns:
            (should_plan: bool, reason: str)
        """
        prompt = COMPLEXITY_JUDGE_PROMPT.format(
            graph_context=graph_context,
            tool_names=", ".join(tool_names),
            user_query=message,
        )

        try:
            response = await self._llm.ainvoke(prompt)
            result = json.loads(self._extract_json(response))
            needs_plan = result.get("needs_plan", False)
            reason = result.get("reason", "")
            estimated = result.get("estimated_tool_calls", 1)

            # 保守策略：estimated_tool_calls >= 3 才进入 Plan 模式
            # 2 次调用仍可由 ReAct 循环自然处理
            return (needs_plan and estimated >= 3), reason

        except (json.JSONDecodeError, KeyError, Exception):
            # 解析失败 → 降级为 Direct 模式（保守默认）
            return False, "complexity judge failed, fallback to direct"

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 输出中提取 JSON 块"""
        # 处理 ```json ... ``` 包裹的情况
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if match:
                return match.group(1)
        # 直接尝试找 { ... }
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return match.group(0) if match else "{}"
```

**子 Agent 模型选择**

复杂度判断是一个轻量任务（输入短、输出固定 JSON），应使用**最小可用模型**以控制延迟和成本：

| 优先级 | 模型 | 预期延迟 | 说明 |
|--------|------|---------|------|
| 1 | 当前会话模型的 mini/flash 变体 | ~200ms | 如 Qwen-Flash、Haiku |
| 2 | 当前会话模型本身 | ~500ms | 如果无更小模型可用 |

通过 `llm_factory(purpose="complexity_judge")` 由 `provider_gateway.py` 路由到合适模型。

**判断逻辑核心**

子 Agent 关注的核心判据是 **`estimated_tool_calls`**（预估需要的连续工具调用次数）：

| estimated_tool_calls | 行为 |
|---------------------|------|
| 1-2 | Direct 模式（ReAct 循环足以处理） |
| ≥ 3 | Plan 模式（需要显式规划步骤顺序和依赖） |

这比启发式规则更准确——同样是"分析这个图"，子 Agent 可以根据可用工具列表和图状态判断具体需要几步。

**上下文注入**

子 Agent 接收三项关键信息：
1. **graph_context**：当前图的摘要（节点数、边数、是否有向、是否有权重），帮助判断任务复杂度
2. **tool_names**：可用工具列表，帮助判断任务是否可拆分为多步
3. **user_query**：用户原始输入

不注入完整 tool spec（节省 token），只需工具名列表即可。

**降级策略**

- LLM 调用超时（>3s）→ 降级为 Direct 模式
- JSON 解析失败 → 降级为 Direct 模式
- `sentence-transformers` 未安装导致模型不可用 → 降级为 Direct 模式
- **保守默认**：任何异常都走 Direct，确保不阻塞用户

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/query_analyzer.py` | **新增**：QueryAnalyzer + COMPLEXITY_JUDGE_PROMPT |
| `backend/agent/igraph.py` | 修改：主循环入口调用 `should_plan()` |
| `backend/agent/provider_gateway.py` | 修改：支持 `purpose="complexity_judge"` 路由到小模型 |

---

### 1.2 Plan Builder（轻量 Planner）

**现状问题**

即使判断了需要规划，当前也没有"生成结构化计划"的能力。LLM 只能在推理过程中隐式地边想边做。

**方案**

新增 `backend/agent/planner.py`，调用一次 LLM 生成结构化 JSON 计划。

**核心数据结构**

```python
# backend/agent/planner.py

@dataclass
class PlanStep:
    step_id: int
    goal: str                    # 子问题/子目标描述
    candidate_tools: list[str]   # 候选工具名（来自 algorithm_catalog）
    depends_on: list[int]        # 依赖的 step_id 列表
    status: str = "pending"      # pending | running | done | failed | skipped
    result_summary: str = ""     # 执行后填入

@dataclass
class AnalysisPlan:
    plan_id: str                 # UUID
    original_query: str
    steps: list[PlanStep]
    mode: str = "planned"        # planned | direct
    created_at: str = ""
    completed_at: str = ""

class Planner:
    """轻量计划生成器"""

    def __init__(self, algorithm_catalog, tool_catalog):
        self._algo_catalog = algorithm_catalog
        self._tool_catalog = tool_catalog
        # 可用工具白名单
        self._valid_tool_names = {t["name"] for t in tool_catalog}

    async def build_plan(self, query: str, graph_info: dict, llm) -> AnalysisPlan:
        """调用一次 LLM，生成结构化计划"""
        prompt = self._build_plan_prompt(query, graph_info)
        response = await llm.ainvoke(prompt)
        plan = self._parse_plan(response, query)
        # 校验：过滤不在白名单中的工具
        for step in plan.steps:
            step.candidate_tools = [t for t in step.candidate_tools if t in self._valid_tool_names]
        return plan

    def _build_plan_prompt(self, query: str, graph_info: dict) -> str:
        """构建计划生成 prompt，注入 TASK_TYPE_MAP + 可用工具列表"""
        # 包含：用户问题、当前图信息、TASK_TYPE_MAP、可用工具名列表
        # 要求 LLM 输出严格 JSON 格式的步骤列表
        ...

    def _parse_plan(self, response: str, query: str) -> AnalysisPlan:
        """解析 LLM 输出为 AnalysisPlan，容错处理"""
        ...
```

**计划生成 Prompt 核心要求**

```
你是图分析规划师。根据用户问题和当前图的信息，生成分步分析计划。

可用任务类型与推荐算法：
{TASK_TYPE_MAP 序列化}

可用工具列表：
{tool_names 列表}

约束：
1. candidate_tools 只能使用上述列表中的工具名
2. 每步只做一件事
3. 标注步骤间依赖关系
4. 通常 2-6 步即可

输出格式：严格 JSON
{"steps": [{"step_id": 1, "goal": "...", "candidate_tools": [...], "depends_on": []}]}
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/planner.py` | **新增**：Planner + PlanStep + AnalysisPlan |

---

### 1.3 Plan Executor

**现状问题**

有了计划后，需要按步骤顺序执行，每步复用现有的 MCP 工具调用链路，而不是另起一套执行机制。

**方案**

在 `backend/agent/igraph.py` 中新增 `_run_with_plan()` 方法，作为 Plan 模式的执行入口。

```python
# backend/agent/igraph.py 中新增

async def _run_with_plan(self, plan: AnalysisPlan):
    """按计划顺序执行，每步委托给现有 ReAct 单步循环"""
    for step in plan.steps:
        # 检查依赖是否完成
        if not self._deps_satisfied(step, plan):
            step.status = "skipped"
            continue

        step.status = "running"
        yield {"event": "step_start", "step_id": step.step_id, "goal": step.goal}

        # 构造单步 prompt：告知 LLM 当前步骤目标和候选工具
        step_prompt = self._build_step_prompt(step, plan)

        # 复用现有 ReAct 循环（限制步数为 4，避免单步过长）
        result = await self._execute_single_step(step_prompt, max_steps=4)

        step.result_summary = result.summary
        step.status = "done" if result.success else "failed"
        yield {"event": "step_done", "step_id": step.step_id, "status": step.status,
               "summary": step.result_summary}

    # 最终由 LLM 汇总所有步骤结果
    final_answer = await self._summarize_plan_results(plan)
    yield {"event": "plan_complete", "answer": final_answer}
```

**核心原则**

- **Plan 只负责定义步骤**，具体工具执行仍由现有 iGraph 的 ReAct 循环完成
- **协议层和 MCP 层不变**，Plan Executor 是 iGraph 内部的编排逻辑
- 每步限制最大 4 次工具调用，避免单步失控
- 失败步骤标记为 `failed`，后续步骤若依赖它则自动 `skipped`
- 所有步骤完成后，由 LLM 汇总报告

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/igraph.py` | 修改：新增 `_run_with_plan()` / `_execute_single_step()` / `_summarize_plan_results()` |

---

### 1.4 Plan Store + SSE 事件

**现状问题**

计划需要持久化以便回溯，同时前端需要实时感知规划和执行进度。

**方案 A：Plan 持久化**

```
logs/{session_id}/
  plan_{plan_id}.json     # AnalysisPlan 序列化
```

每次计划生成或步骤状态变更时，自动写入/更新文件。

**方案 B：SSE 事件扩展**

在现有 SSE 事件流（thinking / token / tool_call / tool_result / done / error）基础上，新增：

| 事件 | 触发时机 | payload |
|------|---------|---------|
| `plan_ready` | 计划生成完毕 | `{plan_id, steps: [{step_id, goal, candidate_tools, depends_on}]}` |
| `step_start` | 开始执行某步 | `{step_id, goal}` |
| `step_done` | 某步执行完毕 | `{step_id, status, summary}` |
| `plan_complete` | 全部步骤完成 | `{plan_id, answer}` |

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/logging/session_store.py` | 修改：新增 plan 文件写入 |
| `backend/api_gateway/app.py` | 修改：SSE 事件流支持新事件类型 |

---

### 1.5 前端计划展示

**现状问题**

用户看不到 Agent 的执行计划，只能看到逐条的流式文本输出。

**方案**

在 `index.html` 中新增"分析计划"面板，监听 SSE 新事件，渲染步骤列表。

**UI 设计**

```
┌──────────────────────────────────────────┐
│ 📋 分析计划                              │
│ ┌──────────────────────────────────────┐ │
│ │ ✅ 步骤 1: 计算所有节点的中心性      │ │
│ │    工具: pagerank, betweenness       │ │
│ │    结果: 节点A PageRank最高(0.12)    │ │
│ ├──────────────────────────────────────┤ │
│ │ 🔄 步骤 2: 检测社区结构              │ │
│ │    工具: louvain_communities         │ │
│ │    执行中...                         │ │
│ ├──────────────────────────────────────┤ │
│ │ ⏳ 步骤 3: 分析核心社区内部节点      │ │
│ │    依赖: 步骤 1, 步骤 2              │ │
│ └──────────────────────────────────────┘ │
└──────────────────────────────────────────┘
```

**实现**

- 监听 `plan_ready` → 渲染步骤列表（全部 pending 状态）
- 监听 `step_start` → 对应步骤变为"执行中"（蓝色 spinner）
- 监听 `step_done` → 对应步骤变为完成（绿色 ✅）或失败（红色 ❌）+ 结果摘要
- 监听 `plan_complete` → 标记计划完成

第一版为**只读展示**，后续版本再支持用户编辑计划。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `index.html` | 修改：新增计划面板 HTML + JS 事件监听 |

---

### 1.6 主循环改造总览

改造后的 `igraph.py` 主循环：

```
用户消息进入
    ↓
QueryAnalyzer.should_plan(message, graph_context, tool_names)
    │                    （子 Agent：轻量 LLM 判断是否需要多轮连续工具调用）
    │
    ├─→ (False, _) → 原有 ReAct 主循环（不受影响）
    │
    └─→ (True, reason) ↓
                        Planner.build_plan(query, graph_info)
                            ↓
                        SSE 推送 plan_ready 事件给前端
                            ↓
                        PlanExecutor._run_with_plan(plan)
                            ↓ 逐步执行
                            ├→ step_start → 复用 ReAct 单步 → step_done
                            ├→ step_start → 复用 ReAct 单步 → step_done
                            └→ ...
                            ↓
                        LLM 汇总所有步骤结果 → 最终报告
                            ↓
                        SSE 推送 plan_complete + done 事件
    ↓
异步触发 MemoryEngine.maybe_update_from_turn()
```

**LLM 调用成本分析**

| 阶段 | 模型 | 调用次数 | 说明 |
|------|------|---------|------|
| 复杂度判断 | 小模型（Flash/Haiku） | 1 | 输入短、输出固定 JSON |
| 计划生成 | 主模型 | 1 | 仅复杂任务触发 |
| 步骤执行 | 主模型 | 每步 1-4 次 | 复用现有 ReAct 循环 |
| 结果汇总 | 主模型 | 1 | 仅 Plan 模式 |

简单任务额外成本：仅 1 次小模型调用（~200ms, ~100 token）。

---

### Phase 1 验收标准

- [ ] 简单问题（"图有多少节点"）仍走 Direct 模式，无性能回退
- [ ] 复杂问题（"找出最重要社区并分析关键节点"）自动进入 Plan 模式
- [ ] 子 Agent 判断延迟 < 500ms（使用小模型时）
- [ ] 子 Agent 调用失败时自动降级为 Direct 模式，不阻塞用户
- [ ] 前端可展示步骤列表和实时执行状态
- [ ] 计划 JSON 持久化到 logs 目录
- [ ] 复杂任务成功率高于纯 ReAct 模式（人工评估 10 个 case）

### Phase 1 风险与控制

| 风险 | 控制 |
|------|------|
| 计划生成质量不稳定 | Algorithm Catalog 白名单约束 + JSON 格式校验 + 降级到 Direct |
| 计划执行与工具能力不一致 | 每步限制 max_steps=4，失败自动标记 |
| 子 Agent 复杂度判断不准 | 保守阈值（estimated_tool_calls ≥ 3 才触发）+ 任何异常降级 Direct |
| 子 Agent 增加延迟 | 使用小模型（Flash/Haiku），延迟 ~200ms，相比用户输入延迟可忽略 |
| 额外 LLM 调用成本 | 复杂度判断用小模型（成本极低）；Plan 生成只多一次主模型调用 |

---

## 六、Phase 2 — 数据集平台与建图管线

**目标**：将 iGraph 从"对话时加载一张图"升级为"对某个数据资产持续分析"，同时补充轻量 RAG，使系统从"对话工具"进化为"分析平台"。

---

### 2.1 Dataset Registry + 图持久化升级

**现状问题**

- 没有"数据集"概念，用户每次重新上传才能使用历史图
- Phase 0.5 的图快照只解决了"服务重启恢复"，但没有"数据资产"的抽象
- 用户无法跨会话复用同一张图

**方案**

在 `graph_data_service/` 下新增 `dataset_registry.py`，管理"数据资产"与"会话图"两层概念。

**存储结构**

```
datasets/
  {dataset_id}/
    meta.json                # 数据集名称、描述、创建时间、来源类型
    graph.npz                # 序列化图数据（原始资产）
    schema.json              # 字段定义（可选）
    versions/
      v001.npz               # 版本快照
      v002.npz

uploads/{session_id}/        # Phase 0.5 已有，增加 dataset 绑定
  graph_current.npz
  dataset_binding.json       # {dataset_id, dataset_version}
```

**关键数据结构**

```python
# graph_data_service/dataset_registry.py

@dataclass
class DatasetMeta:
    dataset_id: str
    name: str
    description: str
    source_type: str           # "graph" | "table" | "text"
    node_count: int
    edge_count: int
    is_directed: bool
    created_at: str
    updated_at: str
    versions: list[str]        # ["v001", "v002", ...]

class DatasetRegistry:
    """数据集注册中心"""

    def __init__(self, base_dir: str = "datasets"):
        self._base_dir = Path(base_dir)

    def create(self, name: str, description: str, graph_data) -> DatasetMeta: ...
    def list_datasets(self) -> list[DatasetMeta]: ...
    def get(self, dataset_id: str) -> DatasetMeta: ...
    def load_graph(self, dataset_id: str, version: str = "latest") -> GraphData: ...
    def delete(self, dataset_id: str) -> bool: ...
```

**新增工具**

| 工具名 | 功能 |
|--------|------|
| `list_datasets` | 列出所有已注册数据集 |
| `load_dataset` | 从数据集加载图到当前会话 |
| `save_as_dataset` | 将当前会话图保存为数据集 |

**会话-数据集绑定**

- 会话可绑定数据集版本：`bind_session_to_dataset(session_id, dataset_id, version)`
- 多个会话可绑定同一数据集的不同版本
- GraphML 训练时自动记录 `(dataset_id, dataset_version)` 到 artifact 元数据

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/dataset_registry.py` | **新增** |
| `graph_data_service/app.py` | 修改：新增 dataset 相关端点 |
| `graph_data_service/tool_catalog.py` | 修改：新增 3 条工具 spec |
| `graph_data_service/compute.py` | 修改：新增对应实现 |
| `mcp_service/graph_data_client.py` | 修改：新增 dataset HTTP 调用 |

**验收标准**

- [ ] 可创建、列出、加载、删除数据集
- [ ] 多会话可绑定同一数据集
- [ ] 重启服务后数据集仍可访问

---

### 2.2 Table → Graph 建图（优先实现，无 LLM 依赖）

**现状问题**

iGraph 只支持手动输入边列表或上传图文件。大量用户有"从 CSV 关系表建图"的需求（如交易记录表、社交关系表）。

**方案**

新增 `graph_data_service/data_pipeline/table_extractor.py`，支持从 CSV/Excel 表格直接构建图。

**流程**

```
用户上传 CSV + 指定字段映射
    ↓
TableExtractor.extract(file_path, config)
    ↓
  config = {
    "source_field": "from_account",    # 源节点列
    "target_field": "to_account",      # 目标节点列
    "weight_field": "amount",          # 权重列（可选）
    "node_attr_fields": ["type"],      # 节点属性列（可选）
    "edge_attr_fields": ["timestamp"], # 边属性列（可选）
    "directed": true
  }
    ↓
读取 CSV → 提取边列表 → 验证
    ↓
GraphBuilder.build(edges, config) → GraphData
    ↓
写入 GraphSession + 可选保存为 Dataset
```

**验证逻辑**

- 孤立节点比例 > 50% 时警告
- 重复边检测和去重
- 数据类型校验（权重必须为数值）

**新增工具**

| 工具名 | 功能 |
|--------|------|
| `build_graph_from_table` | 从 CSV/Excel 表格构建图 |

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/data_pipeline/__init__.py` | **新增** |
| `graph_data_service/data_pipeline/table_extractor.py` | **新增** |
| `graph_data_service/data_pipeline/graph_builder.py` | **新增** |
| `graph_data_service/tool_catalog.py` | 修改：新增工具 spec |
| `backend/api_gateway/app.py` | 修改：文件上传支持 `.csv` / `.xlsx` |

**验收标准**

- [ ] 上传 CSV 后可自动构建图
- [ ] 支持指定源/目标/权重列
- [ ] 构建结果可正常执行后续图算法
- [ ] 错误数据有清晰的错误提示

---

### 2.3 Text → Graph 建图（依赖 LLM，第二步实现）

**现状问题**

用户有"从文本中提取实体关系并构建知识图谱"的需求，但当前完全不支持。

**方案**

新增 `graph_data_service/data_pipeline/text_extractor.py`，利用 LLM 从文本中提取三元组。

**两阶段建图**（不让 LLM 直接生成整张图）

```
阶段一：结构提取
  TextExtractor.extract(text, llm)
    → LLM prompt: "从以下文本抽取实体关系三元组，返回 JSON..."
    → [(entity1, relation, entity2), ...]
    → 返回中间结构供用户确认

阶段二：图构建
  用户确认或修改中间结构
    → GraphBuilder.build(triples) → GraphData
    → 以 entity 为节点、relation 为边属性构建图
```

**LLM Prompt 设计**

```
从以下文本中抽取实体关系三元组。

要求：
1. entity 应为具体名词（人名、地名、组织名、概念名）
2. relation 应为动词或介词短语
3. 返回 JSON 数组格式

文本：
{text}

输出格式：
[{"source": "实体1", "relation": "关系", "target": "实体2"}, ...]
```

**新增工具**

| 工具名 | 功能 |
|--------|------|
| `build_graph_from_text` | 从文本提取实体关系并建图 |

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/data_pipeline/text_extractor.py` | **新增** |
| `graph_data_service/tool_catalog.py` | 修改：新增工具 spec |

**验收标准**

- [ ] 给定一段文本，能提取出合理的三元组
- [ ] 提取结果可转化为图并执行算法
- [ ] 复杂文本（>500 字）处理时间 < 30s

---

### 2.4 轻量 RAG 支持

**现状问题**

Agent 无法利用外部文档知识（图数据说明、行业报告、算法论文）。引入 Milvus 太重，不符合 iGraph 轻量化原则。

**方案**

在 GraphData Service 中集成基于文件系统 + numpy 的轻量向量检索，零外部数据库依赖。

**存储结构**

```
uploads/{session_id}/
  docs/
    {doc_id}.txt            # 原始文档（分块后）
    {doc_id}_chunks.json    # chunk 元数据
  doc_embeddings.npy        # 所有 chunk 的 embeddings 矩阵
  doc_index.json            # chunk_id → doc_id + offset + text 映射
```

**核心流程**

```
上传文档 → 分块（512 tokens/chunk，128 overlap）→ embedding → 存储
    ↓
Agent 调用 retrieve_context(query)
    → query embedding
    → cosine 检索 doc_embeddings.npy
    → 返回 top-5 相关 chunk 文本
    → Agent 将其作为 tool_observation 注入下一轮推理
```

**新增工具**

| 工具名 | 功能 |
|--------|------|
| `upload_document` | 上传文档到当前会话知识库 |
| `retrieve_context` | 语义检索相关文档片段 |

**embedding 模型**：复用 Phase 0.3 已引入的 `all-MiniLM-L6-v2`。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/rag.py` | **新增**：文档分块 + embedding + 检索 |
| `graph_data_service/tool_catalog.py` | 修改：新增 2 条工具 spec |
| `graph_data_service/compute.py` | 修改：新增对应实现 |
| `backend/api_gateway/app.py` | 修改：文档上传路由 |

**验收标准**

- [ ] 上传 PDF/TXT 后可检索相关内容
- [ ] 检索延迟 < 100ms
- [ ] 检索结果对 Agent 回答质量有明显提升（人工评估）
- [ ] 无外部数据库依赖

---

### ~~2.5 多用户记忆隔离~~ （暂缓，后续按需排期）

> 多用户记忆隔离（`user_id` 参数化 + 认证中间件 + 记忆目录路由）暂不纳入 Phase 2 范围。
> 当前以 `default_user` 单用户模式运行，待前面核心能力（规划层 + 数据集 + RAG）稳定后，再根据实际多用户需求推进。

---

### Phase 2 里程碑

| 里程碑 | 包含项 | 验收条件 |
|--------|-------|---------|
| **M2.1** | Dataset Registry + 图持久化 | 数据集 CRUD 可用，跨会话复用图 |
| **M2.2** | Table → Graph | CSV 上传并成功建图 |
| **M2.3** | Text → Graph + RAG | 文本建图 + 文档检索可用 |

---

## 七、Phase 3 — 平台化与生产化

**目标**：完成 GraphML 编排化、图后端可插拔、异步作业系统、结构化报告和 SFT 数据导出，使 iGraph 具备完整的企业级图分析 + 图学习平台能力。**本阶段各项可独立排期，按需推进。**

---

### 3.1 GraphML 编排化（从工具到规划节点）

**现状问题**

GraphML 目前作为独立工具集存在（`train`/`infer` 等），Planner 不会主动将其嵌入分析规划流程。例如"先训练节点嵌入，再用嵌入结果辅助社区检测"这类组合任务无法自动编排。

**方案**

在 Phase 1 的 Planner 基础上，扩展 Algorithm Catalog 的 `TASK_TYPE_MAP`，将 GraphML 任务纳入规划体系。

```python
# algorithm_catalog.py 追加

TASK_TYPE_MAP.update({
    "node_embedding":         ["pre_train"],
    "node_classification":    ["train(task=node_classification)", "infer"],
    "link_prediction":        ["train(task=link_prediction)", "infer"],
    "ml_assisted_analysis":   ["pre_train", "louvain_communities"],  # 组合模板
})
```

**模型版本一致性检查**

- 训练时记录 `(session_id, graph_version, dataset_id)` 到 artifact 元数据
- 推理时校验当前图版本是否匹配
- 不匹配则提示 Agent 重新训练或使用匹配版本

**Planner 增强**

- 识别当 step 涉及 GraphML 时，自动检查 `artifact_store` 中是否已有可复用模型
- 若有且版本匹配，跳过训练直接推理
- 若无，自动插入 `train` step 作为前置依赖

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/algorithm_catalog.py` | 修改：补充 ML 任务类型 |
| `backend/agent/planner.py` | 修改：ML step 识别 + 依赖自动插入 |
| `graph_data_service/graph_ml/artifact_store.py` | 修改：新增版本一致性校验 |

---

### 3.2 图后端可插拔（Neo4j Adapter）

**现状问题**

图数据完全内存 + NetworkX，无法处理百万级节点的大规模图，也不支持持久化图查询。

**方案**

在 GraphData Service 内部引入存储后端抽象层，对上层 Agent 和 MCP **完全透明**。

```python
# graph_data_service/storage/base.py

class GraphStorageBackend(ABC):
    @abstractmethod
    async def load(self, session_id: str) -> GraphData: ...
    @abstractmethod
    async def save(self, session_id: str, graph: GraphData): ...
    @abstractmethod
    async def query_subgraph(self, session_id: str, node_ids: list) -> GraphData: ...
    @abstractmethod
    async def exists(self, session_id: str) -> bool: ...

# graph_data_service/storage/memory_backend.py  （当前逻辑迁入，默认）
class MemoryBackend(GraphStorageBackend): ...

# graph_data_service/storage/neo4j_backend.py   （可选，配置启用）
class Neo4jBackend(GraphStorageBackend): ...
```

**关键原则**

- 算法执行层（`compute.py`）保持 NetworkX 不变
- Neo4j 后端在执行前按需拉取子图到内存
- 通过 `.env` 配置切换，默认 memory

```
GRAPH_STORAGE_BACKEND=memory   # 默认，零依赖
GRAPH_STORAGE_BACKEND=neo4j    # 需配置 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/storage/__init__.py` | **新增** |
| `graph_data_service/storage/base.py` | **新增** |
| `graph_data_service/storage/memory_backend.py` | **新增**（从 session.py 迁入） |
| `graph_data_service/storage/neo4j_backend.py` | **新增** |
| `graph_data_service/session.py` | 修改：持有 `StorageBackend` 实例 |
| `graph_data_service/app.py` | 修改：按配置实例化 backend |

---

### 3.3 异步作业管理系统

**现状问题**

GraphML 训练、大图社区检测等长时间操作在 HTTP 请求中同步执行，无法中断、无进度反馈、超时风险高。

**方案**

在 GraphData Service 中新增轻量作业系统（不引入 Celery，用 `asyncio.Task` + 内存队列实现）。

```python
# graph_data_service/job_manager.py

@dataclass
class Job:
    job_id: str
    session_id: str
    tool_name: str
    status: str         # queued | running | done | failed | cancelled
    progress: float     # 0.0 ~ 1.0
    message: str
    result: Any
    created_at: str

class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    async def submit(self, session_id: str, tool_name: str, fn, args) -> str:
        """提交异步作业，返回 job_id"""
        ...

    def status(self, job_id: str) -> Job: ...
    def cancel(self, job_id: str) -> bool: ...
    def list_jobs(self, session_id: str) -> list[Job]: ...
```

**进度推送**

长时间工具（`train`、`girvan_newman` 等）执行时，通过回调更新 `JobManager` 中的 progress 字段，API Gateway 轮询并通过 SSE 推送 `tool_progress` 事件：

```json
{"event": "tool_progress", "job_id": "xxx", "progress": 0.42, "message": "Epoch 42/100, loss=0.234"}
```

**新增工具**

| 工具名 | 功能 |
|--------|------|
| `get_job_status` | 查询作业状态 |
| `cancel_job` | 取消长任务 |

**涉及文件**

| 文件 | 操作 |
|------|------|
| `graph_data_service/job_manager.py` | **新增** |
| `graph_data_service/graph_ml/trainer/` | 修改：各 trainer 新增 `progress_callback` |
| `graph_data_service/tool_catalog.py` | 修改：新增 2 条工具 spec |
| `backend/api_gateway/app.py` | 修改：SSE 新增 `tool_progress` 事件 |
| `index.html` | 修改：进度条 UI |

---

### 3.4 结构化分析报告

**现状问题**

Agent 输出为即时 Markdown 文本，复杂多步分析结果散落在多轮对话中，难以回溯和下游消费。

**方案**

在 Phase 1 Planner 的基础上，Plan 执行完毕后由 LLM 生成标准化报告对象。

```python
@dataclass
class AnalysisReport:
    session_id: str
    plan_id: str
    query: str
    plan_summary: str           # 分析步骤概述
    key_findings: list[str]     # 关键发现（每步一条）
    algorithms_used: list[str]  # 使用的算法
    conclusion: str             # 最终结论
    limitations: str            # 分析局限性
    next_steps: list[str]       # 建议的后续分析方向
    created_at: str
```

**输出格式参数**

在 chat API 中新增 `output_format` 参数：

| 值 | 行为 |
|----|------|
| `"chat"`（默认） | 当前流式对话行为 |
| `"report"` | 触发 Planner + 结构化报告生成 |
| `"json"` | 返回 `AnalysisReport` JSON，供下游系统消费 |

报告持久化到 `logs/{session_id}/report_{plan_id}.json`。

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/planner.py` | 修改：新增 `generate_report()` |
| `backend/agent/igraph.py` | 修改：支持 `output_format` 参数 |
| `backend/api_gateway/app.py` | 修改：API 接收 `output_format` |
| `index.html` | 修改：新增报告视图 Tab |

---

### 3.5 SFT 数据集导出

**现状问题**

Text Protocol 模式下所有上下文已完整记录在 `logs/{session_id}/message_list.jsonl`，是宝贵的训练数据来源，但没有工具将其转换为标准格式。这是 iGraph **独有的竞争优势**。

**方案**

新增 `/api/export/sft` 接口。

```
GET /api/export/sft
  ?format=alpaca|sharegpt|raw
  &from=2026-01-01
  &to=2026-04-10
  &min_turns=2              # 过滤过短会话
  &require_tool_calls=true  # 只导出含工具调用的轮次
→ 返回 .jsonl 文件下载
```

**转换逻辑**

```
message_list.jsonl 中每个 (user_turn, assistant_protocol, tool_observation..., answer) 序列
→ 提取完整工具调用链路
→ 按格式输出：
   Alpaca: {"instruction": user, "input": graph_context, "output": answer}
   ShareGPT: {"conversations": [{role, value}, ...]}  含完整 tool_calls
   Raw: 原始 jsonl 直接导出
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/api_gateway/app.py` | 修改：新增 `/api/export/sft` 路由 |
| `backend/logging/sft_exporter.py` | **新增**：格式转换逻辑 |
| `index.html` | 修改：新增导出入口按钮 |

---

### 3.6 多 Agent 协作（长期探索）

**现状问题**

单 Agent 线性执行，复杂任务受 16 步上限约束。某些场景下（大图多指标分析、报告与计算分离）并行执行效率更高。

**方案**（仅在以下场景启用）

- 大图多指标分析：各指标可并行计算
- 报告与计算分离：一个 sub-Agent 执行算法，一个 sub-Agent 组织报告
- 图学习 + 图分析组合任务

引入轻量 Supervisor 模式：

```python
class SupervisorAgent:
    """协调多个 sub-Agent 并行执行"""

    async def run(self, plan: AnalysisPlan):
        # 将无依赖关系的步骤分组
        parallel_groups = self._group_independent_steps(plan)
        for group in parallel_groups:
            # 并行执行同组步骤
            tasks = [self._spawn_sub_agent(step) for step in group]
            results = await asyncio.gather(*tasks)
            # 汇总结果到 plan
```

Sub-Agent 复用 `iGraph` 类，共享同一 GraphData Session。Supervisor 只是协调调度层。

**本项暂不实现**，仅保留设计预留，待 Phase 1/2 稳定后按需推进。

---

### Phase 3 里程碑

| 里程碑 | 包含项 | 验收条件 |
|--------|-------|---------|
| **M3.1** | GraphML 编排化 | Planner 能自动规划 ML 步骤 |
| **M3.2** | 图后端可插拔 | Neo4j 可选配置，memory 默认不变 |
| **M3.3** | 异步作业系统 | 训练进度实时推送到前端 |
| **M3.4** | 报告 + SFT 导出 | 结构化报告生成 + SFT jsonl 可导出 |

---

## 八、横切关注点

以下各项不绑定到特定 Phase，可在任意阶段穿插实施。

---

### 8.1 Cytoscape.js 图可视化

**现状**：前端用 mermaid.js 渲染图，只能显示小图（<50 节点），无交互。

**方案**

引入 `Cytoscape.js`（纯前端 CDN，零后端依赖），替换 mermaid 图渲染。

**功能要求**

| 功能 | 说明 |
|------|------|
| 基本渲染 | 节点 + 边 + 标签，自动布局（cola / cose） |
| 算法结果着色 | 最短路径红色、社区不同颜色、高中心性节点放大 |
| 交互操作 | 拖拽布局、缩放、点击节点弹出属性面板 |
| 高亮接口 | 工具结果新增 `highlight` 字段，前端按此渲染覆盖层 |

**highlight 字段格式**

```json
{
  "highlight": {
    "nodes": ["A", "B", "C"],
    "edges": [["A","B"], ["B","C"]],
    "color": "#e74c3c",
    "label": "最短路径"
  }
}
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `index.html` | 修改：引入 Cytoscape.js CDN，替换 mermaid 图渲染 |
| `graph_data_service/codec.py` | 修改：结果序列化带 `highlight` 字段 |
| `graph_data_service/compute.py` | 修改：算法结果附带高亮数据 |

**建议实施时机**：Phase 0 完成后即可开始，与 Phase 1 并行。

---

### 8.2 Skill 体系升级

**现状**：仅 1 个 Skill（graph_machine_learning），按工具集条件触发，整段注入 SKILL.md。

**升级方向**：从"能力约束提示块"升级为 `Skill Registry + Selector + Renderer` 三层架构。

**Phase 0-1 期间先做**

| 改进 | 说明 |
|------|------|
| 新增 `graph_analysis` Skill | 覆盖社区检测、中心性分析、路径查找等常见分析场景的最佳实践 |
| 新增 `data_ingestion` Skill | 覆盖数据导入、表格建图、文本建图的工作流指导 |
| 多 Skill 激活 | 允许同时激活多个 Skill |

**Phase 2 后做**

| 改进 | 说明 |
|------|------|
| 渐进式披露 | `hint`（一句话提示）→ `guide`（详细指导）→ `full`（完整规范），按需升级 |
| 与 Memory 联动 | Playbook 命中时自动关联对应 Skill |
| 与 Planner 联动 | Skill 提供 step 模板，Planner 可复用 |

**核心结构**

```python
# backend/agent/skill_loader.py（升级后）

@dataclass
class SkillMeta:
    name: str
    description: str
    trigger_condition: Callable  # (tools, graph_state, query) -> bool
    levels: dict[str, str]       # {"hint": "...", "guide": "...", "full": "..."}

class SkillRegistry:
    _skills: list[SkillMeta] = []

    def register(self, skill: SkillMeta): ...
    def select(self, tools, graph_state, query) -> list[tuple[SkillMeta, str]]:
        """返回 [(skill, level), ...]"""
        ...
```

**涉及文件**

| 文件 | 操作 |
|------|------|
| `backend/agent/skill_loader.py` | 修改：升级为 SkillRegistry |
| `backend/agent/skills/graph_analysis/SKILL.md` | **新增** |
| `backend/agent/skills/data_ingestion/SKILL.md` | **新增** |

---

### 8.3 测试与评测体系

**现状**：13 个测试文件，~1839 行，覆盖服务集成层，但缺少 Agent 行为级评测。

**补充方向**

| 评测维度 | 实现方式 | 优先级 |
|---------|---------|-------|
| **工具选择正确率** | 构建"问题 → 期望工具序列"基准集（20 题），回放对比 | Phase 0 后 |
| **规划质量** | 人工标注 20 个复杂问题的参考 Plan，与 Planner 输出做结构对比 | Phase 1 后 |
| **记忆召回准确率** | 构建"查询 → 期望记忆条目"测试集，对比新旧召回 | Phase 0 后 |
| **GraphML 稳定性** | 固定随机种子 + 合成图，确保 train/infer 结果可复现 | 持续 |
| **端到端 session replay** | 录制标准会话 → 重放 → 对比工具调用链和最终答案 | Phase 1 后 |
| **建图质量** | 提供标准 CSV/文本 → 验证建图结果的节点数、边数、连通性 | Phase 2 后 |

**基准集维护**

```
tests/
  benchmarks/
    tool_selection.json      # 问题 → 期望工具序列
    plan_quality.json        # 复杂问题 → 参考 Plan
    memory_recall.json       # 查询 → 期望记忆条目
    ingestion_quality/       # 标准 CSV/文本 → 期望图属性
```

**验收标准**

- [ ] 基准集至少 20 个 case
- [ ] CI 中可自动运行非 LLM 依赖的测试
- [ ] LLM 依赖的测试提供手动运行脚本

---

### 8.4 可观测性指标

**目标**：为规划、工具调用、记忆、GraphML 等核心环节增加结构化指标，方便性能分析和质量追踪。

**指标清单**

| 模块 | 指标 | 采集方式 |
|------|------|---------|
| Planner | plan 生成耗时、步骤数、工具命中率 | 日志 + trace.jsonl |
| 工具调用 | 每轮工具调用数、成功率、平均耗时 | trace.jsonl |
| 记忆 | 召回条目数、召回延迟、命中率 | 日志 |
| GraphML | 训练耗时、loss 曲线、推理延迟 | artifact 元数据 |
| ToolSelector | 注入工具数、token 节省量 | 日志 |
| 整体 | 回答完成率、平均轮数、session 活跃时长 | session_meta |

**实现方式**

- 统一写入 `logs/{session_id}/trace.jsonl`（已有框架）
- 新增 `metrics` 字段到 `session_meta.json`
- 不引入外部监控系统，保持轻量

---

## 九、依赖关系、里程碑与验收

### 9.1 全局依赖 DAG

```
                  ┌─────────┐
                  │ Phase 0 │
                  └────┬────┘
                       │
          ┌────────────┼────────────┐
          │            │            │
    ┌─────┴─────┐ ┌───┴───┐ ┌─────┴─────┐
    │ 0.1 算法  │ │ 0.3   │ │ 0.5 图快照│
    │ 0.2 目录  │ │ 工具注│ │           │
    └─────┬─────┘ │ 入    │ └─────┬─────┘
          │       │ 0.4   │       │
          │       │ 语义  │       │
          │       │ 记忆  │       │
          │       └───┬───┘       │
          │           │           │
          └─────┬─────┘           │
                │                 │
          ┌─────┴─────┐           │
          │ Phase 1   │           │
          │ 规划层    │           │
          │ 1.1-1.6   │           │
          └─────┬─────┘           │
                │                 │
          ┌─────┴─────────────────┘
          │
    ┌─────┴─────┐
    │ Phase 2   │
    │ 数据集层  │
    │ 2.1-2.4   │
    └─────┬─────┘
          │
    ┌─────┴─────┐
    │ Phase 3   │
    │ 平台化    │
    │ 3.1-3.6   │
    └───────────┘

横切关注点（8.1-8.4）可在任意阶段穿插
```

### 9.2 Release 里程碑

| Release | 名称 | 包含 Phase | 完成条件 |
|---------|------|-----------|---------|
| **R1** | 强化版 iGraph | Phase 0 全部 | 算法 50+，语义记忆，动态注入，图快照 |
| **R2** | 可规划的 iGraph | Phase 1 全部 | 复杂任务 Plan 模式，前端步骤展示 |
| **R3** | 数据集化 iGraph | Phase 2 全部 | Dataset CRUD，Table/Text 建图，RAG |
| **R4** | 平台化 iGraph | Phase 3 选择性 | 报告输出，SFT 导出，GraphML 编排 |

### 9.3 每阶段质量验收清单

**通用验收项（每个 Phase 都需要）**

- [ ] 所有新增代码有对应测试
- [ ] 现有测试全部通过（无回归）
- [ ] 文档更新（docs/ 目录）
- [ ] `.gitignore` 更新（如有新的生成文件）

**Phase 0 专项验收**

- [ ] `python agent_server.py` 一键启动正常
- [ ] 新增算法在 Text Protocol + Native FC 下均可调用
- [ ] 服务重启后图自动恢复
- [ ] token 消耗降低 ≥ 40%（对比日志）
- [ ] 语义记忆召回人工验证 10 个 case

**Phase 1 专项验收**

- [ ] 简单问题走 Direct，复杂问题走 Plan（自动切换正确率 ≥ 80%）
- [ ] Plan 模式下 10 个复杂 case 成功率 ≥ 70%
- [ ] 前端步骤列表正确渲染
- [ ] Plan JSON 持久化并可回溯

**Phase 2 专项验收**

- [ ] 数据集跨会话可用
- [ ] CSV 建图端到端通过
- [ ] 文本建图三元组提取质量合理
- [ ] RAG 检索对回答质量有提升
- [ ] ~~多用户记忆互不干扰~~（暂缓）

**Phase 3 专项验收**

- [ ] Planner 能自动规划含 ML 步骤的计划
- [ ] Neo4j 可选配置不影响默认行为
- [ ] 训练进度前端可见
- [ ] SFT 导出格式正确可用

---

## 十、风险与应对策略

### 10.1 规划层过重

| 风险 | 把 iGraph 拉向重型工作流引擎，简单问题反而变慢 |
|------|-----|
| **概率** | 中 |
| **影响** | 高——用户体验倒退 |
| **应对** | 1. Plan 模式默认关闭，只在 score ≥ 4 时触发 |
|          | 2. 保留 Direct 模式作为 fallback，永不移除 |
|          | 3. 每步限制 max_steps=4，避免失控 |
|          | 4. 用户可手动切换 `mode=direct` 强制直接模式 |

### 10.2 数据集层复杂度失控

| 风险 | 会话系统与数据系统混杂，状态管理爆炸 |
|------|-----|
| **概率** | 中 |
| **影响** | 中——维护成本急剧上升 |
| **应对** | 1. 严格区分 `session graph`（运行态）与 `dataset graph`（资产态） |
|          | 2. 用独立 `DatasetRegistry` 管理，不侵入现有 `GraphSession` |
|          | 3. 会话与数据集只通过 `dataset_binding.json` 松耦合关联 |

### 10.3 外部依赖过多

| 风险 | 破坏 iGraph 轻量部署优势 |
|------|-----|
| **概率** | 中 |
| **影响** | 高——核心差异化丧失 |
| **应对** | 1. `sentence-transformers` 是唯一新增强依赖，且可选降级 |
|          | 2. Neo4j 永远是可选 adapter，不作为默认 |
|          | 3. RAG 使用纯 numpy，不引入 Milvus / sqlite-vec |
|          | 4. 所有外部依赖均有"不安装也能跑"的降级路径 |

### 10.4 GraphML 与 Planner 融合过早

| 风险 | 规划层尚未稳定，先编排 GNN 会放大复杂度 |
|------|-----|
| **概率** | 低 |
| **影响** | 中 |
| **应对** | GraphML 编排化放到 Phase 3（Phase 1 规划层稳定后再做） |

### 10.5 LLM 计划生成质量不稳定

| 风险 | 不同模型生成的 Plan 质量差异大，JSON 格式不合规 |
|------|-----|
| **概率** | 高 |
| **影响** | 中——Plan 模式可用性受损 |
| **应对** | 1. Algorithm Catalog 白名单严格约束工具选择 |
|          | 2. JSON 解析失败时自动降级到 Direct 模式 |
|          | 3. Prompt 中提供 few-shot 示例 |
|          | 4. 不同模型测试并记录最佳 prompt 模板 |

---

## 附录：新增与修改文件汇总

### 新增文件清单

| 阶段 | 文件路径 | 用途 |
|------|---------|------|
| Phase 0 | `graph_data_service/algorithm_catalog.py` | 算法知识层 |
| Phase 0 | `backend/agent/tool_selector.py` | 语义工具检索 |
| Phase 1 | `backend/agent/query_analyzer.py` | 查询复杂度检测 |
| Phase 1 | `backend/agent/planner.py` | 计划生成 + 数据结构 |
| Phase 2 | `graph_data_service/dataset_registry.py` | 数据集注册中心 |
| Phase 2 | `graph_data_service/data_pipeline/__init__.py` | 建图管线包 |
| Phase 2 | `graph_data_service/data_pipeline/table_extractor.py` | 表格建图 |
| Phase 2 | `graph_data_service/data_pipeline/text_extractor.py` | 文本建图 |
| Phase 2 | `graph_data_service/data_pipeline/graph_builder.py` | 通用图构建器 |
| Phase 2 | `graph_data_service/rag.py` | 轻量 RAG |
| Phase 3 | `graph_data_service/storage/base.py` | 存储后端抽象 |
| Phase 3 | `graph_data_service/storage/memory_backend.py` | 内存存储 |
| Phase 3 | `graph_data_service/storage/neo4j_backend.py` | Neo4j 存储 |
| Phase 3 | `graph_data_service/job_manager.py` | 异步作业管理 |
| Phase 3 | `backend/logging/sft_exporter.py` | SFT 数据导出 |
| 横切 | `backend/agent/skills/graph_analysis/SKILL.md` | 图分析 Skill |
| 横切 | `backend/agent/skills/data_ingestion/SKILL.md` | 数据导入 Skill |
| 横切 | `tests/benchmarks/` | 评测基准集 |

### 主要修改文件清单

| 文件 | Phase 0 | Phase 1 | Phase 2 | Phase 3 |
|------|---------|---------|---------|---------|
| `backend/agent/igraph.py` | ✓ | ✓ | ✓ | ✓ |
| `backend/agent/memory_engine.py` | ✓ | | ✓ | |
| `backend/agent/context_manager.py` | ✓ | | | |
| `backend/agent/skill_loader.py` | | | | ✓ |
| `backend/api_gateway/app.py` | | ✓ | ✓ | ✓ |
| `graph_data_service/tool_catalog.py` | ✓ | | ✓ | ✓ |
| `graph_data_service/compute.py` | ✓ | | ✓ | |
| `graph_data_service/session.py` | ✓ | | | ✓ |
| `graph_data_service/app.py` | ✓ | | ✓ | ✓ |
| `graph_data_service/codec.py` | | | | ✓ |
| `mcp_service/graph_data_client.py` | | | ✓ | |
| `mcp_service/session_registry.py` | | | ✓ | |
| `backend/logging/session_store.py` | ✓ | ✓ | | |
| `index.html` | | ✓ | | ✓ |

---

*核心原则：iGraph 的差异化价值在于**透明协议 + 自动记忆 + GNN 一体化 + 轻量部署**。四个阶段的迭代始终围绕这四点展开，不引入 Milvus / Neo4j 等强依赖（Neo4j 为可选），不破坏现有服务边界，每个优化项均可独立交付。*
