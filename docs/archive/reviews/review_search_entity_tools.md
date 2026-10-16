# Code Review: search_node / search_edge 实体检索工具

> 审查日期：2026-04-28  
> 审查范围：`graph_data_service/embedding.py`（新增）、`graph_data_service/search.py`（新增）、`graph_data_service/rag.py`（改造）、`graph_data_service/app.py`（改造）、`graph_data_service/tool_catalog.py`（改造）、`tests/graph_data/test_entity_search.py`（新增）

---

## 总体评价

实现完整覆盖了计划的 Phase 1–3，结构清晰，架构与设计方案高度吻合。测试全部通过（9/9）。以下问题按优先级排列：**P0（bug）→ P1（显著设计问题）→ P2（设计优化）→ P3（测试缺口）**。

---

## P0：Bug

### 1. `embedding.py:125-127` — 捕获了过宽的异常

```python
# 现状
try:
    return SentenceTransformerProvider()
except Exception:     # ← 过宽
    pass
```

**问题**：`SentenceTransformerProvider.__init__` 除 `ImportError` 之外，还可能抛出 OOM（模型加载耗尽内存）、磁盘满、文件损坏等真正的错误。当前实现会把这些错误全部吞掉，静默降级到 lexical 或 OpenAI，调用方和用户完全无感知。

**修复**：

```python
try:
    return SentenceTransformerProvider()
except ImportError:
    pass
except Exception as exc:
    import logging
    logging.getLogger(__name__).warning("SentenceTransformerProvider init failed: %s", exc)
    pass
```

---

## P1：显著设计问题

### 2. `SentenceTransformerProvider.dim = 384` 是类变量，非默认模型时值错误

```python
class SentenceTransformerProvider:
    name = "sentence-transformers"
    dim = 384   # ← 硬编码，all-MiniLM-L6-v2 专用
```

**问题**：如果使用 `all-mpnet-base-v2`（768 维）或 `bge-large-zh`（1024 维），`dim` 仍为 384，导致：

- `_load_embeddings()` 初始化空矩阵维度错误（`rag.py:94`）
- `EntitySearchIndex._node_matrix` 形状与查询向量不匹配，`@` 运算会抛 shape 错误

**修复**：在 `__init__` 中从模型实例读取：

```python
def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
    from sentence_transformers import SentenceTransformer
    self._model = SentenceTransformer(model_name)
    self.available = True
    self.dim = self._model.get_sentence_embedding_dimension()  # ← 从模型读取
```

---

### 3. `_build()` 和 `OpenAIEmbeddingProvider.encode()` 阻塞事件循环

`EntitySearchIndex.__init__` 调用 `_build()`，其中调用 `provider.encode()`：

- `SentenceTransformerProvider.encode()` 是 CPU 密集型（模型推理）
- `OpenAIEmbeddingProvider.encode()` 使用 `urllib.request.urlopen`（阻塞 I/O）

两者都在同步上下文中调用，而 `_get_search_index()` 在 FastAPI 的 async handler 链路中被调用，会直接阻塞事件循环。大图（数万节点）构建索引时，整个服务在此期间完全无响应。

**修复**：在 `do_search_node` / `do_search_edge` 中卸载到线程池：

```python
async def do_search_node_async(self, session_id: str, arguments: dict) -> dict:
    index = await asyncio.get_event_loop().run_in_executor(
        None, self._get_search_index, session_id
    )
    req = NodeSearchRequest(...)
    return index.search_nodes(req)
```

或将 `_get_search_index` 整体改为异步方法（构建时 `await loop.run_in_executor`）。

---

### 4. `rag.py:37-39` — 维度不匹配时静默丢弃历史数据

```python
if current.size and embeddings.size and current.shape[1] != embeddings.shape[1]:
    existing = existing[start:]    # 丢弃旧的 index 条目
    current = np.empty((0, embeddings.shape[1]), dtype=np.float32)
```

**问题**：用户切换 embedding provider 后再次上传文档，会静默清空之前上传的全部文档向量和索引，用户毫不知情。历史文档查询将无法召回。

**修复**：改为明确报错并提示，或在 `retrieve` 中检测后提示用户重新上传：

```python
if current.size and embeddings.size and current.shape[1] != embeddings.shape[1]:
    (self.docs_dir / f"{doc_id}.txt").write_text(content, encoding="utf-8")
    return {
        "success": False,
        "error": (
            "Embedding 维度不一致：现有文档使用 "
            f"{current.shape[1]} 维，当前 provider 输出 {embeddings.shape[1]} 维。"
            "请先清空会话或重新上传所有文档。"
        ),
    }
```

---

## P2：设计优化

### 5. `search.py` 双重归一化

`SentenceTransformerProvider.encode()` 已指定 `normalize_embeddings=True`，而 `_encode_docs()` 之后又调用 `_normalize_rows()`：

```python
# _encode_docs
matrix = self._provider.encode([...])
return self._normalize_rows(np.asarray(matrix, dtype=np.float32))  # ← 再次归一化
```

对单位向量再次归一化是幂等的（无 bug），但产生不必要的 `linalg.norm` 计算。建议让 provider 协议明确"输出已 L2 归一化"，`_encode_docs` 移除冗余归一化，或针对不同 provider 分支处理。

---

### 6. `search_edge` schema 的 `required` 与实现不一致

`tool_catalog.py` 中 `search_edge.inputSchema.required = ["query"]`，但：

- `EdgeSearchRequest.query` 默认值为 `""`
- `do_search_edge` 使用 `str(arguments.get("query") or "")` 接受缺失值

这意味着 LLM 可以不传 `query`（仅传 `src_query` + `dst_query`），但 schema 会报 validation error，某些严格 MCP 客户端会拒绝该调用。

**修复**：将 `required` 改为 `[]`，在 description 中说明三个查询参数至少提供一个即可：

```python
"required": [],
```

---

### 7. 语义关闭时多候选全部标 `ambiguous`

```python
if not self._semantic_enabled and len(rows) > 1:
    return "low", True
```

无语义时，通过 `node_type` filter 过滤出 2 个节点也被标为 `ambiguous=True`，LLM 会被迫向用户确认，即使结构约束已经足够精确（如 `filters={"node_type": "paper", "min_degree": 5}` 只剩 1 条）。此处条件已有 `len(rows) > 1` 保护，但过滤后仍有多条时保守程度过高。

建议：仅在 `rows > 1` 且没有任何 filter 约束时才触发此路径；有 filter 时 confidence 设为 `medium` 而非 `low`，`ambiguous=False`。

---

### 8. `_search_edges_by_endpoints` 中端点候选数硬编码为 20

```python
def _endpoint_scores(self, query: str, limit: int = 20) -> dict[int, float]:
```

对于稠密异质图（数千个同类型节点），`limit=20` 可能截断真实匹配节点，导致真实目标边找不到。

建议：将 `limit` 提升为 `min(50, num_nodes // 10)` 的自适应值，或作为 `EntitySearchIndex` 的构造参数。

---

## P3：测试缺口

计划中的 11 个测试用例，当前实现了 9 个，缺少：

| 缺少的用例 | 原因 | 建议 |
|---|---|---|
| `test_rag_with_real_provider` | rag.py 改造后无测试 | 使用 `KeywordProvider` 模拟：上传文档后，相关 query 能召回对应 chunk |
| `test_provider_fallback_order` | `default_provider()` 选择逻辑无测试 | Mock `SentenceTransformer` import 失败，验证降级到 `LexicalProvider` |

此外，以下场景未被覆盖：

- `test_search_edge_query_only`：不提供 `src_query` / `dst_query`，仅靠 `query` 召回边
- `test_search_node_integer_id`：`query="0"` 时的整数节点 ID 精确命中路径（`_exact_node_id` 的第二分支）
- `test_embedding_build_error_fallback`：provider `encode()` 抛异常时，`_build()` 正确 fallback 为 `semantic_enabled=False`

---

## 优点（值得保留的做法）

- **provider 注入模式**：`GraphDataService` 持有单例 `_embedding_provider`，注入到 `LightweightRAG` 和 `EntitySearchIndex`，避免重复加载模型。
- **版本号失效设计**：`is_stale(version)` 一行即可正确处理图更新，无需手动 invalidate。
- **端点辅助模式 (`_search_edges_by_endpoints`)**：多查询信号加权平均的设计简洁有效，src/dst/edge 三路分数统一处理。
- **文本构造优先级链**：`node_text` > 结构字段组合 > 名称，充分利用已有字段，不硬编码任何领域词。
- **RAG 维度不匹配检测 (`rag.py:63-70`)**：`retrieve` 中正确检测维度不一致并返回有意义的错误信息（虽然 `ingest_document` 中的处理仍需改进）。
- **`_jsonable_label`**：处理 numpy array label 的 JSON 序列化，防止 `ndarray` 直接出现在响应中。
- **测试中的 `KeywordProvider`**：4 维关键词向量作为测试 provider，既隔离了真实模型依赖，又能验证语义路径的逻辑正确性，是个好的测试设计。

---

## 改动优先级汇总

| 优先级 | 文件 | 问题 |
|---|---|---|
| P0 | `embedding.py:125` | `except Exception` → `except ImportError` + warning |
| P1 | `embedding.py:55` | `dim` 改为从模型实例读取 |
| P1 | `app.py` | `_get_search_index` 改为 `run_in_executor` |
| P1 | `rag.py:37-39` | 维度不匹配改为明确报错 |
| P2 | `tool_catalog.py` | `search_edge.required` 改为 `[]` |
| P2 | `search.py:_confidence_and_ambiguity` | 有 filter 时不标 ambiguous |
| P3 | `tests/` | 补充 RAG 测试、provider fallback 测试 |
