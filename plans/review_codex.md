# Codex 实现 Review

> 基于 `plans/plan_final.md` 对 `codex/plan-final-implementation` 分支的代码变更进行逐模块 Review。
> 审核日期：2026-04-13

---

## 一、变更统计

| 类型 | 文件数 | 新增行数 | 删除行数 |
|------|--------|---------|---------|
| 修改已有文件 | 12 | ~1260 | ~155 |
| 新增文件 | 12 | ~770 | 0 |
| **合计** | **24** | **~2030** | **~155** |

新增文件清单：
- `backend/agent/planner.py` (~140 行)
- `backend/agent/query_analyzer.py` (~95 行)
- `backend/agent/tool_selector.py` (~110 行)
- `backend/logging/sft_exporter.py` (~40 行)
- `backend/agent/skills/graph_analysis/SKILL.md`
- `backend/agent/skills/data_ingestion/SKILL.md`
- `graph_data_service/algorithm_catalog.py` (~220 行)
- `graph_data_service/dataset_registry.py` (~130 行)
- `graph_data_service/job_manager.py` (~70 行)
- `graph_data_service/rag.py` (~100 行)
- `graph_data_service/data_pipeline/` (3 文件 ~180 行)
- `graph_data_service/storage/` (3 文件 ~50 行)

---

## 二、各 Phase 完成度评估

### Phase 0 — 基础增强

| 子任务 | 计划状态 | 实现状态 | 完成度 |
|--------|---------|---------|--------|
| 0.1 算法库扩展 | 34 个新算法, 6 新工具 | 6 新工具 + enum 扩展均已实现 | 90% |
| 0.2 Algorithm Catalog | AlgorithmMeta + TASK_TYPE_MAP | 完整实现，~40 条记录 | 85% |
| 0.3 工具动态注入 (ToolSelector) | 语义检索 top-K | 实现，含降级逻辑 | 80% |
| 0.4 记忆语义化 | SemanticMemoryIndex | 已集成到 MemoryEngine | 70% |
| 0.5 图快照 + Undo | save/load/undo | 完整实现 + 服务启动自动恢复 | 95% |

### Phase 1 — 规划层

| 子任务 | 计划状态 | 实现状态 | 完成度 |
|--------|---------|---------|--------|
| 1.1 QueryAnalyzer | 子 Agent 判断 | 实现，含启发式 fallback | 70% |
| 1.2 Planner | LLM 生成结构化计划 | 完整实现 + fallback plan | 80% |
| 1.3 Plan Executor | _run_with_plan() | 已实现，复用 ReAct 循环 | 75% |
| 1.4 Plan Store + SSE | 持久化 + 新事件类型 | session_store 扩展 + SSE 事件 | 85% |
| 1.5 前端计划面板 | 步骤列表 + 状态渲染 | planPanelHtml() 完整实现 | 90% |
| 1.6 主循环改造 | chat_stream 重构 | 拆分为 _run_react_loop + plan 分支 | 85% |

### Phase 2 — 数据集平台

| 子任务 | 计划状态 | 实现状态 | 完成度 |
|--------|---------|---------|--------|
| 2.1 Dataset Registry | CRUD + 版本 | 完整实现 | 90% |
| 2.2 Table -> Graph | CSV/Excel 建图 | TableExtractor + GraphBuilder 实现 | 85% |
| 2.3 Text -> Graph | LLM 三元组抽取 | TextExtractor 实现，但无 LLM 注入 | 50% |
| 2.4 轻量 RAG | 文档分块 + 检索 | LightweightRAG 实现 | 60% |

### Phase 3 — 平台化（部分实现）

| 子任务 | 计划状态 | 实现状态 | 完成度 |
|--------|---------|---------|--------|
| 3.1 GraphML 编排化 | Planner 纳入 ML | TASK_TYPE_MAP 含 ML 条目 | 30% |
| 3.2 图后端可插拔 | Storage 抽象 | 骨架实现，未接入 session.py | 20% |
| 3.3 异步作业系统 | JobManager | 结构实现，未实际使用 | 25% |
| 3.4 结构化报告 | output_format | 参数定义了，未在流程中消费 | 15% |
| 3.5 SFT 导出 | API + 格式转换 | 基础实现 | 40% |

### 横切关注点

| 子任务 | 实现状态 | 完成度 |
|--------|---------|--------|
| 8.1 Cytoscape.js 图可视化 | 未实现 | 0% |
| 8.2 Skill 体系升级 | 新增 2 个 Skill + 多 Skill 激活 | 40% |
| 8.3 测试体系 | 仅新增 2 个测试用例 | 10% |

---

## 三、Bug 与关键问题

### BUG-1：`_run_with_plan` 变量遮蔽导致 state 丢失（严重）

**文件**: `backend/agent/igraph.py`，`_run_with_plan()` 方法内部

```python
async def _run_with_plan(self, plan, msgs, *, state: dict):
    ...
    for step in plan.steps:
        ...
        state: dict = {}          # <-- 遮蔽了参数 state！
        async for event in self._run_react_loop(msgs, ..., state=state):
            yield event
        step.status = "done" if state.get("answered") else "failed"
        ...

    state.update({                # <-- 这里更新的是循环最后一次创建的局部 state
        "final_answer": final_answer,
        ...
    })
```

循环体内 `state: dict = {}` 重新声明了一个局部变量，遮蔽了外层参数 `state`。结果：
- 每次循环迭代创建一个新的空 dict，上一步的结果被丢弃
- 最终 `state.update(...)` 更新的是最后一轮循环内创建的局部 dict，而非调用方传入的 `run_state`
- **调用方 `chat_stream()` 中 `run_state` 始终为空**，导致 `final_answer_for_memory` 始终为 `""`

**修复**: 将 `state: dict = {}` 改为 `step_state: dict = {}`，后续引用也改为 `step_state`。

---

### BUG-2：`_invoke_aux_llm` 复用主模型，未路由到小模型

**文件**: `backend/agent/igraph.py`

```python
async def _invoke_aux_llm(self, prompt: str) -> str:
    model_msgs = [
        {"role": "system", "content": "你是一个严格输出 JSON 的图分析辅助组件。"},
        {"role": "user", "content": prompt},
    ]
    return await self._provider_gateway.invoke_text(model_msgs, step=900000 + ...)
```

**问题**: plan_final.md 1.1 节明确要求 QueryAnalyzer 使用**最小可用模型**（haiku / qwen-flash）以控制延迟和成本。当前实现直接走 `provider_gateway.invoke_text()`，使用的是**与主对话相同的模型**。

对于每条用户消息，都会触发一次 `should_plan()` 的 LLM 调用。如果主模型是 GPT-4 或 Claude Opus，这意味着每轮对话额外增加一次大模型调用（~500ms + ~2000 token）。

**建议**: `provider_gateway` 增加 `invoke_text(model_msgs, purpose="complexity_judge")` 路由逻辑，根据 purpose 选择小模型。或在 `_invoke_aux_llm` 中显式使用低成本模型。

---

### BUG-3：`EmbeddingBackend` 默认禁用，语义功能形同虚设

**文件**: `backend/agent/tool_selector.py`

```python
@classmethod
def load(cls) -> "EmbeddingBackend":
    if os.getenv("IGRAPH_ENABLE_SENTENCE_TRANSFORMERS", "").strip().lower() not in {"1", "true", ...}:
        return cls(name="disabled", available=False, dim=384, _model=None)
    ...
```

**问题**: plan_final.md 0.3 节的降级策略是"若 `sentence-transformers` 未安装，自动回退到全量注入"。但实际实现**要求显式设置环境变量 `IGRAPH_ENABLE_SENTENCE_TRANSFORMERS=1` 才启用**。

这意味着：
- ToolSelector 的 `select()` 方法检查 `self._backend.available`，默认为 False → **始终返回全量工具集**，语义检索完全不生效
- SemanticMemoryIndex 的 `recall()` 方法检查 `self.enabled`，默认为 False → **始终返回空列表**，语义记忆完全不生效

Plan 的降级逻辑应该是：尝试 import → 成功就用 → 失败才 fallback。当前逻辑是：不设 env var → 直接跳过 → 永远 fallback。

**建议**: 删除环境变量门控，改为 try-import 降级：
```python
@classmethod
def load(cls) -> "EmbeddingBackend":
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        return cls(name="all-MiniLM-L6-v2", available=True, dim=384, _model=model)
    except Exception:
        return cls(name="disabled", available=False, dim=384, _model=None)
```

---

### BUG-4：`TextExtractor` 未注入 LLM，只能走启发式

**文件**: `graph_data_service/app.py`

```python
class GraphDataService:
    def __init__(self) -> None:
        ...
        self._text_extractor = TextExtractor()  # 没有传入 llm 参数
```

`TextExtractor.__init__` 接受 `llm: Callable | None = None`。未传入 LLM 时，`extract()` 方法跳过 LLM 调用，直接走 `_heuristic_extract()`。

启发式抽取只识别以下几个固定模式：
- `连接到`, `属于`, `位于`, `related to`, `works at`, `knows`
- `A — B` (dash 分隔)

对于绝大多数自然语言文本，这些模式命中率极低，**`build_graph_from_text` 工具基本不可用**。

**建议**: GraphDataService 应接受一个 LLM callable（可通过 HTTP 调用 API Gateway 的辅助 LLM 接口），或将 TextExtractor 的 LLM 依赖延迟到调用时注入。

---

### BUG-5：`LightweightRAG` 每次调用重新实例化，无缓存

**文件**: `graph_data_service/app.py`

```python
def do_upload_document(self, session_id, ...):
    rag = LightweightRAG(str(session._session_dir))  # 每次 new
    ...

def do_retrieve_context(self, session_id, query, top_k):
    rag = LightweightRAG(str(session._session_dir))  # 每次 new
    ...
```

每次调用都创建新 `LightweightRAG` 实例，该实例在 `__init__` 中创建目录但不加载索引。`retrieve()` 内部每次从磁盘读取 `doc_embeddings.npy` 和 `doc_index.json`。

对于频繁检索场景，这会导致不必要的 I/O。

**建议**: 在 `GraphSession` 或 `GraphDataService` 中缓存 `LightweightRAG` 实例（按 session_id），或作为 `GraphSession` 的属性懒初始化。

---

### ISSUE-6：`memory_engine.py` 语义召回去重逻辑有问题

**文件**: `backend/agent/memory_engine.py`

```python
semantic_hits = self._semantic_index.recall(user_input, top_k=8)
picked_memories: List[dict] = []
if semantic_hits:
    ...
    for item in semantic_hits:
        memory_id = str(item.get("memory_id") or "")
        if memory_id and memory_id in seen_ids:
            continue
        ...
        picked_memories.append(item)
scored = sorted(items, key=lambda it: self._score_item(it, terms), reverse=True)
for item in scored:
    if len(picked_memories) >= 8:
        break
    if item in picked_memories:  # <-- dict 比较用的是 object identity 还是 value equality？
        continue
    picked_memories.append(item)
```

**问题**:
1. `item in picked_memories` 对 dict 做 `==` 比较，Python 中 dict equality 是值比较，但这依赖于 semantic_hits 返回的是 `self._items` 中的**同一个**对象引用。如果 `recall()` 返回的 dict 是拷贝，则去重失败。
2. `memory_id` 字段可能不存在于所有 memory item 中，导致 `memory_id` 为空字符串 → `if memory_id` 为 False → 跳过去重逻辑。
3. 当 `semantic_hits` 为空列表时，`picked_memories` 被初始化但为空，后续关键词匹配正常工作。但当 semantic 返回结果但 `enabled` 为 False 时（当前默认情况），`recall()` 返回空列表，实际走的还是纯关键词匹配——这是对的，但代码路径不够直观。

---

### ISSUE-7：`sft_exporter.py` 功能严重不足

**文件**: `backend/logging/sft_exporter.py` + `backend/api_gateway/app.py`

与 plan_final.md 3.5 节对比：

| 计划要求 | 实现状态 |
|---------|---------|
| `from` / `to` 日期过滤 | 未实现 |
| ShareGPT 格式含完整 tool_calls | 仅含 user/assistant 文本轮次 |
| 工具调用链路提取 | 未实现 |
| 返回 .jsonl 文件下载 | 返回 JSON 响应（非文件下载） |
| graph_context 作为 Alpaca input | input 字段始终为空字符串 |

此外，`export_sft` 路由中 `self.logger.store` 属性访问不安全——`Logger` 类没有公开的 `store` 属性，`hasattr` 检查可能始终为 False，导致接口返回空结果。

---

### ISSUE-8：`_graph_info_payload` 过于简陋

**文件**: `backend/agent/igraph.py`

```python
def _graph_info_payload(self) -> dict:
    return {"has_graph": self._has_graph}
```

此方法的返回值传给 `Planner.build_plan()` 作为 `graph_info` 参数。Planner 将其序列化后注入 LLM prompt：

```python
f"当前图信息: {json.dumps(graph_info, ensure_ascii=False)}\n"
```

LLM 只看到 `{"has_graph": true}`——完全无法判断图的规模、是否有向、是否有权重。Plan 1.1 节明确要求注入 node_count, edge_count, is_directed, has_weights 等信息。

**建议**: 利用 `set_graph_meta()` 已经缓存的元数据，构建更丰富的 payload：
```python
def _graph_info_payload(self) -> dict:
    meta = self.logger.get_graph_meta()  # 或维护本地字段
    return {
        "has_graph": self._has_graph,
        "node_count": meta.get("graph_node_count"),
        "edge_count": meta.get("graph_edge_count"),
        "is_directed": meta.get("graph_is_directed"),
        "has_weights": meta.get("graph_has_weights"),
    }
```

---

### ISSUE-9：`storage/` 模块完全未接入

**文件**: `graph_data_service/storage/`

三个文件（base.py、memory_backend.py、neo4j_backend.py）已创建，但：
- `session.py` 未使用 `GraphStorageBackend` 抽象
- `app.py` 未根据配置选择 backend
- `Neo4jBackend` 所有方法 raise `NotImplementedError`
- `MemoryBackend` 是一个独立实现，与 `GraphSession._graph` 字段完全脱节
- `__init__.py` 导出了 `Neo4jBackend`，但 import 时不会报错（没有 neo4j 依赖也能 import，只是调用时失败）

**评估**: 这是纯骨架代码，对当前系统零功能贡献。建议要么完成接入，要么暂时删除避免代码膨胀。

---

### ISSUE-10：`JobManager` 存在但未被实际使用

**文件**: `graph_data_service/job_manager.py` + `graph_data_service/app.py`

`JobManager` 在 `GraphDataService.__init__()` 中实例化，`_dispatch_graph_tool` 中提供了 `get_job_status` 和 `cancel_job` 两个工具的分发。但：
- 没有任何工具调用通过 `JobManager.submit()` 提交异步作业
- `train` / `infer` 仍然通过 `asyncio.to_thread()` 同步执行
- 没有 `tool_progress` SSE 事件
- Trainer 没有 `progress_callback`

**评估**: 骨架实现，需要后续接入 heavy tools 才有价值。

---

## 四、架构设计评审

### 4.1 做得好的地方

1. **主循环拆分合理**: `chat_stream()` 被拆为 `_run_react_loop()` + `_run_with_plan()`，Plan 模式复用了 ReAct 循环作为执行引擎，不重复造轮子。

2. **tool_catalog 单一真相源未被破坏**: 新增的 6 个工具（subgraph_extraction、link_prediction_score、path_analysis、clique_analysis、reachability、global_metrics）+ 数据管理工具（undo_graph、list_datasets 等）均在 tool_catalog.py 中定义，MCP 自动注册链路完好。

3. **图快照机制设计稳健**: `save_snapshot()` / `load_snapshot()` / `undo()` 实现清晰，历史版本自动轮转（最多 5 个），服务启动自动恢复。

4. **EmbeddingBackend 降级设计**: hash-based 伪向量作为 fallback 是合理选择（虽然语义效果差，但保证了接口一致性）。

5. **DatasetRegistry 实现完整**: 文件系统存储、版本管理、元数据持久化，符合 plan 要求的轻量级设计。

6. **前端 Plan 面板**: SSE 事件监听 + 状态渲染的实现干净利落，步骤状态的视觉区分（pending/running/done/failed/skipped）做得好。

7. **compute.py 算法扩展干净**: 新增的 6 个计算方法（subgraph_extraction、link_prediction_score、path_analysis、clique_analysis、reachability、global_metrics）结构统一，错误处理到位。

### 4.2 需要改进的地方

1. **ToolSelector 和 SemanticMemoryIndex 混在同一文件**: `tool_selector.py` 包含了 `EmbeddingBackend`、`ToolSelector`、`SemanticMemoryIndex` 三个不同职责的类。`SemanticMemoryIndex` 应该放在 `memory_engine.py` 附近或独立为 `semantic_index.py`。

2. **QueryAnalyzer 构造函数与 plan 不一致**: plan 设计为 `QueryAnalyzer(llm_factory)` 其中 `llm_factory` 创建轻量 LLM 实例。实际实现接收的是 `llm_factory: Callable[[str], Awaitable[str]]` — 一个直接的 LLM 调用函数，而不是工厂。这导致无法为不同 purpose 选择不同模型。

3. **context_manager.py 未被修改**: plan 0.3 说"修改 context_manager.py 调用 ToolSelector 替换全量注入"。实际是在 `igraph.py` 的 `_build_model_messages()` 中做的工具过滤，这虽然可行但不如在 context_manager 中做更内聚。

4. **Planner 的 fallback_plan 过于简单**: 仅根据"社区"和"中心"两个关键词决定步骤。遇到其他类型的复杂查询（如路径分析、链路预测）时，fallback plan 退化为通用的 2 步计划。

---

## 五、代码质量评审

### 5.1 类型安全

- `planner.py`、`query_analyzer.py`、`tool_selector.py` 使用了完整的类型标注
- `algorithm_catalog.py` 使用 `@dataclass(frozen=True)`，不可变，良好
- `rag.py` 中 `_Hasher` 类未标注返回类型

### 5.2 错误处理

- `Planner.build_plan()` 对 LLM 调用做了 try-except，失败时 fallback
- `QueryAnalyzer.should_plan()` 同样有 fallback 到启发式
- `_run_tool()` 中新增了更多工具的 `_has_graph` 状态追踪，但外层 try-except 粒度过粗
- `compute.py` 新增方法统一 try-except 返回 error dict，良好

### 5.3 日志与可观测性

- `_persist_plan()` 会将 plan 状态写入 trace.jsonl 和独立 plan JSON 文件
- `PLAN_DECISION` trace event 记录了 should_plan 判断结果
- 但 `_run_react_loop()` 中缺少步骤级别的 trace event（只有从外层 `_run_with_plan` 的 step_start/step_done）

### 5.4 测试覆盖

**严重不足**。plan 要求 "新增至少 20 个测试用例"（仅算法扩展），实际只新增了 2 个测试：
- `test_undo_graph_and_dataset_roundtrip` — 覆盖 undo + dataset CRUD
- `test_build_graph_from_text_and_retrieve_context` — 覆盖 text→graph + RAG

以下完全没有测试：
- 6 个新计算工具（subgraph_extraction 等）
- node_centrality 新增的 5 个 enum 值（harmonic, katz, load, voterank, percolation）
- community_detection 新增的 4 个 enum 值
- Planner / QueryAnalyzer / ToolSelector 的单元测试
- SFT exporter 的格式正确性
- 前端 SSE 事件的端到端测试

---

## 六、改进建议（按优先级排序）

### P0 — 必须修复

| # | 问题 | 建议 |
|---|------|------|
| 1 | `_run_with_plan` 变量遮蔽 bug | 将 `state: dict = {}` 改为 `step_state: dict = {}` |
| 2 | `EmbeddingBackend` 默认禁用 | 改为 try-import 降级，删除环境变量门控 |
| 3 | `TextExtractor` 无 LLM | 在 `GraphDataService` 中注入 LLM callable |
| 4 | `_graph_info_payload` 过于简陋 | 返回 node_count/edge_count/is_directed/has_weights |

### P1 — 应该修复

| # | 问题 | 建议 |
|---|------|------|
| 5 | 辅助 LLM 未路由到小模型 | `provider_gateway` 支持 purpose 路由 |
| 6 | `LightweightRAG` 无缓存 | 在 `GraphDataService` 中按 session_id 缓存实例 |
| 7 | `sft_exporter` 功能不全 | 补充日期过滤、tool_calls 链路、文件下载 |
| 8 | 测试严重不足 | 补充至少 20 个计算工具测试 + planner/analyzer 单元测试 |

### P2 — 建议优化

| # | 问题 | 建议 |
|---|------|------|
| 9 | storage 模块未接入 | 要么完成接入，要么暂时删除 |
| 10 | JobManager 未使用 | 接入 train/infer 异步执行 + progress 推送 |
| 11 | ToolSelector/SemanticMemoryIndex 混在同一文件 | 拆分到独立模块 |
| 12 | Planner fallback 过于简单 | 扩展关键词覆盖或使用 algorithm_catalog 的 task_type 匹配 |
| 13 | SemanticMemoryIndex 去重逻辑 | 使用 `id()` 或显式 key 去重 |
| 14 | chat_stream 中 plan 模式的 final_answer 可能重复输出 | 检查 _run_react_loop 的 token 事件与 plan_complete 的 answer 是否冲突 |

---

## 七、功能缺失清单

以下是 plan_final.md 明确要求但尚未实现的功能：

### Phase 0 缺失

- [ ] 算法覆盖达到 50+ 种（当前约 40+，部分 catalog 条目的 description 过于泛化）
- [ ] ToolSelector text 协议 token 降低 ≥ 40% 的验证（环境变量门控导致功能未生效）
- [ ] "关键人物" 能召回含 "核心节点" 的记忆（语义功能同样未生效）

### Phase 1 缺失

- [ ] 子 Agent 判断延迟 < 500ms（当前使用主模型，预期 > 1s）
- [ ] provider_gateway.py 支持 `purpose="complexity_judge"` 路由
- [ ] 复杂任务成功率高于纯 ReAct 模式（人工评估 10 case）

### Phase 2 缺失

- [ ] Text→Graph 的 LLM 三元组抽取（当前只有启发式）
- [ ] PDF 文档上传支持（plan 提到但 RAG 只处理纯文本）
- [ ] schema.json 数据集字段定义
- [ ] 数据集绑定在 GraphML 训练中的利用

### Phase 3 缺失

- [ ] output_format 在 chat 流程中实际消费（当前仅接受参数但不改变行为）
- [ ] Trainer progress_callback 接入 JobManager
- [ ] report 生成与持久化（session_store.save_report 存在但未调用）
- [ ] SFT 导出的完整格式支持

### 横切缺失

- [ ] Cytoscape.js 图可视化
- [ ] 算法结果 highlight 字段
- [ ] 完整测试套件

---

## 八、总体评价

### 优点

1. **骨架搭建速度快**: 在约 2030 行新增/修改中覆盖了 plan 的四个 Phase，模块拆分清晰
2. **不破坏已有架构**: 三层服务分工、tool_catalog 单一真相源、Protocol 双栈设计均未受影响
3. **关键链路可用**: 图快照 + undo、算法扩展、Dataset Registry、前端 Plan 面板的核心功能可以直接使用
4. **降级思路正确**: Planner/QueryAnalyzer/ToolSelector 都有 fallback 路径

### 不足

1. **核心新功能未真正激活**: ToolSelector 语义检索 + SemanticMemoryIndex 因环境变量门控默认禁用，Plan 0.3 和 0.4 的核心价值未兑现
2. **Plan 模式有关键 bug**: 变量遮蔽导致 state 丢失，需要立即修复才能使用
3. **Phase 2/3 多为骨架**: TextExtractor 无 LLM、RAG 无缓存、Storage 未接入、JobManager 未使用、SFT 简化过度
4. **测试严重缺失**: 2 个新测试 vs 计划的 20+ 个，任何重构都缺乏安全网
5. **辅助 LLM 成本未控制**: 每轮对话都用主模型做复杂度判断，违背 plan 的成本控制设计

### 建议下一步

1. **先修 P0 bug**（变量遮蔽、环境变量门控、TextExtractor LLM 注入、graph_info_payload）
2. **补充核心测试**（至少覆盖新增算法和 Planner）
3. **然后做一轮端到端验证**: 简单查询 → Direct 模式正常；复杂查询 → Plan 模式生成合理计划并执行成功
4. **Phase 2/3 骨架代码按需激活**: storage、job_manager、sft_exporter 等暂不接入也不影响主流程
