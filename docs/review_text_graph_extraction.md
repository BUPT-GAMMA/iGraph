# Code Review: Text Graph Extraction Sub-Agent

> 审查日期：2026-04-28  
> 分支：`codex/search-entity-tools`  
> 审查范围：基于 `plans/text_graph_extraction_subagent_plan.md` 的全量实现

---

## 总体评价

实现整体符合方案 B 设计，层次边界清晰，核心流水线（chunk → extract → dedup → build）可以跑通。`GraphBuilder`、`TripleAccumulator` 质量较高，`app.py` 的改造也基本到位。存在若干中高严重性 bug 需要修复后才能在生产中可靠运行。

---

## 一、高严重性问题

### H1 `default_max_tokens = 4096` 可能截断 JSON 输出

**文件**：`backend/agent/sub_agents/text_graph_extractor.py:42`

每个 chunk 最多 3000 tokens，加上 evidence 原文的 JSON 序列化，80 条 triple 的输出轻松超过 4096 tokens。截断后 `_parse_response` 会拿到损坏的 JSON，触发 exception，该 chunk 被 skip 并记入 warning，但用户不会知道是 token 限制导致的丢失。

```python
# 当前
default_max_tokens = 4096

# 建议
default_max_tokens = 8192   # 或与 max_chunk_tokens 关联，按 max_chunk_tokens * 2 估算
```

---

### H2 `entity_alias_candidates` 被忽略，`entity_aliases` / `relation_aliases` 恒为空

**文件**：`backend/agent/sub_agents/text_graph_extractor.py:70–80`

每个 chunk 的 LLM 响应里包含 `entity_alias_candidates`，但 extractor 只读 `triples` 和 `warnings`，alias 数据直接丢弃。`TextGraphExtractionResult.entity_aliases` 和 `relation_aliases` 始终是空字典，既不传给 `TripleAccumulator`，也没有任何后续处理。

这意味着方案里"实体别名规范化"的功能完全未实现。

```python
# text_graph_extractor.py 的 extract() 中需要补充：
alias_candidates = parsed.get("entity_alias_candidates") or []
if isinstance(alias_candidates, list):
    accumulator.register_alias_candidates(alias_candidates)
```

`TripleAccumulator` 需要配套的 `register_alias_candidates()` 方法和归并逻辑。

---

## 二、中严重性问题

### M1 `_parse_response` 用了 `@classmethod` 但不使用 `cls`

**文件**：`backend/agent/sub_agents/text_graph_extractor.py:109`

```python
@classmethod
def _parse_response(cls, text: str) -> dict[str, Any]:
    payload = cls._extract_json_object(text)   # _extract_json_object 是 staticmethod
    data = json.loads(payload)
    ...
```

`cls` 未使用。应改为 `@staticmethod`，与 `_extract_json_object` 保持一致。

---

### M2 `do_build_graph_from_triples` 的 `deduped_triples` 字段语义不准确

**文件**：`graph_data_service/app.py:388–415`

```python
valid_count = self._count_valid_triples(triples)   # 统计 source/target 非空的条数
...
"deduped_triples": valid_count,   # 命名暗示去重后数量，实际是过滤后数量
```

`_count_valid_triples` 只过滤空 source/target，不做去重。若传入 100 条有 30 条重复 triple，`deduped_triples=100` 而实际入图边数可能是 70。字段名会误导调用方。

建议：
- 改为 `valid_triples`，与 `nodes`/`edges` 一起让调用方自行判断；  
- 或用 `graph.num_edges` 作为实际结果。

---

### M3 `_merge_graphs` 中 undirected 图的 edge 语义有歧义

**文件**：`graph_data_service/app.py:524–552`

```python
merged = GraphBuilder.build_from_triples(triples, directed=True)  # 始终 directed=True
if directed:
    return merged
return GraphData(..., directed=False)   # 包装为 undirected，但未添加反向边
```

当 `directed=False` 且 existing graph 是 **有向图**时，`_graph_to_triples` 只返回单向 triple，`build_from_triples(directed=True)` 不添加反向边，最终 `GraphData(directed=False)` 的边只有单向，与"无向图存储双向边"的约定不符。

建议直接用 `directed` 参数调用 `build_from_triples`：

```python
merged = GraphBuilder.build_from_triples(triples, directed=directed)
return merged
```

---

### M4 `build_from_triples` 始终写入 `edge_feat`，可能影响现有逻辑

**文件**：`graph_data_service/data_pipeline/graph_builder.py:103`

```python
edge_feat = np.asarray(edge_features, dtype=np.float32)   # 全为 1.0 时也写入
return GraphData(..., edge_feat=edge_feat, ...)
```

原 `build_from_triples` 不写 `edge_feat`（`None`）。改造后，即使所有 confidence 都是默认值 1.0，`edge_feat` 也不为 `None`。依赖 `graph.edge_feat is None` 判断"是否有特征"的下游代码（如 ML readiness check）行为会发生变化。

建议仅当存在非默认 confidence 时才写入：

```python
has_confidence = any(c != 1.0 for c in (item.get("confidence") for item in triples) if c is not None)
edge_feat_arg = np.asarray(edge_features, dtype=np.float32) if has_confidence else None
```

或保持始终写入但更新 ML readiness check 的判断条件。

---

### M5 `_create_text_graph_provider_gateway` 异常范围过宽

**文件**：`graph_data_service/app.py:137–164`

```python
try:
    model = normalize_model_name(model_raw)
    provider = provider_for_model(model)
except Exception:   # ← 包括编程错误
    return None
```

`except Exception` 会吞掉 `NameError`、`ImportError` 等编程错误，使得配置错误在启动时静默失败。建议只捕获预期的配置异常：

```python
except (ValueError, KeyError):
    return None
```

---

## 三、低严重性问题

### L1 `TextChunker._split_large_unit` 字符偏移计算不可靠

**文件**：`backend/agent/sub_agents/text_chunker.py:101`

```python
piece_start = absolute_start + text.find(piece, offset)
```

`text.find(piece, offset)` 对重复出现的 piece（如"的"、短标点序列）会找到错误位置。`start_char` / `end_char` 字段存入 `TextChunk` 但当前未被 extraction 逻辑使用（只用 `chunk_id` 和 `text`），所以暂时不影响功能，但偏移字段对未来引用溯源会产生误导。

建议改用 `re.finditer` 记录准确偏移，或保留但加注释说明当前字段的准确性限制。

---

### L2 chunk_id 在 `add_triples` 中可能被重复记录

**文件**：`backend/agent/sub_agents/triple_accumulator.py:60–70`

`add_triples` 方法同时从 `item["chunk_id"]`（extractor 已设置）和外部 `chunk_id` 参数收集 chunk_id，两者通常相同。由于使用 `set`，实际结果正确，但逻辑冗余，阅读时容易混淆。

建议只保留一种来源：要么由 extractor 在 triple 里设置，要么由 accumulator 参数注入，选一个。

---

### L3 测试覆盖缺口

**文件**：`tests/servers/test_graph_data_app.py`

当前测试使用单 chunk 的 fake LLM，未覆盖以下场景：

| 缺失场景 | 建议测试方法 |
|---|---|
| `build_graph_from_triples` 端到端（HTTP 路径）| 补一个直接调用该工具的测试 |
| `append=True` 合并语义 | 先建图，再 append，验证 nodes/edges 增长 |
| LLM 不可用时的错误路径 | `GraphDataService()` 不传 factory，验证返回 error 字段 |
| 长文本触发多 chunk | fake_llm 被调用次数等于 chunk 数，验证去重发生 |
| `edge_type` / `edge_text` / `edge_feat` 字段写入 | 查询 `session.get_graph()` 的字段值 |

---

### L4 `_prompt_invoker` 路径的 system prompt 降级

**文件**：`backend/agent/sub_agents/base.py:67–69`，`text_graph_extractor.py:15`

使用 `llm_factory`（`_prompt_invoker`）时，system prompt 通过 `compose_prompt()` 前缀到 user prompt，而不是作为独立的 system message。对 chat 模型来说这是降级处理。当前只有 `text_graph_llm_factory` 测试入口走这条路径，生产走 `provider_gateway`，影响有限，但值得记录。

---

## 四、设计层面说明（非 bug）

以下是实现与计划的偏差，但偏差合理，供参考：

**Phase 3（长文本分块）与 Phase 2 合并实现**：方案分阶段，但 Codex 将 chunker 和 accumulator 一并交付。这是好的，避免了中间状态的不稳定版本。

**`entity_alias_candidates` 字段**：prompt 中要求 LLM 输出此字段，但 extractor 未消费。这对应 Phase 4（全局规范化）尚未实现，属于已知缺口，与计划一致。

**`relation_field` 参数未实现**：计划在最终审定时已移除此参数，固定映射。实现与最终方案一致。

---

## 五、修复优先级

| 优先级 | 项目 | 工作量 |
|---|---|---|
| **必修（上线前）** | H1：提高 `default_max_tokens` | 1 行 |
| **必修（上线前）** | M1：`_parse_response` 改为 `@staticmethod` | 1 行 |
| **必修（上线前）** | M2：`deduped_triples` 改为 `valid_triples` 或用实际边数 | 3 行 |
| **建议修复** | M3：`_merge_graphs` 统一 directed 参数 | 1 行 |
| **建议修复** | M4：`edge_feat` 按需写入 | ~5 行 |
| **建议修复** | M5：缩窄异常捕获范围 | 1 行 |
| **后续迭代** | H2：实现 alias candidate 收集与 reconcile | ~50 行 |
| **后续迭代** | L3：补充测试用例 | ~80 行 |
| **低优先级** | L1：chunker 字符偏移修正 | ~20 行 |
| **低优先级** | L2：消除 chunk_id 双重记录 | ~5 行 |
