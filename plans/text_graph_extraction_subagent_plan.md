# 文本图抽取 Sub-Agent 改造计划

> 制定日期：2026-04-28  
> 目标：将当前 `build_graph_from_text` 的规则抽取路径改造为 Agent 侧文本图抽取 Sub-Agent，并用确定性的三元组建图工具替代旧工具。旧 `build_graph_from_text` 不保留。

---

## 一、背景

当前系统中，文本建图入口是 GraphData 工具 `build_graph_from_text`：

```text
build_graph_from_text(text)
  -> GraphDataService.do_build_graph_from_text()
  -> TextExtractor.extract()
  -> GraphBuilder.build_from_triples()
  -> GraphSession.set_graph()
```

但 `GraphDataService` 初始化 `TextExtractor()` 时没有注入 LLM，因此实际执行的是启发式规则抽取，而不是模型抽取。这个设计有几个问题：

1. 工具名暗示“从文本抽取实体关系”，但真实能力主要是正则规则，复杂自然语言效果有限。
2. GraphData Service 作为数据和计算服务，不适合承担 LLM 调用、长文本分块、实体归一、冲突处理等 Agent 任务。
3. 长文本一次性抽取容易超上下文、输出截断、遗漏关系，也难以稳定去重。
4. 当前 `GraphBuilder.build_from_triples()` 没有保存 `relation` 字段，关系语义会丢失。

因此需要把“理解文本并抽取关系”的职责上移到 Agent 层，把 GraphData 层收敛为“接收结构化三元组并确定性建图”。

---

## 二、目标与非目标

### 2.1 目标

1. 新增文本图抽取 Sub-Agent，负责从短文本和长文本中抽取实体关系三元组。
2. 支持长文本分块、逐块抽取、全局三元组累积、去重、实体/关系规范化。
3. 删除旧 `build_graph_from_text` 工具，不保留旧规则抽取入口。
4. 新增确定性工具 `build_graph_from_triples`，由 GraphData Service 负责把三元组物化为当前会话图。
5. 保留三元组中的 `relation` 信息，作为边类型、边名称或边文本的一部分写入 `GraphData`。
6. 抽取结果必须带证据、置信度和来源 chunk，便于解释与调试。

### 2.2 非目标

1. 不在 GraphData Service 内直接调用 LLM。
2. 不继续维护 `TextExtractor` 作为主要抽取路径。
3. 不保留 `build_graph_from_text` 作为 fallback 工具。
4. 不在第一版实现复杂知识图谱本体、事件抽取、时间线推理或跨文档实体链接。
5. 不引入外部数据库；第一版三元组累积和去重都在内存中完成。

---

## 三、目标架构

```text
User text / uploaded document text
    |
    v
LLM → call build_graph_from_text(text, extraction_goal, directed, append)
    |
    v  [tool dispatch — MCP Server → GraphDataService → Sub-Agent]
TextGraphExtractionSubAgent
    |
    +-- chunk text
    +-- extract triples per chunk with LLM
    +-- validate JSON schema
    +-- accumulate triples
    +-- normalize entities / relations
    +-- dedupe and resolve conflicts
    |
    v
GraphBuilder.build_from_triples(triples)
    |
    v
GraphSession.set_graph()
    |
    v  [tool return]
{ nodes, edges, chunks, raw_triples, deduped_triples, skipped, warnings, sample_triples }
    |
    v
LLM 向用户解释结果
```

LLM **不接触**中间 triples，只看最终摘要。`build_graph_from_triples` 作为独立工具继续暴露，供有结构化三元组的场景直接使用（外部知识库导入、程序生成等）。

职责边界：

| 层 | 负责 | 不负责 |
|---|---|---|
| LLM (iGraph) | 决定调用哪个工具、解释返回结果 | 中间 triples 的处理 |
| `build_graph_from_text` tool dispatch | 接收文本参数、调用 Sub-Agent、转发建图、返回摘要 | 图算法 |
| TextGraphExtractionSubAgent | 文本理解、分块、LLM 抽取、去重、规范化 | 存储会话图 |
| GraphData Service / GraphBuilder | 校验 triples、确定性建图、保存快照 | LLM 抽取 |

---

## 四、工具改造

### 4.1 替换 `build_graph_from_text` 的后端实现

**保留工具名和对 LLM 的可见性**，只替换内部实现：旧路径（`TextExtractor` 规则抽取）→ 新路径（`TextGraphExtractionSubAgent`）。

需改动的文件：

| 文件 | 改动 |
|---|---|
| `graph_data_service/tool_catalog.py` | 更新 `build_graph_from_text` schema：description 改为说明 Sub-Agent 能力；新增 `extraction_goal`、`append` 参数 |
| `graph_data_service/app.py` | ① 将 `do_build_graph_from_text()` 改为调用 Sub-Agent 并返回摘要（不再使用 `TextExtractor`）；② 保留 `ASYNC_TOOLS` 中的 `"build_graph_from_text"`（仍是异步）；③ 删除 `self._text_extractor = TextExtractor()`（约第 109 行）；④ 清理第 18 行的 `TextExtractor` import |
| `graph_data_service/data_pipeline/text_extractor.py` | 删除文件（测试迁移完成后） |
| `graph_data_service/data_pipeline/__init__.py` | 移除 `TextExtractor` 导出，仅保留 `GraphBuilder` |
| `backend/agent/skills/data_ingestion_text/SKILL.md` | 更新 description，说明工具现在支持长文本、分块抽取；`any_tools` 保持 `[build_graph_from_text]` 不变 |
| tests | 更新测试：验证新返回字段（摘要格式），不再测试规则抽取行为 |

更新后的工具 schema：

```json
{
  "name": "build_graph_from_text",
  "description": "从自然语言文本中抽取实体关系并构建图到当前会话。支持长文本自动分块、逐块 LLM 抽取、去重和规范化。返回摘要统计，不返回中间三元组。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "text": {"type": "string"},
      "directed": {"type": "boolean", "default": true},
      "append": {"type": "boolean", "default": false},
      "extraction_goal": {
        "type": "string",
        "description": "可选，指导抽取方向，例如"只抽取人物和公司关系""
      }
    },
    "required": ["text"]
  }
}
```

工具返回格式（JSON 字符串）：

```json
{
  "success": true,
  "nodes": 92,
  "edges": 137,
  "chunks_processed": 12,
  "raw_triples": 184,
  "deduped_triples": 137,
  "skipped": 5,
  "warnings": ["指代不明：'该公司' 出现 3 次，未归一"],
  "sample_triples": [
    {"source": "张三", "relation": "就职于", "target": "某公司", "confidence": 0.88}
  ]
}
```

### 4.2 新增工具 `build_graph_from_triples`

`build_graph_from_text` 内部会调用此工具的 **Python 逻辑**，但 LLM 不需要经过它。作为独立工具保留，供有结构化数据的场景使用（外部知识库导入、程序化生成三元组等）。

建议工具 schema：

```json
{
  "name": "build_graph_from_triples",
  "description": "从结构化实体关系三元组构建图并加载到当前会话。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "triples": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "source": {"type": "string"},
            "relation": {"type": "string"},
            "target": {"type": "string"},
            "confidence": {"type": "number"},
            "evidence": {"type": "string"},
            "chunk_id": {"type": "integer"}
          },
          "required": ["source", "relation", "target"]
        }
      },
      "directed": {"type": "boolean", "default": true},
      "append": {"type": "boolean", "default": false}
    },
    "required": ["triples"]
  }
}
```

第一版字段映射（固定，不暴露配置参数）：

- `relation` → `edge_type`（`GraphData._edge_type`，`np.ndarray[object]`）。
- `evidence` → `edge_text`（`GraphData._edge_text`，`List[str]`）。
- `confidence` → `edge_feat`（`GraphData._edge_feat`，shape `[E, 1]` float32）；若 triple 无 confidence 则全填 `1.0`。
- `append=false` 覆盖当前图；`append=true` 与当前图合并。

> **注意**：早期草案曾设计 `relation_field` 参数（enum: edge_type / edge_text / edge_names），但 `relation_field="edge_text"` 会与 `evidence` 写入位置冲突，且 GraphData 字段名为 `edge_names`（复数）而非 `edge_name`。第一版直接固定映射，后续有需求再扩展。本工具是确定性同步操作，**不加入 `ASYNC_TOOLS`**。

### 4.3 GraphBuilder 改造

`build_from_triples` 已存在（`graph_data_service/data_pipeline/graph_builder.py:64`），当前签名为：

```python
# 现状：只映射 source/target，忽略 relation/evidence/confidence
GraphBuilder.build_from_triples(triples: Iterable[dict], *, directed: bool = True) -> GraphData
```

需改造为：

```python
GraphBuilder.build_from_triples(
    triples: Iterable[dict],
    *,
    directed: bool = True,
) -> GraphData
```

改造要求（**不改变方法签名**，从 triple dict 中自动读取新字段）：

1. 跳过空 source / target。
2. `relation` 为空时使用 `"related_to"`；非空时收集为 `edge_type` 列表。
3. `evidence` 非空时收集为 `edge_text` 列表；全为空则不传 `edge_text`。
4. `confidence` 收集为 `edge_feat`（`[[c], [c], ...]`，shape `[E, 1]`）；缺失时填 `1.0`。
5. 保留节点名称映射（`id2node` / `node2id`）。
6. 对输入 triples 做基础 schema 校验（source/target 必须是非空字符串），跳过错误条目并返回 skipped 计数。

---

## 五、Sub-Agent 设计

### 5.1 新文件

```text
backend/agent/sub_agents/text_graph_extractor.py   # Sub-Agent 主体
backend/agent/sub_agents/text_chunker.py            # 分块逻辑（Phase 4 新增）
```

核心类：

```python
class TextGraphExtractionSubAgent:
    def __init__(self, llm: BaseChatModel) -> None:
        # LLM 实例由调用方（iGraph）通过 create_llm() 创建后注入，
        # Sub-Agent 不自行创建 LLM，避免重复初始化和 API key 管理混乱。
        self._llm = llm

    async def extract(
        self,
        text: str,
        *,
        directed: bool = True,
        max_chunk_tokens: int = 3000,
        overlap_tokens: int = 250,
        extraction_goal: str = "",
    ) -> TextGraphExtractionResult:
        ...
```

结果结构：

```python
@dataclass
class TextGraphExtractionResult:
    triples: list[dict]          # 每条 triple 含 source/relation/target/evidence/confidence/chunk_ids
    raw_triple_count: int        # 去重前候选总数
    deduped_triple_count: int    # 去重后保留数
    skipped_count: int           # 因 schema 错误被丢弃的条数
    chunk_count: int
    entity_aliases: dict[str, list[str]]   # canonical -> aliases（全局 reconcile 后）
    relation_aliases: dict[str, list[str]] # canonical -> aliases
    warnings: list[str]
```

### 5.2 Sub-Agent prompt 约束

每块抽取时要求：

1. 只抽取文本明确支持的关系。
2. 每条 triple 必须包含 `source`、`relation`、`target`、`evidence`、`confidence`。
3. `evidence` 必须来自当前 chunk，不能编造。
4. 不要用常识补全。
5. 不要把标题、列表编号、章节名误当关系。
6. 遇到代词、指代不明、跨段缺失时降低置信度或放入 warning。
7. 只输出 JSON。

建议单块输出：

```json
{
  "triples": [
    {
      "source": "张三",
      "relation": "就职于",
      "target": "某公司",
      "confidence": 0.88,
      "evidence": "张三于 2020 年加入某公司",
      "notes": ""
    }
  ],
  "entity_alias_candidates": [
    {"canonical": "某公司", "aliases": ["该公司"]}
  ],
  "warnings": []
}
```

---

## 六、长文本分块策略

### 6.1 为什么必须分块

长文本直接送入一次 LLM 抽取会遇到：

1. 上下文过长导致模型忽略中间内容。
2. 输出 triples 过多导致截断。
3. 成本高且失败后需要整体重试。
4. 去重和错误定位困难。

因此长文本应先分块，再逐块抽取。

### 6.2 分块规则

第一版使用确定性分块：

1. 优先按 Markdown 标题切分。
2. 再按段落切分。
3. 段落过长时按句子切分。
4. 仍过长时按 token/字符硬切。
5. chunk 之间保留 overlap，默认 250 tokens。
6. 每个 chunk 记录 `chunk_id`、起止字符偏移、标题路径。

建议默认：

| 参数 | 默认值 |
|---|---|
| `max_chunk_tokens` | 3000 |
| `overlap_tokens` | 250 |
| `max_output_triples_per_chunk` | 80 |

### 6.3 分块带来的问题

分块不是银弹，会引入以下风险：

1. **跨块关系丢失**：source 在上一块，target 在下一块，关系句被切断。
2. **指代消解困难**：例如“他”“该公司”“上述模型”可能依赖前文。
3. **重复抽取**：overlap 区域会重复产生同一 triple。
4. **实体别名不一致**：同一实体在不同块中名字不同。
5. **关系词漂移**：同一关系可能被抽成“就职于”“工作于”“employed_by”。
6. **局部误判累积**：单块误抽取进入全局集合后会污染图。
7. **全局约束缺失**：时间线、层级关系、章节语境可能需要跨块理解。

应对策略：

| 风险 | 第一版缓解 |
|---|---|
| 跨块关系丢失 | overlap + evidence 必填，无法确认则不抽 |
| 指代消解 | 保留 warning，不强行补全 |
| 重复抽取 | TripleAccumulator 去重 |
| 实体别名 | 规则规范化 + LLM 全局 reconcile |
| 关系漂移 | relation normalization |
| 局部误判 | confidence 阈值 + evidence 审计 |
| 全局约束缺失 | 二阶段全局复核 |

---

## 七、TripleAccumulator

新增：

```text
backend/agent/sub_agents/triple_accumulator.py
```

职责：

1. 接收每个 chunk 的 triples。
2. 规范化 source/relation/target。
3. 用 canonical key 去重。
4. 合并 evidence、chunk_ids、confidence。
5. 输出稳定排序后的 triples。

### 7.1 去重 key

第一版：

```text
normalize_entity(source) + "\t" + normalize_relation(relation) + "\t" + normalize_entity(target)
```

`normalize_entity()`：

- trim
- 合并连续空白
- 去掉常见包裹标点
- 英文 lower
- 全角半角规整

`normalize_relation()`：

- trim
- lower 英文
- 空白转 `_`
- 常见关系别名映射，例如：
  - `就职于` / `工作于` / `works at` / `employed by` -> `works_at`
  - `属于` / `is part of` / `belongs to` -> `belongs_to`
  - `连接到` / `links to` / `connects to` -> `connects_to`

### 7.2 合并策略

同一 key 的重复 triple：

- `confidence`: 取最大值，同时保留平均值可选。
- `evidence`: 最多保留前 3 条不同证据。
- `chunk_ids`: 去重排序。
- `mentions`: 计数加一。

### 7.3 全局 reconcile

长文本抽取结束后，可选跑一次 LLM reconcile：

输入：

- 高频实体列表
- 高频关系列表
- 低置信度或冲突 triple 样本

输出：

- entity alias map
- relation alias map
- conflicts / warnings

第一版可以把 reconcile 设为可选开关，避免实现过重。

---

## 八、Agent 编排

### 8.1 iGraph 集成

方案 B 下，iGraph **无需新增意图检测逻辑**。LLM 根据工具描述自行决定调用 `build_graph_from_text`，工具内部完成整个流水线并返回摘要。iGraph 对这次工具调用的处理与其他工具完全一致：

1. LLM 决定调用 `build_graph_from_text(text=..., extraction_goal=...)`。
2. MCP Server 转发到 GraphDataService → Sub-Agent 流水线执行。
3. 工具返回摘要 JSON（nodes/edges/chunks/warnings 等）。
4. LLM 用摘要向用户解释结果，可选调用 `get_graph_info` 获取更多细节。
5. `_has_graph` / 节点数 / 边数由 `build_graph_from_text` 返回值直接更新，无需额外查询。

### 8.2 工具路由

| 输入类型 | 工具 |
|---|---|
| 明确边列表 `src dst [weight]` | `load_graph` |
| 自然语言文本 / 文章 / 文档描述 | `build_graph_from_text` |
| 上传图文件（.graphml / .json 等） | `load_graph_from_upload` |
| 上传普通文档并要求建图 | 先用 `retrieve_context` 获取文本，再 `build_graph_from_text` |
| 已有结构化三元组（外部导入） | `build_graph_from_triples` |

---

## 九、用户可见行为

成功建图后，最终回答应包含：

1. 分块数量。
2. 原始抽取 triples 数。
3. 去重后 triples 数。
4. 建成图的节点数、边数。
5. 低置信度或跳过原因摘要。
6. 可选展示 5 条样例三元组。

示例：

```text
已从文本构建图：共处理 12 个文本块，抽取 184 条候选关系，去重后保留 137 条。
当前图包含 92 个节点、137 条边。关系已写入 edge_type，证据写入 edge_text。
有 9 条关系因指代不明被跳过，主要集中在“该公司”“上述模型”等表达。
```

失败时：

```text
没有从文本中抽取到足够明确的实体关系，因此没有创建图。
文本中存在大量指代或抽象描述，但缺少明确的 source-relation-target 结构。
```

---

## 十、测试计划

### 10.1 GraphData 工具测试

新增：

```text
tests/graph_data/test_build_graph_from_triples.py
```

覆盖：

1. 基础 triples -> GraphData。
2. relation 写入 edge_type。
3. evidence 写入 edge_text。
4. `directed=false` 时写入双向边。
5. 空 triples 返回错误。
6. 缺 source / target 的 triple 被拒绝或跳过并报告。
7. append 模式可与当前图合并。

### 10.2 Sub-Agent 单元测试

新增：

```text
tests/agent/test_text_graph_extractor.py
tests/agent/test_triple_accumulator.py
```

覆盖：

1. 长文本分块不超过阈值。
2. overlap 造成的重复 triple 会合并。
3. relation alias 能归一。
4. evidence 和 chunk_id 被保留。
5. schema 错误输出能被修复或丢弃。
6. 空抽取不会调用建图工具。

### 10.3 集成测试

覆盖：

1. 自然语言文本建图走 sub-agent，不调用旧工具。
2. 明确边列表仍走 `load_graph`。
3. 删除 `build_graph_from_text` 后工具 catalog 不再暴露旧工具。
4. iGraph 最终能更新 `_has_graph`、节点数和边数。

---

## 十一、迁移步骤

### 阶段 1：确定性建图基础

1. 新增 `build_graph_from_triples` schema（tool_catalog）。
2. 增加 GraphDataService dispatch（同步路径）。
3. 改造 `GraphBuilder.build_from_triples`，保留 relation → edge_type、evidence → edge_text、confidence → edge_feat。
4. 加 GraphData 工具测试。

### 阶段 2：替换 `build_graph_from_text` 后端

1. 更新 `build_graph_from_text` schema（tool_catalog）：添加 `extraction_goal`、`append` 参数，更新 description。
2. 新增 `TextGraphExtractionSubAgent`（`backend/agent/sub_agents/text_graph_extractor.py`），先支持短文本单次 LLM 抽取 + JSON schema 校验。
3. 改造 `do_build_graph_from_text()`：调用 Sub-Agent → 调用 `GraphBuilder.build_from_triples` → 返回摘要 JSON（不再使用 `TextExtractor`）。
4. 删除 `self._text_extractor`、`TextExtractor` import；删除 `data_pipeline/__init__.py` 中的 `TextExtractor` 导出。
5. 删除或隔离 `text_extractor.py`（测试全部迁移后）。
6. 更新 skills SKILL.md 和相关测试。

> **零中断**：阶段 1 和阶段 2 步骤 1–2 可先完成，确保 Sub-Agent 可用后再执行步骤 3（切换 dispatch），同一 PR 提交。

### 阶段 3：长文本分块与累积

1. 新增 `backend/agent/sub_agents/text_chunker.py`（`TextChunker` 类）。
2. 新增 `backend/agent/sub_agents/triple_accumulator.py`（`TripleAccumulator`）。
3. Sub-Agent 支持逐块抽取和进度事件。
4. 支持去重、证据合并、warning 汇总。

### 阶段 4：全局规范化与质量报告

1. 增加 relation alias map。
2. 增加可选 entity reconcile。
3. 抽取报告写入工具返回摘要。
4. 加长文本集成测试。

---

## 十二、风险与决策点

### 12.1 是否并行处理 chunks

第一版建议串行或小并发：

- 串行最简单，日志和失败恢复清晰。
- 小并发可以提高速度，但需要保证 accumulator 合并顺序稳定。

建议先串行，后续加 `max_concurrency=3`。

### 12.2 是否允许用户确认三元组再建图

建议第一版支持参数：

```text
preview_only=false
```

但默认直接建图。对于高风险或低置信度比例过高的抽取，Agent 可以先展示预览并请求用户确认。

### 12.3 关系应写入哪里

第一版固定映射（不通过参数控制）：

- `edge_type` = normalized relation（`GraphData._edge_type`）
- `edge_text` = evidence（`GraphData._edge_text`）
- `edge_feat[:,0]` = confidence（`GraphData._edge_feat`，shape [E,1]）

`confidence` 不存入 `edge_text`，避免与 evidence 混淆。后续需要更丰富 metadata 时，扩展 `edge_feat` 列或新增 `edge_metadata` 字段。

### 12.4 工具内嵌 LLM 调用的风险

`build_graph_from_text` 工具在 GraphDataService dispatch 中会调用 Sub-Agent，Sub-Agent 再对每个 chunk 发起 LLM 请求。这意味着**一次工具调用可能触发数十次 LLM 请求**，与普通确定性工具有本质不同。

潜在风险：

| 风险 | 缓解 |
|---|---|
| 超时：长文本分块多，总耗时可能超 MCP/HTTP 超时 | 提高 GraphDataService dispatch 的超时阈值；或改为返回 `job_id`，异步轮询 |
| 嵌套计费：主 Agent 的 LLM + Sub-Agent 的 LLM 同时计费 | 第一版接受；后续可复用主 Agent 的 LLM 连接 |
| 错误传播：Sub-Agent 某块失败不应使整个工具调用失败 | Sub-Agent 对每块 try/catch，失败块计入 `warnings`，继续处理其他块 |
| token 膨胀：Sub-Agent prompt × 块数 | `max_chunk_tokens` 和 `max_output_triples_per_chunk` 限制单块开销 |

### 12.5 兼容性

`build_graph_from_text` 工具名保留，旧测试和外部 MCP 客户端**无需修改工具调用方式**，但返回字段从简单 `{success, message}` 变为摘要 JSON，需更新断言。

---

## 十三、完成标准

1. `/tools` 仍返回 `build_graph_from_text`，但 schema 包含 `extraction_goal` / `append` 参数。
2. `/tools` 返回 `build_graph_from_triples`（新增，供结构化输入使用）。
3. 调用 `build_graph_from_text` 时，内部由 `TextGraphExtractionSubAgent` 完成抽取，不再走规则路径。
4. 工具返回摘要 JSON，包含 nodes / edges / chunks / raw_triples / deduped_triples / warnings。
5. GraphData Service 不再持有 `TextExtractor` 实例，不再依赖正则规则抽取。
6. 长文本可分块处理，并能去重合并 triples。
7. relation → edge_type、evidence → edge_text、confidence → edge_feat，不再丢失。
8. 明确边列表仍能通过 `load_graph` 正常加载。
9. 所有新增和迁移测试通过。

