# search_node / search_edge 实体检索工具设计计划

> 制定日期：2026-04-28  
> 目标：为 iGraph 增加两个图内实体解析工具 `search_node` 与 `search_edge`，在用户没有给出精确节点名或边端点时，能够从自然语言描述中找到可用于后续图工具调用的精确实体。同时将现有 `rag.py` 升级为真正可用的语义检索，彻底替换 `_Hasher`。

---

## 一、背景与目标

当前图工具大多要求用户或 Agent 已经知道精确节点名、边端点，例如：

- `get_node_info(node_name)`
- `get_edge_info(src_name, dst_name)`
- `shortest_path(source, target)`
- `subgraph_extraction(node=...)`

但真实交互中，用户经常描述实体，而不是直接给出实体 ID：

- "那个提到 GraphSAGE 的论文节点"
- "度数最高的作者"
- "从 transformer 论文指向作者的那条引用边"
- "连接 A 类节点和 B 类节点中权重最大的边"

此外，现有的 `upload_document` / `retrieve_context` 工具虽然接口完整，但底层 `LightweightRAG` 使用 `_Hasher`（hash trick，无语义理解），导致文档检索效果差——用户上传文档后实际无法做有意义的语义搜索。

因此本计划的目标有两个：

1. **新增实体检索能力**：`search_node` / `search_edge` 将自然语言描述解析为精确实体。
2. **升级 RAG**：将 `rag.py` 中的 `_Hasher` 替换为真实嵌入模型，使 `retrieve_context` 工具真正可用。

这两个目标通过同一套 `EmbeddingProvider` 基础设施实现，一处配置，两处受益。

---

## 二、设计原则

### 2.1 避免启发式

不采用以下方案作为主要能力：

- 手写关键词规则，例如"包含 `度数高` 就给 degree 加分"。
- 手写领域词典，例如"作者、论文、引用"固定映射到某些字段。
- 用大量 if/else 判断用户 query 中的中文词或英文词来改变评分。
- `_Hasher` / hash trick 伪装为"语义检索"。

### 2.2 工具层职责

`search_node` / `search_edge` 只做三件事：

1. 执行结构化过滤与排序。
2. 基于实体文档做向量召回。
3. 对候选进行 rerank / 消歧，并返回可解释结果。

工具层不负责凭空理解"最重要""相关""相似"这类自然语言含义。

### 2.3 渐进可用

- 如果 embedding provider 可用：启用语义召回。
- 如果 reranker 可用：启用 rerank 和置信度判断。
- 如果 embedding 不可用：只执行 exact lookup + structured filters + structured sort，并明确返回 `semantic_enabled: false`。
- **不使用 `_Hasher` 作为"语义检索"**；如需 fallback，仅作为 lexical baseline，结果标记 `retrieval_mode: "lexical"`。

---

## 三、总体架构

```
User query
    |
    v
Agent / Query Parser
    |
    | 结构化检索请求
    v
search_node / search_edge
    |
    +-- Exact lookup
    |
    +-- Structured filters / sort
    |
    +-- Entity embedding retrieval  ──┐
    |                                 │  共享 EmbeddingProvider
    +-- LLM or model rerank           │
    |                                 │
    v                                 │
Candidate entities + best entity +   │
ambiguity flag                        │
    |                           LightweightRAG (retrieve_context)
    v                                 │
Downstream graph tools          ──────┘
```

### 3.1 文件层级

| 层 | 文件 | 职责 |
|---|---|---|
| 共享基础设施 | `graph_data_service/embedding.py` | `EmbeddingProvider` 抽象 + 各实现 |
| 文档检索（现有） | `graph_data_service/rag.py` | **改造**：`_Hasher` → `EmbeddingProvider` |
| 实体检索（新增） | `graph_data_service/search.py` | `EntitySearchIndex`，使用 `EmbeddingProvider` |
| 业务 facade | `graph_data_service/compute.py` | 暴露 `search_node()` / `search_edge()` |
| 工具注册 | `graph_data_service/tool_catalog.py` | 注册工具 schema |
| 分发路由 | `graph_data_service/app.py` | dispatch `search_node` / `search_edge` |

---

## 四、EmbeddingProvider（新文件 `embedding.py`）

这是整个方案的基础。`LightweightRAG` 和 `EntitySearchIndex` 都依赖它。

```python
class EmbeddingProvider(Protocol):
    name: str
    available: bool
    dim: int

    def encode(self, texts: list[str]) -> np.ndarray:
        """返回 L2 归一化的 float32 矩阵，shape (N, dim)。"""
        ...
```

### 4.1 具体实现

**`SentenceTransformerProvider`**（首选）

```python
class SentenceTransformerProvider:
    name = "sentence-transformers"
    dim = 384   # all-MiniLM-L6-v2

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name)
        self.available = True

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)
```

- 本地运行，无 API Key，确定性
- `all-MiniLM-L6-v2`：~80MB，首次加载约 2s，CPU batch encode ~10k 条/秒

**`OpenAIEmbeddingProvider`**（次选，需 `OPENAI_API_KEY`）

```python
class OpenAIEmbeddingProvider:
    name = "openai"
    dim = 1536   # text-embedding-3-small

    def encode(self, texts: list[str]) -> np.ndarray:
        # 同步调用，分批次，每批 ≤ 2048 tokens 约 96 条
        ...
```

**`LexicalProvider`**（`_Hasher` 的重命名，仅作 fallback）

```python
class LexicalProvider:
    name = "lexical"
    available = True
    dim = 384

    def encode(self, texts: list[str]) -> np.ndarray:
        # 原 _Hasher 逻辑，hash trick bag-of-words
        ...
```

- **只在两种真实 provider 均不可用时自动选用**
- 调用方须检查 `provider.name == "lexical"` 并在结果中标记 `retrieval_mode: "lexical"`

### 4.2 自动选择工厂

```python
def default_provider() -> EmbeddingProvider:
    try:
        return SentenceTransformerProvider()
    except ImportError:
        pass
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return OpenAIEmbeddingProvider(api_key)
    return LexicalProvider()   # 最后兜底
```

通过环境变量 `EMBEDDING_PROVIDER` 可强制指定（`sentence-transformers` / `openai` / `lexical`）。

---

## 五、rag.py 改造

当前 `LightweightRAG` 直接持有 `_Hasher` 实例，改造只需两步：

### 5.1 接受外部 provider

```python
class LightweightRAG:
    def __init__(self, session_dir: str, provider: EmbeddingProvider | None = None) -> None:
        self.session_dir = Path(session_dir)
        self.docs_dir = self.session_dir / "docs"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_path = self.session_dir / "doc_embeddings.npy"
        self.index_path = self.session_dir / "doc_index.json"
        self._provider = provider or default_provider()   # ← 替换 _Hasher
```

`ingest_document` 和 `retrieve` 内部将 `self._embedder.encode(...)` 改为 `self._provider.encode(...)`。

### 5.2 在 `GraphDataService` 中注入同一 provider

```python
class GraphDataService:
    def __init__(self) -> None:
        ...
        self._embedding_provider = default_provider()   # 全局单例
        self._rag_cache: Dict[str, LightweightRAG] = {}

    def _get_rag(self, session_id: str) -> LightweightRAG:
        if session_id not in self._rag_cache:
            session = self.get_or_create(session_id)
            self._rag_cache[session_id] = LightweightRAG(
                str(session._session_dir),
                provider=self._embedding_provider,   # ← 注入
            )
        return self._rag_cache[session_id]
```

改造完成后：
- `retrieve_context` 工具真正具备语义检索能力
- provider 类型在响应中透明暴露（`retrieval_mode: "sentence-transformers"` 等）

### 5.3 旧的 `_Hasher` 类

保留在 `rag.py` 中但重命名为 `_LexicalHasher`，由 `LexicalProvider` 内部使用。不再作为 `LightweightRAG` 的直接依赖。

---

## 六、实体文档设计

### 6.1 节点文档（`NodeSearchDocument`）

文本构造优先级（越靠前信息密度越高）：

```
if gd.node_text[i] exists:
    document = node_text[i]
elif has node_type or node_label:
    document = f"{name} type={node_type[i]} label={node_label[i]}"
else:
    document = name   # 至少有名字可搜索
```

完整文档结构：

```python
{
    "entity": "node",
    "node_id": 42,
    "node_name": "paper_123",
    "node_type": "paper",           # Optional
    "label": "gnn",                 # Optional
    "in_degree": 3,
    "out_degree": 9,
    "total_degree": 12,
    "neighbors_preview": ["author_9", "author_12"],   # top 5
    "document": "<embedding text>",   # 用于 embedding 的文本
}
```

### 6.2 边文档（`EdgeSearchDocument`）

文本构造优先级：

```
if gd.edge_text[i] exists:
    document = edge_text[i]
elif has edge_type or edge_name:
    document = f"{src} --[{edge_type}, {edge_name}, w={weight:.2f}]--> {dst}"
else:
    document = f"{src} -> {dst} w={weight:.2f}"
```

完整文档结构：

```python
{
    "entity": "edge",
    "edge_idx": 88,
    "edge_name": "e_88",           # Optional
    "src_name": "paper_123",
    "dst_name": "author_9",
    "edge_type": "cites",          # Optional
    "label": "citation",           # Optional
    "weight": 1.0,
    "src_node_summary": "paper_123 type=paper label=gnn",   # 端点摘要（限 100 字符）
    "dst_node_summary": "author_9 type=author",
    "document": "<embedding text>",
}
```

**端点摘要长度限制**：避免大图构建时边文档过大（E 条边 × 端点完整文本 ≈ 内存爆炸）。

---

## 七、EntitySearchIndex（`search.py`）

```python
class EntitySearchIndex:
    def __init__(
        self,
        gd: GraphData,
        graph_version: int,
        provider: EmbeddingProvider,
    ) -> None:
        self._provider = provider
        self._version = graph_version
        self._node_docs: list[dict] = []
        self._edge_docs: list[dict] = []
        self._node_matrix: np.ndarray   # (N, dim), L2 归一化
        self._edge_matrix: np.ndarray   # (E, dim), L2 归一化
        self._build(gd)

    def is_stale(self, version: int) -> bool:
        return version != self._version

    def search_nodes(self, req: NodeSearchRequest) -> dict:
        ...

    def search_edges(self, req: EdgeSearchRequest) -> dict:
        ...
```

### 7.1 `_build()` 流程

```
1. 遍历所有节点，构建 node_docs（含 document 字段）
2. provider.encode([doc["document"] for doc in node_docs]) → node_matrix
3. 遍历所有边，构建 edge_docs（含端点摘要，限长）
4. provider.encode([doc["document"] for doc in edge_docs]) → edge_matrix
```

### 7.2 `search_nodes()` 流程

```
validate request
    │
    ▼
exact lookup（gd.node2id.get(query) → 直接命中 → score=1.0）
    │
    ▼（未命中）
apply structured filters（node_type, label, min/max degree）
    │
    ▼
embedding retrieval if provider.name != "lexical"
    → q_vec = provider.encode([query])[0]
    → scores = node_matrix[filtered_ids] @ q_vec
    → top_k by score
    │ if provider.name == "lexical"
    → 仅按 filter 结果顺序返回，标记 retrieval_mode: "lexical"
    │
    ▼
apply structured sort（override score order if sort.field != "score"）
    │
    ▼
rerank top candidates（if rerank=True and LLM reranker available）
    │
    ▼
return NodeSearchResult
```

### 7.3 `search_edges()` 两种模式

**模式 A：直接边描述搜索**（提供 `query`）
- 对 edge_matrix 做向量召回，filter / sort / rerank。

**模式 B：端点辅助搜索**（同时提供 `src_query` 或 `dst_query`）
- 内部对 node_matrix 分别召回 src/dst 候选节点（各 top-20）。
- 枚举 src_candidates × dst_candidates 之间真实存在的边。
- 对这批真实边按 `query`（或端点分数乘积）排名。
- 若 src 或 dst 搜索本身 ambiguous，将歧义传递到返回结果。

---

## 八、工具接口设计

### 8.1 search_node

```json
{
  "name": "search_node",
  "description": "根据节点名称、别名、文本内容、属性约束或自然语言描述搜索图中的节点，返回可用于其他工具的精确 node_name。当用户没有给出精确名称时，先调用此工具再调用 get_node_info / shortest_path 等。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "节点名称、文本片段或自然语言描述。"
      },
      "top_k": {"type": "integer", "default": 8},
      "filters": {
        "type": "object",
        "properties": {
          "node_type":     {"type": "string"},
          "label":         {"type": "string"},
          "min_degree":    {"type": "integer"},
          "max_degree":    {"type": "integer"},
          "min_in_degree": {"type": "integer"},
          "max_in_degree": {"type": "integer"},
          "min_out_degree":{"type": "integer"},
          "max_out_degree":{"type": "integer"}
        }
      },
      "sort": {
        "type": "object",
        "properties": {
          "field": {"type": "string", "enum": ["score","node_id","in_degree","out_degree","total_degree"]},
          "order": {"type": "string", "enum": ["asc","desc"], "default": "desc"}
        }
      },
      "rerank": {"type": "boolean", "default": true}
    },
    "required": ["query"]
  },
  "category": "compute"
}
```

### 8.2 search_edge

```json
{
  "name": "search_edge",
  "description": "根据边名称、端点描述、边文本、边属性或自然语言描述搜索图中的边，返回可用于其他工具的精确 src_name / dst_name。支持通过 src_query / dst_query 同时约束端点。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query":     {"type": "string", "description": "边描述或关系描述。"},
      "src_query": {"type": "string", "description": "可选，起点节点的名称或描述。"},
      "dst_query": {"type": "string", "description": "可选，终点节点的名称或描述。"},
      "top_k": {"type": "integer", "default": 8},
      "filters": {
        "type": "object",
        "properties": {
          "edge_type": {"type": "string"},
          "label":     {"type": "string"},
          "min_weight":{"type": "number"},
          "max_weight":{"type": "number"}
        }
      },
      "sort": {
        "type": "object",
        "properties": {
          "field": {"type": "string", "enum": ["score","edge_idx","weight"]},
          "order": {"type": "string", "enum": ["asc","desc"], "default": "desc"}
        }
      },
      "rerank": {"type": "boolean", "default": true}
    },
    "required": ["query"]
  },
  "category": "compute"
}
```

---

## 九、Rerank 与消歧

### 9.1 EntityReranker

```python
class EntityReranker:
    async def rerank(
        self,
        query: str,
        candidates: list[dict],   # top 20 candidate summaries
        entity: str,              # "node" or "edge"
    ) -> RerankResult:
        ...

@dataclass
class RerankResult:
    best_index: int
    confidence: Literal["high", "medium", "low"]
    reason: str
    ambiguous: bool
```

第一版：空实现 `PassthroughReranker`，按 embedding score 原序返回，`confidence="medium"`，`ambiguous = (score[0] - score[1]) < 0.05`。

后续：接入 ProviderGateway，对 top-20 候选摘要发 LLM JSON 请求，解析上述结构。

### 9.2 歧义策略

返回 `ambiguous: true` 的情况：

- 第一名与第二名分数差 < 0.05（embedding）。
- reranker 置信度为 `low`。
- query 长度 < 3 字符且精确查找未命中。
- 多候选满足结构约束但文本证据不足。

`ambiguous: true` 时 Agent 应向用户确认候选，而不是直接调用下游精确工具。

---

## 十、返回格式

### 10.1 search_node 返回

```json
{
  "success": true,
  "query": "提到 GraphSAGE 的论文节点",
  "semantic_enabled": true,
  "retrieval_mode": "sentence-transformers",
  "rerank_enabled": true,
  "best": {
    "node_name": "paper_123",
    "node_id": 42,
    "score": 0.91,
    "confidence": "high"
  },
  "candidates": [
    {
      "node_name": "paper_123",
      "node_id": 42,
      "score": 0.91,
      "node_type": "paper",
      "label": "gnn",
      "in_degree": 3,
      "out_degree": 9,
      "total_degree": 12,
      "evidence": ["node text mentions GraphSAGE", "node_type matches filter"],
      "text_preview": "..."
    }
  ],
  "ambiguous": false
}
```

### 10.2 search_edge 返回

```json
{
  "success": true,
  "query": "从 GraphSAGE 论文指向作者的引用边",
  "semantic_enabled": true,
  "retrieval_mode": "sentence-transformers",
  "rerank_enabled": true,
  "best": {
    "src_name": "paper_123",
    "dst_name": "author_9",
    "edge_name": "e_88",
    "score": 0.88,
    "confidence": "medium"
  },
  "candidates": [
    {
      "src_name": "paper_123",
      "dst_name": "author_9",
      "edge_name": "e_88",
      "edge_type": "cites",
      "label": "citation",
      "weight": 1.0,
      "score": 0.88,
      "evidence": ["source node matches src_query", "edge_type matches filter"],
      "text_preview": "..."
    }
  ],
  "ambiguous": false
}
```

### 10.3 无语义时的返回

```json
{
  "semantic_enabled": false,
  "retrieval_mode": "lexical",
  "message": "No embedding provider available. Install sentence-transformers for semantic search.",
  ...
}
```

---

## 十一、索引缓存

在 `GraphDataService` 中：

```python
class GraphDataService:
    def __init__(self):
        ...
        self._embedding_provider = default_provider()   # 全局单例，复用于 RAG 和实体检索
        self._search_index_cache: Dict[str, EntitySearchIndex] = {}

    def _get_search_index(self, session_id: str) -> EntitySearchIndex:
        session = self.get_required(session_id)
        gd = session.get_graph()
        if gd is None:
            raise ValueError("尚未加载图数据。")
        existing = self._search_index_cache.get(session_id)
        if existing is None or existing.is_stale(session.graph_version):
            existing = EntitySearchIndex(gd, session.graph_version, self._embedding_provider)
            self._search_index_cache[session_id] = existing
        return existing
```

**失效时机**：`set_graph` / `load_graph_*` / `clear_graph` 均调用 `_bump_version()`，下次 `_get_search_index()` 自动重建。无需手动 invalidate。

---

## 十二、分发路由（`app.py`）

```python
# 在 _dispatch_graph_tool() 末尾 "未知工具" 之前添加：

if tool_name == "search_node":
    return self.do_search_node(session_id, arguments)

if tool_name == "search_edge":
    return self.do_search_edge(session_id, arguments)
```

```python
def do_search_node(self, session_id: str, arguments: dict) -> dict:
    index = self._get_search_index(session_id)
    req = NodeSearchRequest(
        query=str(arguments["query"]),
        top_k=int(arguments.get("top_k", 8)),
        filters=arguments.get("filters") or {},
        sort=arguments.get("sort"),
        rerank=bool(arguments.get("rerank", True)),
    )
    return index.search_nodes(req)

def do_search_edge(self, session_id: str, arguments: dict) -> dict:
    index = self._get_search_index(session_id)
    req = EdgeSearchRequest(
        query=str(arguments["query"]),
        src_query=arguments.get("src_query"),
        dst_query=arguments.get("dst_query"),
        top_k=int(arguments.get("top_k", 8)),
        filters=arguments.get("filters") or {},
        sort=arguments.get("sort"),
        rerank=bool(arguments.get("rerank", True)),
    )
    return index.search_edges(req)
```

---

## 十三、Agent 使用规范

工具说明需明确引导 LLM：

1. 当用户没有提供精确节点名，只提供描述、文本片段、类型、标签、结构约束时，先调用 `search_node`。
2. 当用户没有提供精确边端点，只描述关系、边文本、边类型或端点语义时，先调用 `search_edge`。
3. `search_*` 返回 `ambiguous: true` 时，不应继续调用 `get_node_info` / `shortest_path`，应先向用户确认候选。
4. `search_*` 返回高置信 best 后，再调用精确工具完成真正的图分析。

**典型调用链示例**：

> 用户：找一下那个提到 GraphSAGE 的论文节点到作者节点的最短路径。

```
1. search_node({"query": "提到 GraphSAGE 的论文节点", "filters": {"node_type": "paper"}})
2. search_node({"query": "作者节点", "filters": {"node_type": "author"}})
3. 两者均非 ambiguous → shortest_path({"source": "paper_123", "target": "author_9"})
```

---

## 十四、实施步骤

### Phase 1：EmbeddingProvider + RAG 升级（基础设施）

**目标**：建立共享 embedding 基础设施，`retrieve_context` 工具真正可用。

**改动**：

- **新增** `graph_data_service/embedding.py`
  - `EmbeddingProvider` Protocol
  - `SentenceTransformerProvider`
  - `OpenAIEmbeddingProvider`
  - `LexicalProvider`（原 `_Hasher` 重命名）
  - `default_provider()` 工厂

- **改造** `graph_data_service/rag.py`
  - 构造函数接受 `provider: EmbeddingProvider | None = None`
  - 内部 `_embedder` 改为 `_provider`
  - 原 `_Hasher` 类重命名为 `_LexicalHasher`，仅供 `LexicalProvider` 内部使用

- **改造** `graph_data_service/app.py`
  - `GraphDataService.__init__` 初始化 `self._embedding_provider = default_provider()`
  - `_get_rag()` 注入 provider

**验收**：

- `retrieve_context` 在有 `sentence-transformers` 时返回语义相关文档。
- `retrieve_context` 在无 embedding 时返回 `retrieval_mode: "lexical"` 并仍能工作。
- `LightweightRAG` 单元测试通过。

### Phase 2：实体检索工具骨架（结构化检索）

**目标**：不依赖 embedding 也能完成 exact + filter + sort。

**改动**：

- **新增** `graph_data_service/search.py`
  - `NodeSearchRequest` / `EdgeSearchRequest` dataclass
  - `NodeSearchDocument` / `EdgeSearchDocument` 构造
  - `EntitySearchIndex.__init__` + `_build()`（此阶段可先不建 embedding matrix）
  - `search_nodes()` exact lookup + filter + sort
  - `search_edges()` exact lookup + filter + sort（含端点辅助模式）

- **改造** `graph_data_service/tool_catalog.py`
  - 注册 `search_node` / `search_edge` schema

- **改造** `graph_data_service/app.py`
  - 添加 `_search_index_cache` + `_get_search_index()`
  - dispatch `search_node` / `search_edge`

**验收**：

- 精确节点名能命中，score=1.0。
- `node_type` / degree filter 生效。
- `sort.field = "total_degree"` 生效。
- 精确边端点能命中。
- `edge_type` / weight filter 生效。

### Phase 3：Embedding 召回

**目标**：支持描述式搜索。

**改动**：

- `EntitySearchIndex._build()` 中调用 `provider.encode()` 建立 `_node_matrix` / `_edge_matrix`
- `search_nodes()` / `search_edges()` 中加入向量召回逻辑
- `(session_id, graph_version)` 缓存失效正确工作

**验收**：

- 有 embedding provider 时，描述式 query 能召回文本相关实体。
- 无 embedding provider 时，不伪装语义，返回 `semantic_enabled: false`。
- 图版本更新后索引自动重建。

### Phase 4：Rerank 与歧义处理

**目标**：提高候选排序质量，避免强猜。

**改动**：

- `PassthroughReranker`（按分数差判断 ambiguous）
- 接入 ProviderGateway 的 `LLMReranker`（可选）
- 输出 `confidence` / `ambiguous` / `evidence`

**验收**：

- 分数差 < 0.05 时返回 `ambiguous: true`。
- 高置信结果可直接用于下游图工具。
- reranker 输出非法 JSON 时降级，标记 `rerank_error`。

### Phase 5：Agent 提示与测试补齐

**改动**：

- 更新 tool description（强调先搜索再精确调用）。
- 测试文件 `tests/graph_data/test_entity_search.py`。

---

## 十五、测试计划

文件：`tests/graph_data/test_entity_search.py`

| 用例 | 描述 |
|---|---|
| `test_search_node_exact_name` | 精确名称命中，score=1.0 |
| `test_search_node_filter_type` | `node_type` filter 生效 |
| `test_search_node_sort_total_degree` | sort by degree desc 正确 |
| `test_search_edge_exact_endpoints` | 精确端点命中 |
| `test_search_edge_filter_type_weight` | edge_type + weight filter |
| `test_search_edge_src_dst_queries` | 端点辅助搜索模式 |
| `test_search_without_embedding_marks_disabled` | 无 provider 时标记 semantic_enabled=false |
| `test_search_index_invalidates_on_version_change` | 版本变化后重建索引 |
| `test_ambiguous_results_low_confidence` | 接近分数返回 ambiguous=true |
| `test_rag_with_real_provider` | RAG retrieve_context 语义相关性 |
| `test_provider_fallback_order` | provider 自动选择顺序 |

---

## 十六、风险与取舍

| 风险 | 说明 | 缓解 |
|---|---|---|
| `sentence-transformers` 安装 | 首次 pip install 约 200MB | 放入 requirements.txt，首次加载 2s 可接受 |
| embedding provider 不可用 | 描述式搜索能力下降 | 明确返回 `semantic_enabled: false` |
| reranker 成本 | 每次搜索可能触发 LLM 调用 | 默认仅 top-20 rerank；`rerank=false` 可跳过 |
| 大图索引内存 | N×dim float32 矩阵 | 10万节点 × 384 dim ≈ 150MB，可接受；超大图可延迟构建 |
| 歧义结果被 Agent 误用 | 下游工具用错实体 | tool description 强制说明 `ambiguous=true` 时必须先确认 |
| OpenAI 调用延迟 | batch encode 首次慢 | 结果缓存于 `EntitySearchIndex`，不重复请求 |

---

## 十七、推荐落地顺序

1. Phase 1：`embedding.py` + `rag.py` 升级（让 `retrieve_context` 真正可用，独立收益最大）
2. Phase 2：`search.py` 骨架 + `tool_catalog.py` + dispatch（接口固化）
3. Phase 3：接入 embedding 矩阵（让 search_node/edge 有语义能力）
4. Phase 4：Rerank + ambiguous
5. Phase 5：测试 + prompt 优化

---

## 十八、最终形态

```
自然语言实体描述
    │
    ▼
search_node / search_edge   ←── EmbeddingProvider（同时服务 retrieve_context）
    │
    ▼
精确 node_name / src_name / dst_name
    │
    ▼
get_node_info / get_edge_info / shortest_path / subgraph_extraction / ...
```

改动后 iGraph 的完整实体解析路径形成闭环，且 `retrieve_context` 也从 hash trick 升级为真实语义检索。
