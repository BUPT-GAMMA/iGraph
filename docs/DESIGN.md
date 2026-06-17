# iGraph 系统设计文档

> 本文档详细描述 iGraph 系统的整体架构、各服务模块的设计与实现细节、数据流转、协议约定、以及关键设计决策的背景与取舍。

---

## 目录

1. [系统概述](#1-系统概述)
2. [整体架构](#2-整体架构)
3. [服务拓扑与端口分配](#3-服务拓扑与端口分配)
4. [GraphData Service（图数据服务）](#4-graphdata-service图数据服务)
5. [MCP Service（工具代理服务）](#5-mcp-service工具代理服务)
6. [GraphML Service（图机器学习服务）](#6-graphml-service图机器学习服务)
7. [Agent 核心（backend/agent）](#7-agent-核心backendagent)（含 Sub-Agents 包）
8. [API Gateway（API 网关）](#8-api-gateway-api-网关)
9. [前端（index.html）](#9-前端indexhtml)
10. [进程管理（ProcessSupervisor）](#10-进程管理processsupervisor)
11. [日志与会话持久化](#11-日志与会话持久化)
12. [会话生命周期](#12-会话生命周期)
13. [Memory、Skills 与 Tools Selector](#13-memory-skills-与-tools-selector)
14. [透明协议设计](#14-透明协议设计)
15. [GraphData 数据模型](#15-graphdata-数据模型)
16. [GraphData JSON 格式与序列化](#16-graphdata-json-格式与序列化)
17. [MCP 工具清单与调用数据流](#17-mcp-工具清单与调用数据流)
18. [图机器学习任务详解](#18-图机器学习任务详解)
19. [多 LLM 后端支持](#19-多-llm-后端支持)
20. [环境变量与配置](#20-环境变量与配置)
21. [部署模式](#21-部署模式)
22. [安全与边界处理](#22-安全与边界处理)
23. [关键设计决策与取舍](#23-关键设计决策与取舍)
24. [稳定性与可维护性设计](#24-稳定性与可维护性设计)
25. [已知边界与待完善项](#25-已知边界与待完善项)

---

## 1. 系统概述

iGraph 是一个基于 LLM + MCP（Model Context Protocol）的交互式图论分析系统。用户通过自然语言与系统对话，即可完成图数据加载、图算法分析、图机器学习训练与推理等任务。

### 核心能力

| 能力类别 | 具体功能 |
|---------|--------|
| 图数据管理 | 从文本边列表加载图、从上传文件加载图（支持 `.txt`/`.npz`/`.json`/`.csv`/`.xlsx`）、清空图、有向图转无向图、图快照持久化与 undo 回滚 |
| 数据集管理 | 数据集 CRUD + 版本管理、CSV/Excel 指定格式上传加载、文本三元组抽取建图 |
| 图结构查询 | 节点信息查询、边信息查询、边存在性检查、图全局信息 |
| 经典图算法 | 最短路径（含 Yen K-shortest）、全最短路径、最大流、最小割、最小生成树、社区检测（7 种算法）、节点中心性（10 种类型）、连通性分析、环检测、拓扑排序、度分布分析、子图提取（ego/k-core/k-truss/k-corona）、链路预测评分、路径分析、团分析、可达性分析、全局图指标 |
| 任务规划 | 查询复杂度自动判断、LLM 生成结构化分析计划、按步执行 + 结果汇总 |
| 轻量 RAG | 文档上传分块 + 向量检索（零外部数据库依赖） |
| 图机器学习 | 节点分类（GCN/GAT/SAGE）、链接预测（GCN/GAT/SAGE）、预训练（Node2Vec/对比学习/掩码自编码） |
| 多 LLM 后端 | Claude Opus 4-6 / Claude Sonnet 4-6 / GPT-5-4 / GPT-5-2 / Qwen3.6 Plus / Qwen3.7 Max |
| 交互模式 | Web UI（流式 SSE）、CLI（同步对话）、Claude Desktop（MCP stdio） |

### 技术栈

- **后端框架**: FastAPI + Uvicorn
- **LLM 接入**: OpenAI SDK（兼容 OpenAI / Anthropic / Qwen 的 OpenAI-compatible 端点）
- **MCP 框架**: FastMCP（`mcp` Python SDK）
- **图算法**: NetworkX
- **图神经网络**: PyTorch + PyTorch Geometric（PyG）
- **图数据容器**: NumPy（自研 `GraphData` 类）
- **前端**: 纯 HTML + Tailwind CSS + Vanilla JS + marked.js

---

## 2. 整体架构

系统由三个独立进程/服务组成，通过 HTTP 通信：

```
┌─────────────────────────────────────────────────────────────────────┐
│                        客户端层                                      │
│                                                                      │
│   Browser (Web UI)          CLI (main.py)       Claude Desktop       │
│        │                        │                     │              │
│    HTTP/SSE               同步调用               MCP stdio           │
│        │                        │                     │              │
└────────┼────────────────────────┼─────────────────────┼──────────────┘
         │                        │                     │
         ▼                        ▼                     │
┌──────────────────┐    ┌──────────────────┐            │
│  API Gateway     │    │   iGraph     │            │
│  (port 1109)     │    │   (进程内)        │            │
│                  │    │                  │            │
│  FastAPI 路由    │    │  chat() 同步     │            │
│  会话管理        │───▶│  chat_stream()   │            │
│  文件上传        │    │  异步流式        │            │
│  前端 HTML       │    │                  │            │
└──────────────────┘    └────────┬─────────┘            │
                                 │                      │
                    MCP JSON-RPC (streamable-http)       │
                                 │                      │
                                 ▼                      ▼
                        ┌──────────────────────────────────┐
                        │       MCP Service (port 1110)     │
                        │                                    │
                        │  FastMCP 工具注册（动态注册）        │
                        │  SessionRegistry 会话映射          │
                        │  GraphDataClient → GraphData HTTP  │
                        └──────────────────┬─────────────────┘
                                           │
                                    HTTP REST
                                           │
                                           ▼
                        ┌──────────────────────────────────┐
                        │   GraphData Service (port 1111)   │
                        │                                    │
                        │  GraphSession 管理                 │
                        │  GraphData 容器                    │
                        │  图加载 / 清空 / 转换               │
                        │  GraphCompute（图算法）             │
                        │  统一工具端点 /tools/{name}         │
                        │                                    │
                        │  ┌─────────────────────────────┐  │
                        │  │  GraphML 子模块（内置）        │  │
                        │  │  PyG 训练 / 推理              │  │
                        │  │  ArtifactStore                │  │
                        │  │  GCN / GAT / SAGE             │  │
                        │  └─────────────────────────────┘  │
                        └──────────────────────────────────┘
```

### 分层职责

| 层 | 组件 | 职责 |
|---|------|------|
| **客户端层** | Browser / CLI / Claude Desktop | 用户交互入口 |
| **网关层** | API Gateway (`backend/api_gateway`) | HTTP 路由、会话管理、文件上传、SSE 流式推送 |
| **Agent 层** | iGraph (`backend/agent`) | LLM 推理循环、协议解析、工具调用编排 |
| **工具层** | MCP Service (`mcp_service`) | MCP 工具注册与暴露、会话映射、工具调用转发 |
| **数据层** | GraphData Service (`graph_data_service`) | 图数据持有与管理、图算法计算（每 session 隔离）|
| **ML 子模块** | GraphML (`graph_data_service/graph_ml`) | 图神经网络训练与推理（内置于 GraphData） |

### 设计原则

1. **图状态宿主分离**: GraphData 只存储在 GraphData Service 进程中，Agent/MCP 均不持有图数据副本。图算法（含 ML）计算在 GraphData Service 内部完成，MCP 是纯代理。
2. **工具透明性**: LLM 通过文本标签协议调用工具（默认），也可通过 API tools 参数传入（native 模式）。
3. **会话隔离**: 每个用户会话拥有独立的 GraphSession、独立的对话历史、独立的 MCP 连接。
4. **服务可独立部署**: 三个服务可以在同一进程中托管，也可以分别部署到不同机器。

### 层间边界规则

| 层 | 只做这些 | 不做这些 |
|---|---------|---------|
| API Gateway | 会话/API 编排、文件上传、SSE 推送 | 图算法、LLM 直接调用 |
| iGraph | LLM 推理循环、virtual tool 截获、记忆更新调度 | 直接持有图数据、HTTP 路由 |
| MCP Service | 工具语义暴露（从 catalog 动态注册）、会话路由 | 图算法计算、图状态持久化 |
| GraphData | 图状态持有、图算法执行、GraphML 子模块 | LLM 调用、会话 HTTP API |

---

## 3. 服务拓扑与端口分配

| 服务 | 默认端口 | 环境变量 | 传输协议 |
|------|---------|---------|---------|
| API Gateway | 1109 | `API_SERVER_PORT` | HTTP (FastAPI) |
| MCP Service | 1110 | `MCP_PORT` | MCP streamable-http / stdio |
| GraphData Service | 1111 | `GRAPH_DATA_PORT` | HTTP (FastAPI) |

GraphML 是 GraphData 的内置子模块，无独立端口。

**调用方向**（严格单向）：

```
API Gateway → iGraph → MCP Service → GraphData Service（含 GraphML 子模块）
API Gateway → GraphData Service（仅文件上传注册）
```

---

## 4. GraphData Service（图数据服务）

### 4.1 定位

GraphData Service 是整个系统的**图状态持有者**。所有图数据都存储在此服务的内存中，按 `session_id` 隔离。其他服务（MCP / GraphML）通过 HTTP 接口读取或操作图数据。

### 4.2 目录结构

```
graph_data_service/
├── __init__.py            # 包入口
├── __main__.py            # python -m graph_data_service 入口
├── server.py              # Uvicorn 启动入口，创建全局 GraphDataService 实例
├── app.py                 # FastAPI app factory + GraphDataService 类 + 路由定义
├── tool_catalog.py        # 工具规格唯一真相源（GRAPH_TOOLS / GRAPH_ML_TOOLS）
├── algorithm_catalog.py   # 算法知识层（AlgorithmMeta + TASK_TYPE_MAP）
├── compute.py             # GraphCompute 图算法计算层（50+ 算法，含版本化 NetworkX 缓存；全局输出截断上限：列表 ≤50、路径 ≤20、社区 ≤20×10、分量 ≤10×20）
├── graph_data.py          # GraphData 核心数据容器（只读）
├── session.py             # GraphSession 单会话图状态管理（含快照 / undo / 数据集绑定）
├── codec.py               # GraphData ↔ JSON 标准化编解码
├── adapters.py            # GraphData → NetworkX 转换
├── io.py                  # GraphData .npz / .json 持久化
├── dataset_registry.py    # 数据集注册中心（CRUD + 版本管理）
├── rag.py                 # 轻量 RAG（文档分块 + numpy 向量检索）
├── job_manager.py         # 异步作业管理器（asyncio.Task 内存队列；支持 progress_callback 注入，作业状态通过 job.detail 传递进度）
├── data_pipeline/         # 数据建图管线
│   ├── graph_builder.py   # 边列表 / 三元组 → GraphData
│   └── text_extractor.py  # 文本三元组抽取（启发式 + 可选 LLM）
├── storage/               # 图后端抽象层（预留，默认内存态）
│   ├── base.py            # GraphStorageBackend ABC
│   ├── memory_backend.py  # 内存后端（当前默认）
│   └── neo4j_backend.py   # Neo4j 后端（骨架，可选）
└── graph_ml/              # GraphML 内置子模块
    ├── module.py          # GraphMLModule（服务入口，集成到 GraphDataService）
    ├── config_catalog.py  # 模型结构可视化辅助
    ├── schemas.py         # Pydantic 请求/响应 schema
    ├── artifact_store.py  # 模型产物存储（GRAPH_ML_ARTIFACT_ROOT）
    ├── models/            # GNN Backbone（GCN / GAT / SAGE）
    ├── tasks/             # 任务数据准备（节点分类 / 链接预测 / 预训练）
    ├── trainer/           # 训练器（trainer_node_classification 等）
    └── infer/             # 推理器（infer_node_classification 等）
```

### 4.3 GraphDataService 与 GraphCompute

```python
class GraphDataService:
    _sessions: Dict[str, GraphSession]       # session_id → GraphSession
    _compute: GraphCompute                   # 图算法计算层
    _graph_ml: GraphMLModule                 # GraphML 子模块
    _dataset_registry: DatasetRegistry       # 数据集注册中心
    _text_extractor: TextExtractor           # 文本三元组抽取器
    _job_manager: JobManager                 # 异步作业管理器
    _rag_cache: Dict[str, LightweightRAG]    # 会话 RAG 实例缓存

class GraphCompute:
    _cache: Dict[str, Tuple[int, nx.DiGraph]]
    #        session_id → (graph_version, nx_graph)
```

`GraphDataService` 在 `__init__` 中创建所有子组件实例，并自动从磁盘恢复可恢复的图快照（`_restore_sessions_from_disk`）。图数据变更（加载/清空/转换）时同步调用 `compute.invalidate(session_id)` 清除缓存。

**GraphCompute 输出规模约定**：所有返回节点/边列表的工具均遵循全局截断上限（`_MAX_LIST_ITEMS=50`、`_MAX_PATHS=20` 等），超出部分用 `_truncated: bool` + `_total: int` 字段标记，调用方可据此判断是否需要进一步查询。详见 `TOOL_SCALABILITY_AUDIT.md`。

核心方法：
- `get_or_create(session_id)` — 懒创建 GraphSession（含快照恢复尝试）
- `get_required(session_id)` — 获取 session，不存在时抛 404
- `delete_session(session_id)` — 删除 session 及其全部图数据 + 清理 RAG 缓存
- `dispatch_tool(session_id, tool_name, arguments)` — 统一异步工具分发入口

### 4.4 HTTP API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/sessions/{session_id}` | 创建会话 |
| DELETE | `/sessions/{session_id}` | 删除会话及图数据 |
| GET | `/health` | 健康检查，返回当前 session 数量 |
| POST | `/sessions/{sid}/uploads/register` | 注册上传文件映射（文件名→磁盘路径） |
| POST | `/sessions/{sid}/graph/load_text` | 从边列表文本加载图 |
| POST | `/sessions/{sid}/graph/load_upload` | 从已上传文件加载图 |
| POST | `/sessions/{sid}/graph/clear` | 清空当前图 |
| POST | `/sessions/{sid}/graph/to_undirected` | 将有向图转为无向图 |
| GET | `/sessions/{sid}/graph/info` | 查询图基本信息（节点数/边数/类型等） |
| GET | `/sessions/{sid}/graph/document` | 获取完整 GraphData 标准化文档（含全量数据） |
| POST | `/sessions/{sid}/uploads/ingest` | 直接上传文件到 GraphData（multipart） |
| **POST** | **`/sessions/{sid}/tools/{tool_name}`** | **统一工具端点：执行图算法或查询（由 MCP 层调用）** |

**统一工具端点**是本次重构的核心新增接口。请求体格式：
```json
{ "arguments": { "source": "A", "target": "F", "use_weight": true } }
```
GraphData Service 的 `execute_tool()` 函数通过 `tool_name` 分发到对应的图算法实现（`GraphCompute` 方法或图数据操作端点），直接在服务内部完成，无需再跨网络获取图文档。

### 4.5 GraphSession 类

```python
class GraphSession:
    session_id: str
    _graph: Optional[GraphData]             # 当前图（可为空）
    _graph_version: int                      # 单调递增版本号
    _uploads_by_name: Dict[str, UploadFileRecord]  # 文件名 → 磁盘路径映射
    _session_dir: Path                       # 会话存储目录（快照 / 数据集绑定）
    _snapshots_dir: Path                     # 历史快照目录（最多 5 个版本）
```

**图版本管理**: 每次 `set_graph()` 或 `clear_graph()` 操作都会递增 `_graph_version`，并自动写入快照（`save_snapshot()`）。MCP 层使用此版本号作为缓存 key，避免每次工具调用都重新传输整个图。

**图快照持久化**: 每次图变更后：
1. 当前图序列化为 `{session_dir}/graph_current.npz`
2. 同时保存一份带版本号的快照到 `{session_dir}/snapshots/v{NNN}.npz`
3. 快照数量超过 5 个时自动轮转删除最旧的
4. 服务启动时 `_restore_sessions_from_disk()` 扫描所有 session 目录，自动恢复

**Undo 回滚**: `undo()` 方法从快照目录加载倒数第二个版本恢复图，删除最新快照。

**数据集绑定**: `bind_dataset(dataset_id, version)` 将当前会话绑定到一个数据集版本，信息写入 `{session_dir}/dataset_binding.json`。

**文本加载流程** (`load_graph_from_text`):

1. 逐行解析 `src dst [weight]` 格式
2. 构建 `node2id` / `id2node` 双向映射
3. 构建 NumPy `edge_index` 和 `edge_weight`
4. 创建 `GraphData` 实例并 `set_graph()`
5. 支持 `append=True` 追加模式（保留已有节点和边）

**上传文件加载** (`load_graph_from_upload_path`):

1. 通过 `_resolve_existing_upload_path()` 在多个候选目录中查找文件
2. `.npz` / `.json` 文件使用 `GraphData.load()` 加载
3. 其他文件视为文本边列表

**上传路径解析**: `_resolve_existing_upload_path()` 会依次在以下位置搜索文件：
- 原始路径
- `os.path.realpath()` / `os.path.abspath()` 规范化路径
- 项目根目录下 `uploads/` 目录
- 环境变量 `API_UPLOAD_DIR` / `GRAPH_UPLOAD_DIR` / `GRAPH_DATA_UPLOAD_DIR` 指定的目录

### 4.6 codec.py — 标准化编解码

`graph_data_to_document(graph)` 将 GraphData 编码为可 JSON 传输的字典，包含：

```json
{
  "schema_version": "1.0",
  "num_nodes": 6,
  "directed": true,
  "name": "demo",
  "graph_desc": null,
  "id2node": {"0": "A", "1": "B", ...},
  "node2id": {"A": 0, "B": 1, ...},
  "edge_index": [[0, 0, 1, ...], [1, 2, 2, ...]],
  "edge_weight": [4.0, 2.0, 1.0, ...],
  "node_feat": null,
  "edge_feat": null,
  "node_label": null,
  "node_type": null,
  "edge_label": null,
  "edge_type": null,
  "graph_label": null
}
```

`graph_data_from_document(doc)` 执行反向解码。此编解码对用于 MCP Service 从 GraphData Service 获取图数据（通过 `/graph/document` 端点），以及 GraphML Service 获取图数据用于训练与推理。

---

## 5. MCP Service（工具代理服务）

### 5.1 定位

MCP Service 是**工具代理层**，基于 FastMCP 框架向 Agent / Claude Desktop 暴露图分析工具。它本身**不持有图数据也不执行计算**，所有工具调用均转发到后端服务：

1. **图工具**（加载/查询/算法等）：统一转发到 GraphData Service `/sessions/{sid}/tools/{tool_name}`
2. **图 ML 任务**（训练/推理）：同样转发到 GraphData Service（GraphML 是其内置子模块）

### 5.2 目录结构

```
mcp_service/
├── __init__.py
├── __main__.py           # python -m mcp_service [transport] 入口
├── server.py             # FastMCP 工具注册（动态注册，~210 行）
├── config.py             # MCPConfig 配置数据类
├── session_registry.py   # MCP session → agent_session_id 映射
├── graph_data_client.py  # GraphData Server HTTP 客户端（含 call_tool 方法）
└── raw_client.py         # RawMCPClient — Agent 端 MCP HTTP 客户端
```

> **注意**: `graph_ml_client.py` 已删除，GraphML 工具与图分析工具共用 `graph_data_client.py` 的 `call_tool` 路径。

### 5.3 SessionRegistry — 会话映射

MCP 协议中每个连接有一个内部 session key（`id(ctx.request_context.session)`），但 Agent 侧使用自己的 `agent_session_id`（8 位 UUID）。SessionRegistry 维护二者的映射：

```python
class SessionRegistry:
    _session_map: Dict[str, str]   # mcp_key → agent_session_id
```

映射建立流程：
1. Agent 首次连接 MCP 时调用 `register_session(agent_session_id)`
2. MCP Server 将 `_mcp_key(ctx)` → `agent_session_id` 写入 registry
3. 后续所有工具调用通过 `_sid(ctx)` 自动解析出 `agent_session_id`
4. Agent 断开时调用 `delete_graph_session()` 清理映射

**LLM 不感知 session_id**: 工具签名中没有 `session_id` 参数，会话路由完全透明。

### 5.4 工具转发实现

所有图工具通过统一的 `_invoke_graph_tool()` 辅助函数转发：

```python
async def _invoke_graph_tool(tool_name: str, ctx: Context, arguments: dict) -> str:
    sid = _sid(ctx)
    result = await _graph_data.call_tool(sid, tool_name, arguments)
    return _json(result)
```

调用链：`MCP 工具 → _invoke_graph_tool → GraphDataClient.call_tool → POST /sessions/{sid}/tools/{tool_name}`

这使得 MCP Server 的每个工具实现都极为简洁，仅负责：1) 从 `ctx` 解析 `session_id`，2) 将参数传递给 `_invoke_graph_tool`。

### 5.5 GraphDataClient

MCP Service 通过 `GraphDataClient` 统一转发所有工具调用（含 GraphML 工具）：

- `GraphDataClient`: 使用 `httpx.AsyncClient(trust_env=False)` 调用 GraphData Service（普通工具默认 30s 超时；train/infer 走长超时，默认 900s，可通过 `GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS` 调整）。核心方法：
  ```python
  async def call_tool(self, session_id, tool_name, arguments) -> dict:
      # POST /sessions/{session_id}/tools/{tool_name}
      # body: {"arguments": {...}}
  ```

GraphML 工具（`train`/`infer` 等）与图分析工具共用同一转发路径，GraphData Service 内部分发到 `GraphMLModule`。

`trust_env=False` 确保不受系统代理影响（macOS 上 `NO_PROXY` 问题）。

### 5.6 RawMCPClient — Agent 侧 MCP 客户端

Agent 通过 `RawMCPClient` 与 MCP Service 通信，使用 MCP streamable-http 传输：

```python
class RawMCPClient:
    mcp_url: str                  # e.g. "http://localhost:1110/mcp"
    _session_id: Optional[str]    # MCP session ID（来自响应头）
    _request_id: int              # 单调递增的 JSON-RPC request ID
    _client: Optional[httpx.AsyncClient]
```

**协议流程**:
1. `open()` → 创建 httpx 客户端 → `_initialize()` 发送 `initialize` RPC → 发送 `notifications/initialized` 通知
2. `list_tools()` → `tools/list` RPC → 返回工具定义列表
3. `call_tool(name, arguments)` → `tools/call` RPC → 返回工具执行结果
4. `close()` → 关闭 httpx 客户端

`call_tool()` 对 train/infer 使用长超时（默认 900s，可通过 `MCP_HEAVY_TOOL_TIMEOUT_SECONDS` 调整），避免 GraphML 长训练在 Agent→MCP 这一跳被 30s 默认超时截断。

**SSE 兼容**: MCP Server 有时以 `text/event-stream` 返回响应，`_parse_sse_json()` 从 SSE `data:` 行中提取最后一个有效 JSON payload。

**Session ID 管理**: 每次 HTTP 响应中的 `mcp-session-id` 头会被自动捕获并在后续请求中携带。

### 5.7 MCP 工具注册（动态注册）

**重构后**：`server.py` 从 ~730 行缩减至 ~210 行。工具不再硬编码，而是从 `graph_data_service/tool_catalog` 动态注册。

**动态注册机制**（`_build_dynamic_handler` + `_register_graph_tools`）：

```python
for spec in GRAPH_TOOLS:
    tool_name = spec["name"]
    input_schema = spec["inputSchema"]
    # 用 inspect.Signature 构建真实参数名的函数签名
    handler = _build_handler_with_signature(tool_name, input_schema, _invoke_graph_tool)
    mcp.tool(description=spec["description"])(handler)
```

关键点：

- **`inspect.Signature`**：FastMCP 通过 `inspect.signature()` 生成 inputSchema。使用 `**kwargs` 会生成 `{kwargs: dict}` 的错误 schema。因此必须为每个工具构建真实参数名（`filename: str`、`append: bool = False` 等）并设置 `handler.__signature__`。
- **工具规格来源**：`graph_data_service/tool_catalog.py`（`GRAPH_TOOLS`/`GRAPH_ML_TOOLS`），GraphData 同时暴露 `GET /tools` 端点。
- **隐藏工具**：`register_session` / `delete_graph_session` 硬编码保留，不走 catalog，Agent 在 `_visible_tool_specs` 中过滤。

**新增工具**：只需在 `tool_catalog.py` 中添加一条规格，MCP 注册、system prompt、native FC 参数全部自动生效。

**GraphML 工具条件注册**：与图工具相同机制，从 `GRAPH_ML_TOOLS` 动态注册，仅在 `_GRAPH_ML_ENABLED=True` 时执行。可用性判断：`MCP_ENABLE_GRAPH_ML_TOOLS` 环境变量 > GraphML `/health` 自动探测。

### 5.8 传输方式

MCP Service 支持两种传输方式：

| 传输 | 启动方式 | 适用场景 |
|------|---------|---------|
| `streamable-http` | `python -m mcp_service streamable-http` | Agent HTTP 调用（默认） |
| `stdio` | `python -m mcp_service` 或 `python -m mcp_service stdio` | Claude Desktop |

---

## 6. GraphML 子模块（内置于 GraphData）

### 6.1 定位

GraphML 现在是 `graph_data_service` 的**内置子模块**，与 `GraphCompute` 并列，不再作为独立 HTTP 服务部署。它直接读取 `GraphDataService` 会话内存中的 `GraphData` 与 `graph_version`，无需跨进程传输图文档。

GraphML 工具是**可选能力**——若 PyTorch/PyG 未安装，工具仍可注册，但调用 `train`/`infer` 时会返回安装提示错误，不影响其他工具正常使用。

### 6.2 目录结构

```
graph_data_service/graph_ml/
├── __init__.py
├── module.py            # GraphMLModule — 集成到 GraphDataService 的入口
├── config_catalog.py    # 模型结构可视化辅助
├── schemas.py           # Pydantic 请求/响应 schema
├── artifact_store.py    # 模型产物存储（GRAPH_ML_ARTIFACT_ROOT）
├── models/              # GNN Backbone 模型
│   ├── __init__.py      # 模型工厂 build_backbone()
│   ├── GCN.py           # GCNBackbone (GCNConv)
│   ├── GAT.py           # GATBackbone (GATConv)
│   └── SAGE.py          # SAGEBackbone (SAGEConv)
├── tasks/               # 任务定义层
│   ├── __init__.py
│   ├── node_classification.py
│   ├── link_prediction.py
│   └── pre_train.py
├── trainer/             # 训练器
│   ├── __init__.py      # GraphMLTrainer 编排入口
│   ├── trainer_node_classification.py
│   ├── trainer_link_prediction.py
│   └── trainer_pre_train.py
└── infer/               # 推理器
    ├── __init__.py      # GraphMLInferencer 编排入口
    ├── infer_node_classification.py
    ├── infer_link_prediction.py
    └── infer_pre_train.py
```

### 6.3 工具端点（统一通过 GraphData 调用）

所有 GraphML 工具通过 GraphData 统一工具端点暴露：

| MCP 工具名 | GraphData 路由 | 功能 |
|------------|---------------|------|
| `train` | `POST /sessions/{sid}/tools/train` | 训练 GNN 模型（带 `__async=true` 时提交异步作业，返回 `job_id`） |
| `infer` | `POST /sessions/{sid}/tools/infer` | 推理 |
| `visualize_model_architecture` | `POST /sessions/{sid}/tools/visualize_model_architecture` | 模型结构可视化 |

### 6.4 关键变化（相较旧版独立服务）

1. **无独立端口**：不再监听 `1112`，无 `GRAPH_ML_HOST`/`GRAPH_ML_PORT`/`GRAPH_ML_URL` 等变量。
2. **直接访问图数据**：训练/推理直接读取会话内存中的 `GraphData`，无需通过 `/graph/document` 拉取并反序列化。
3. **参数 schema 随工具暴露**：训练/推理参数规格直接写入 `train` / `infer` 工具 schema，由确认/修改流程复核后执行。
4. **产物路径**：模型存储由 `ArtifactStore` 管理，路径由 `GRAPH_ML_ARTIFACT_ROOT` 配置（默认 `artifacts/graph_ml`）。
5. **训练进度实时推送**：Agent 侧 `_execute_train_tool_with_progress` 以异步作业方式调用 `train`，每 0.5s 轮询 `get_job_status` 并将 `job.detail`（每 epoch 的 `loss`/`val_acc`/`progress` 等）转为 `tool_progress` SSE 事件实时推送到前端；前端渲染进度条（进行中蓝色 / 正常完成绿色 / 早停黄色）。

### 6.5 GNN Backbone 模型

三种 PyG Backbone 统一接口 `forward(x, edge_index) -> Tensor`：

| 模型 | 卷积层 | 特点 |
|------|--------|------|
| **GCNBackbone** | `GCNConv` | 经典 GCN，消息传递基于归一化邻接矩阵 |
| **GATBackbone** | `GATConv` | 图注意力网络，多头注意力机制（可配 `gat_heads`） |
| **SAGEBackbone** | `SAGEConv` | GraphSAGE，采样聚合邻居特征 |

**通用结构**: 每个 Backbone 都是多层堆叠（默认 2 层），中间层 ReLU 激活 + Dropout，最后一层无激活。

**模型工厂**: `build_backbone(model_name, in_channels, hidden_channels, out_channels, ...)` 根据名称创建对应 Backbone。

### 6.6 任务层

#### 节点分类 (NodeClassificationTask)

- **标签解析** (`parse_node_labels`): 支持整数标签、字符串标签（自动编码）、one-hot 标签（argmax）
- **数据划分** (`split_indices`): 按 `train/val/test` 比例随机划分有标签节点，确保每个分区至少有 1 个样本
- **推理节点** (`infer_node_ids`): 支持指定节点名列表或全部节点

#### 链接预测 (LinkPredictionTask)

- **正样本收集** (`collect_positive_edges`): 全部边或 `target_edges` 指定的子集
- **负样本采样** (`sample_negative_edges`): 随机采样不存在的边对，最多尝试 `num_samples * 30` 次
- **评估指标**: AUC（手动实现 Mann-Whitney U）、最佳阈值搜索（`best_threshold`）
- **数据划分**: 正样本按比例划分；验证集和测试集各自独立采样等量负样本

#### 预训练 (PreTrainTask)

- **方法分类**: `node2vec`（不需要 GNN 模型）；`contrastive_learning` / `mask_autoencoding`（需要 GNN 模型）
- **Node2Vec**: 基于随机游走生成节点嵌入
- **对比学习 / 掩码自编码**: 使用 GNN Backbone 进行自监督预训练

### 6.7 特征构建

`base_node_features(gd)` 生成节点特征张量：
- 如果 GraphData 已含 `node_feat`：直接使用（归一化为 float32）
- 否则自动生成 4 维特征：`[入度, 出度, 总度, 1.0]`，按列最大值归一化

`feature_source="embedding"` 时：从上游预训练模型的 `embedding.npy` 对齐到当前图节点（按名称匹配）。

### 6.8 GraphMLTrainer 编排

训练调用路径（`app.py` 分发逻辑）：

- 请求带 `__async=true`：通过 `JobManager.submit()` 提交异步作业，立即返回 `{job_id}`；训练在 `asyncio.to_thread` 中执行，`progress_callback` 由 `JobManager._make_progress_callback` 注入，每个 epoch 写入 `job.detail`。Agent 侧通过轮询 `get_job_status` 读取 `job.detail` 产出 `tool_progress` 事件。
- 请求不带 `__async`：走 `_dispatch_heavy_graph_ml_tool_once`（`asyncio.to_thread` 直接执行，阻塞直至完成）。

训练流程（`GraphMLTrainer.train()`）：
1. **参数校验** (`validate_train_request`)：检查 model / method 是否必填
2. **设置随机种子**: `random` / `numpy` / `torch` / `torch.cuda`
3. **加载图数据**: 直接从 `GraphDataService` 会话内存读取 `GraphData`（无 HTTP 跨进程）
4. **图版本校验**: 如果请求指定了 `graph_version` 则校验一致性
5. **特征构建**: `graph` 或 `embedding` 两种来源
6. **分发到具体 Trainer** + 注入 `progress_callback`: `NodeClassificationTrainer` / `LinkPredictionTrainer` / `PreTrainTrainer`
7. **训练循环**: 每个 epoch 末调用 `progress_callback({kind: "train_epoch", epoch, epochs, progress, loss, ...})`；早停时额外调用一次 `{kind: "train_early_stop", stopped_early: true, ...}`
8. **保存产物**: 模型权重 + meta.json + node_names.json + embedding.npy 等
9. **返回结果**: model_id, metrics, graph_version 等

### 6.9 GraphMLInferencer 编排

推理流程：
1. **解析模型**: 通过 `ArtifactStore.resolve_model()` 定位模型目录
2. **读取 meta.json**: 获取 task、graph_version、feature_source 等
3. **图版本校验**: 默认不允许版本不一致（除非 `allow_version_mismatch=true`）
4. **加载图 + 特征 + 模型权重**
5. **分发到具体 Inferencer**: 按 task 类型执行推理
6. **返回结果**: predictions / embeddings / scores

### 6.10 ArtifactStore — 模型产物存储

```python
class ArtifactStore:
    root_dir: str  # 默认 "artifacts/graph_ml"
```

**目录结构**:
```
artifacts/graph_ml/
└── {session_id}/
    └── mdl_{task}_{timestamp}_{hex}/
        ├── meta.json           # 模型元信息
        ├── model.pt            # PyTorch 模型权重
        ├── node_names.json     # 训练时节点名列表
        └── embedding.npy       # 节点嵌入（pretrain 产出）
```

**model_id 格式**: `mdl_{task}_{unix_timestamp}_{6_hex_chars}`，如 `mdl_node_classification_1711234567_a3f2c1`

**跨 session 查找**: `resolve_model()` 会在所有 session 目录中查找 model_id，支持上游 embedding 模型跨 session 复用。

---

## 7. Agent 核心（backend/agent）

### 7.1 目录结构

```
backend/agent/
├── __init__.py
├── igraph.py            # iGraph 核心类（含 Plan 执行器 + virtual tool 截获）
├── protocol.py               # parse_model_output / normalize_model_output 工具函数
├── model_registry.py         # 模型名称注册表（模型列表、名称归一化、provider 映射）
├── provider_gateway.py       # LLM Provider SDK 网关（OpenAI SDK 统一接口）
├── context/                  # 上下文管理包
│   ├── __init__.py           # 重新导出 ContextManager / ReasoningMode 等
│   ├── context_manager.py    # 上下文管理器（持有 ProtocolDriver，代理 build_model_messages / stream_turn）
│   ├── protocol_driver.py    # ProtocolDriver ABC + create_driver 工厂
│   ├── text_protocol_driver.py  # 文本协议驱动（StatefulStreamParser）
│   └── native_fc_driver.py   # 原生 Function Calling 驱动
├── sub_agents/               # LLM 子代理包（所有辅助 LLM 调用统一入口）
│   ├── __init__.py           # 重新导出所有子代理类型
│   ├── base.py               # SubAgent 基类（provider_gateway 注入 + invoke_text / stream_text）
│   ├── memory.py             # MemoryUpdateSubAgent（记忆写入决策）
│   ├── planner.py            # Planner + AnalysisPlan + PlanStep（结构化分析计划）
│   ├── query_analyzer.py     # QueryAnalyzer + ComplexityDecision（查询复杂度判断）
│   ├── skill_generator.py    # SkillGenerator（从对话生成 SKILL.md 候选）
│   └── tool_selector.py      # LLMToolSelector + ToolSelectionRequest/Result（工具选择）
├── memory/                   # 记忆包（详见 SKILLS_MEMORY.md）
│   ├── __init__.py           # 导出 MemoryEngine / MemoryManager / BuiltinMemoryProvider / MemoryScope / UserProfile
│   ├── memory_engine.py      # MemoryEngine：核心读写引擎（frozen snapshot + sub-agent 写入 + PlaybookReviewer 集成）
│   ├── manager.py            # MemoryManager：编排 builtin + 可选 extra provider
│   ├── provider.py           # MemoryProvider ABC（name / initialize / get_tool_schemas / handle_tool_call）
│   ├── builtin_provider.py   # BuiltinMemoryProvider（委托 MemoryEngine，实现 MemoryProvider）
│   ├── scope.py              # MemoryScope 数据类（user_id / workspace_id / project_id / …）
│   ├── paths.py              # MemoryPathResolver：按 scope 解析 ~/.igraph/memory/ 路径层次
│   ├── user_profile.py       # UserProfile 数据类 + load_from_items + to_snapshot_lines（优先级截断）
│   ├── tools.py              # memory_read / memory_delete virtual tool handler + VIRTUAL_MEMORY_TOOL_SPECS
│   ├── safety.py             # INJECTION_PATTERNS + scan_memory_content()
│   ├── store.py              # atomic_json_write（temp+fsync+os.replace）+ append_jsonl（flock）
│   └── service.py            # MemoryService：REST governance 层（供 /api/memory/* 端点调用）
│       # 持久化存储位置（由 MemoryPathResolver 解析）：
│       # ~/.igraph/memory/{workspace}/{user}/
│       #   ├── profile.json / playbooks.json / events.jsonl
│       #   ├── projects/{project}/memory.json
│       #   └── datasets/{dataset}/memory.json
├── skills/                   # 技能包（详见 SKILLS_MEMORY.md）
│   ├── __init__.py
│   ├── models.py             # SkillSpec / SkillCondition 数据类（frozen dataclasses）
│   ├── registry.py           # SkillRegistry：扫描 bundled/user/review_queue + frontmatter 解析 + lru_cache
│   ├── skill_loader.py       # build_skills_system_prompt()（compact index 构建 + 激活过滤）
│   ├── tools.py              # skills_list / skill_view / skill_manage virtual tool handler
│   ├── guard.py              # injection scan（复用 safety.py）+ supporting file 路径保护
│   ├── config_resolver.py    # ~/.igraph/config.yaml 解析 + ${key} 模板替换
│   ├── paths.py              # bundled_skills_dir / user_skills_dir / review_queue_dir / skill_events_path
│   ├── manager.py            # SkillManager：create / write_file / remove_file / approve / reject
│   ├── playbook_reviewer.py  # PlaybookReviewer：validated playbook → review_queue 候选 skill
│   ├── data_ingestion_table/SKILL.md      # 表格上传、输入格式确认、加载流程
│   ├── data_ingestion_text/SKILL.md       # 文本三元组抽取建图流程
│   ├── dataset_management/SKILL.md        # 数据集保存/加载/版本流程
│   ├── experiment_reporting/SKILL.md      # 分析结果报告流程
│   ├── graph_analysis_centrality/SKILL.md # 中心性分析流程
│   ├── graph_analysis_community/SKILL.md  # 社区检测流程
│   ├── graph_ml_inference/SKILL.md        # GraphML 推理流程
│   ├── graph_ml_training/SKILL.md         # GraphML 训练流程
│   ├── graph_path_connectivity/SKILL.md   # 路径与连通性分析流程
│   ├── graph_rag/SKILL.md                 # 文档检索与 RAG 流程
│   └── graph_state_check/SKILL.md         # 图加载状态检查流程
└── tools/                    # 工具常量包
    ├── __init__.py
    └── core_set.py           # CORE_TOOL_NAMES（5 项，永不进入 selector 裁剪）
```

### 7.2 Sub-Agents 包（sub_agents/）

**重构背景**：原本各辅助 LLM 调用（Planner、QueryAnalyzer、LLMToolSelector、SkillGenerator、记忆写入）分散在 `backend/agent/` 各处，通过回调函数（`invoke_llm: Callable`）接收 LLM 调用能力，耦合隐式、签名不统一。

重构后统一收拢到 `sub_agents/` 包，共同继承 `SubAgent` 基类，直接注入 `ProviderGateway` 实例。

```python
class SubAgent:
    name: str                          # 子代理名称（日志标识）
    system_prompt: str                 # 角色提示词（类属性，可覆盖）
    default_max_tokens: int | None     # 默认 token 限制（default: 512）
    default_temperature: float | None  # 默认温度（default: 0.0）

    def __init__(self, *, provider_gateway=None, prompt_invoker=None, prompt_streamer=None)

    async def invoke_text(self, user_prompt, *, step=None, max_tokens=None, temperature=None) -> str
    async def stream_text(self, user_prompt, ...) -> AsyncIterator[str]
    def build_messages(self, user_prompt) -> list[dict]   # [system, user] 消息列表
```

**子代理一览**：

| 子代理类 | 文件 | 职责 |
|---------|------|------|
| `MemoryUpdateSubAgent` | `memory.py` | 判断是否写入记忆 + 生成 upsert/delete/playbook 操作 |
| `Planner` | `planner.py` | 生成结构化 JSON 分析计划（AnalysisPlan + PlanStep） |
| `QueryAnalyzer` | `query_analyzer.py` | 判断查询复杂度（ComplexityDecision，决定是否进入 Plan 模式） |
| `SkillGenerator` | `skill_generator.py` | 从对话历史流式生成 SKILL.md 候选（供 PlaybookReviewer 使用） |
| `LLMToolSelector` | `tool_selector.py` | 每 turn 为主 LLM 选出最相关的 k 个工具（per-turn 缓存 + keyword fallback） |
| `ContextCompressor` | `context_compressor.py` | 将旧上下文压缩为短期记忆摘要（中文 Markdown，120K 字符输入上限，tool_calls 截断） |
| `PlanSolver` | `plan_solver.py` | 逐步执行结构化分析计划（每步隔离上下文，`[PLAN_STEP_RESULT]` 结果写回共享消息链） |
| `PlaybookDedup` | `playbook_dedup.py` | 检测语义重复 playbook，输出合并/归档方案（canonical + archive_ids），由 MemoryEngine 定期或在 playbook 写入后触发 |
| `TextGraphExtractor` | `text_graph_extractor.py` | 文本分块三元组抽取（LLM 辅助），支持长文本滑动窗口、逐块异步 LLM 提取、三元组去重与置信度过滤；供 `build_graph_from_text` 工具调用 |

**注入方式变更**（重构前后对比）：

```python
# 旧版：回调函数注入
QueryAnalyzer(invoke_llm=self._invoke_aux_llm)
LLMToolSelector(tool_specs, invoke_llm=self._invoke_aux_llm, ...)
Planner(selector, specs)  # stream_plan(llm=..., llm_stream=...)

# 新版：ProviderGateway 直接注入
QueryAnalyzer(provider_gateway=self._provider_gateway)
LLMToolSelector(tool_specs, provider_gateway=self._provider_gateway, ...)
Planner(selector, specs, provider_gateway=self._provider_gateway)
```

### 7.3 iGraph 类

```python
class IGraph:
    session_id: str
    model: str
    provider: str                        # "openai" / "anthropic" / "qwen"
    reasoning_mode: ReasoningMode        # "direct" / "staged"
    planning_mode: PlanningMode          # "never" / "auto" / "always"
    last_active: float                   # time.monotonic()，供 LRU 淘汰使用
    context_manager: ContextManager      # 内部持有 ProtocolDriver，对外代理 build_model_messages / stream_turn
    _provider_gateway: ProviderGateway
    _mcp_client: RawMCPClient
    _tool_specs: List[dict]              # 全部 MCP 工具定义
    _visible_tool_specs: List[dict]      # 可见工具（MCP 工具 + virtual tools；排除 register_session 等）
    _mcp_visible_tool_specs: List[dict]  # MCP 侧可见工具（不含 virtual tools）
    _tool_map: Dict[str, dict]           # name → spec（仅 MCP 工具，不含 virtual）
    _tool_selector: Optional[LLMToolSelector]  # LLM 工具选择器（per-turn 缓存，无 embedding）
    _expanded_tool_names: set[str]       # 主 agent 通过 tool_expand 手动加入的工具名
    _query_analyzer: QueryAnalyzer       # 查询复杂度判断器
    _planner: Optional[Planner]          # 轻量任务规划器（依赖 LLMToolSelector）
    _skill_registry: SkillRegistry       # 技能注册表（lazy discover）
    _has_graph: bool                     # 本地图状态缓存
    _graph_node_count: int               # 当前图节点数
    _graph_edge_count: int               # 当前图边数
    _graph_is_directed: bool             # 当前图是否有向
    _graph_has_weights: bool             # 当前图是否带权
    _memory_manager: Optional[MemoryManager]    # 记忆管理器（可选，由 ApiGatewayServer 注入）
    _memory_prompt_block: str            # 会话开始时 load_and_freeze() 返回的冻结快照（整 session 不变）
    _memory_update_chain: Optional[asyncio.Task]  # 串行异步记忆更新链
    _context_token_limit: int            # token 上限（默认 192000，SESSION_CONTEXT_TOKEN_LIMIT 覆盖）
    _context_compressor: ContextCompressor  # 上下文压缩子代理
    _context_compression_count: int      # 当前 session 压缩次数（随 context_usage 上报）
    _plan_solver: PlanSolver             # 分析计划求解器（__init__ 时注入，不随 Agent 重建）
    _paused_plan_state: Optional[dict]   # 暂停等待用户确认时的计划快照（含 step_messages）
```

**重构变化**：

- **sub_agents 包统一收口**：`Planner`、`QueryAnalyzer`、`LLMToolSelector`、`MemoryUpdateSubAgent`、`SkillGenerator` 全部迁移到 `sub_agents/`，通过 `SubAgent` 基类共享 `invoke_text`/`stream_text`，`ProviderGateway` 直接注入（取代原来的 `invoke_llm: Callable` 回调）。
- **`embedding.py` 已删除**：`ToolSelector`（sentence-transformers）替换为 `LLMToolSelector`（LLM sub-agent，零向量库依赖）。
- **`_memory_engine` → `_memory_manager`**：`ApiGatewayServer` 创建 `MemoryManager(BuiltinMemoryProvider(engine))`，iGraph 只依赖 `MemoryManager` 接口。
- **新增 virtual tools 截获**：`_handle_virtual_tool()` 在 MCP 分发前处理 `skills_list` / `skill_view` / `skill_manage` / `memory_read` / `memory_delete` / `tool_expand`。
- **新增 `_expanded_tool_names`**：`tool_expand` virtual tool 将目标工具名加入此集合，使 selector 在后续步骤中包含它。
- **新增 per-turn selector 重置**：每次 `_chat_stream_impl` 入口调用 `_tool_selector.set_turn_id(uuid)` 清空 per-turn 缓存。
- 删除 `_sync_loop` / `_sync_thread`：不再维护后台事件循环，直接运行于 FastAPI 的 uvicorn 事件循环。
- `planning_mode` 可通过 `set_planning_mode()` 动态切换；`last_active` 每次 `chat_stream` 入口更新供 LRU 淘汰。

**构造参数**：

- `restored_messages`: 从 `SessionStore` 读取的历史上下文消息（跨进程会话恢复）
- `restored_has_graph`: 历史会话是否有图（始终 `False`，图数据无法跨进程恢复）
- `memory_manager`: `MemoryManager` 实例，由 `ApiGatewayServer` 全局创建并注入（可选，默认 `None`）

### 7.4 MCP 连接管理

**首次连接** (`_ensure_connected`):

1. 调用 `_mcp_client.open()` 建立连接（NO_PROXY 在启动入口处已集中设置）
2. 调用 `_mcp_client.list_tools()` 获取工具列表
3. 过滤掉 `_HIDDEN_TOOLS`（`register_session`、`delete_graph_session`）
4. 调用 `register_session` 在 MCP/GraphData 链路中注册当前 session

**断线重连** (`_reconnect_mcp`)：工具调用失败且异常类型为 `ClosedResourceError` 时自动重连并重试一次。

**事件循环绑定**：`httpx.AsyncClient`（`RawMCPClient` 内部）绑定到创建时的事件循环。若检测到 loop 切换（`self._mcp_loop is not current_loop`），自动关闭旧连接并在新 loop 上重建。

### 7.5 事件循环架构（重构后简化）

**旧架构**：iGraph 维护独立后台 loop（`_sync_loop` + daemon 线程），通过 `queue.Queue` 与 FastAPI 的请求 loop 桥接。

**新架构**：不再维护后台 loop。`chat_stream` 直接在 FastAPI/uvicorn 的事件循环中运行，无线程切换开销。

```python
async def chat_stream(self, user_input: str):
    self.last_active = time.monotonic()
    async for event in self._chat_stream_impl(user_input):
        yield event

def chat(self, user_input: str) -> str:
    # 仅用于 CLI，独立创建 loop
    return asyncio.run(self._collect_chat(user_input))
```

Uvicorn 使用单一事件循环，Agent 实例与 session 1:1 绑定，不会跨 loop 复用。

### 7.6 对话循环 (`_chat_stream_impl`) — Plan-aware

这是 Agent 的核心推理入口。首先通过 `QueryAnalyzer` 判断是否需要显式规划，然后走 Plan 模式或 Direct（ReAct）模式：

```
用户输入
    │
    ▼
追加 user message（纯用户文本，不含系统指令）
    │
    ▼
_tool_selector.set_turn_id(uuid)   ← 重置 per-turn 工具选择缓存
    │
    ▼
planning_mode 判断：
    ├─ "always" → 强制进入 Plan 模式（跳过 QueryAnalyzer）
    ├─ "never"  → 强制跳过 Plan 模式（直接走 ReAct）
    └─ "auto"   → QueryAnalyzer.should_plan(message, graph_context, tool_names)
                    辅助 LLM 判断是否需要多轮连续工具调用（temperature=0, max_tokens=512）
                    保守策略：estimated_tool_calls ≥ 3 才进入 Plan 模式
                    任何异常 → 降级为 Direct 模式
    │
    ├─→ (False) ─→ _run_react_loop(msgs, max_steps=16)
    │                  │
    │                  │  ┌──── ReAct 循环（最多 16 轮）──────────────────────────┐
    │                  │  │                                                         │
    │                  │  │  1. _select_tools_for_turn(user_input)                  │
    │                  │  │     LLMToolSelector.select(req, k=8)                    │
    │                  │  │     → 入选工具 ∪ CORE_TOOL_NAMES ∪ _expanded_tool_names │
    │                  │  │     （per-turn 缓存，同一 turn 内多步复用）               │
    │                  │  │                                                         │
    │                  │  │  2. context_manager.build_model_messages()               │
    │                  │  │     ├─ [1] role system prompt（整 session 不变）          │
    │                  │  │     ├─ [2] reasoning mode rule（system msg 动态注入）     │
    │                  │  │     ├─ [3] memory frozen snapshot（整 session 不变）      │
    │                  │  │     ├─ [4] selected tools schema（text 模式写 system）    │
    │                  │  │     ├─ [5] skills compact index（≤ 2000 字符）            │
    │                  │  │     └─ [6] 历史消息                                      │
    │                  │  │                                                         │
    │                  │  │  3. context_manager.stream_turn() → 事件 yield           │
    │                  │  │     （协议解析失败时最多重试 _MAX_PROTOCOL_RETRIES 次，     │
    │                  │  │      注入纠正提示后重新生成）                               │
    │                  │  │                                                         │
    │                  │  │  4. 若 LLM 调用 virtual tool：                           │
    │                  │  │     _handle_virtual_tool(name, args)                    │
    │                  │  │     ├─ skills_list / skill_view / skill_manage → handle_skill_tool_call│
    │                  │  │     ├─ memory_read / memory_delete → handle_memory_tool_call│
    │                  │  │     └─ tool_expand(name) → _expanded_tool_names.add(name)│
    │                  │  │                                                         │
    │                  │  │  5. tool_calls → _run_tool(tc) → continue               │
    │                  │  │     answer → break                                      │
    │                  │  └─────────────────────────────────────────────────────────┘
    │                  │
    └─→ (True) ──→ Planner.build_plan(query, graph_info, llm)
                     │  调用一次辅助 LLM，生成结构化 JSON 计划
                     │  校验 candidate_tools 白名单
                     │  失败时 fallback 到 LLMToolSelector.select_sync()
                     │
                     ▼
                  _plan_solver.run(plan, msgs, persist_plan=...)
                     │
                     ├─ for plan_step in plan.steps:
                     │     ├─ 依赖检查（前置步骤未完成 → skip）
                     │     ├─ yield solve_step_start
                     │     ├─ build_step_messages()：
                     │     │     [PLAN_SOLVER_CONTEXT 规划总览 + 图状态]
                     │     │     + [prior [PLAN_STEP_RESULT] 消息]
                     │     │     + [当前步骤 prompt]
                     │     ├─ for turn in range(max_turns_per_step=4):
                     │     │     build_model_messages(working_messages=step_messages)
                     │     │     → react loop（各步骤上下文完全隔离）
                     │     │     step 中间消息 → log(include_in_context=False)
                     │     │     全部工具失败 → 标记 failed
                     │     ├─ build_step_result_message() → [PLAN_STEP_RESULT] → 写入 msgs
                     │     ├─ yield solve_step_done
                     │     └─ persist_plan()
                     │
                     └─ yield {"type": "done", ...}
    │
    ▼
  _schedule_memory_update()
    │  异步触发（非阻塞，串行链 _memory_update_chain）
    │
    ▼
  MemoryManager.sync_turn(session_id, user_input, assistant_output, ...)
    │  → MemoryEngine.maybe_update_from_turn()
    │    ├─ 子代理 LLM 评估 → should_write + operations
    │    ├─ _apply_operations()
    │    │     category=="profile" → user_profile.json
    │    │     其他 → long_term_memory.json
    │    └─ _apply_playbook_operations() → playbooks.json
    │
    ▼
  yield {"type": "done", "has_graph": bool}
```

**关键设计变化**：

- **工具选择层**：`ToolSelector`（embedding.py，语义向量）→ `LLMToolSelector`（LLM sub-agent，无 embedding）；per-turn 缓存通过 `set_turn_id()` 管理；fallback 走关键词匹配而非全量工具。
- **Memory 注入**：动态 Top-K recall → 会话开始时 `load_and_freeze()` 一次性冻结快照；中途写入不刷新 system prompt，保护 prefix cache。
- **Virtual tools**：`skills_list` / `skill_view` / `skill_manage` / `memory_read` / `memory_delete` / `tool_expand` 在 `_handle_virtual_tool` 拦截，不走 MCP。
- **Profile 路由**：memory sub-agent 写入 `category="profile"` 的 op 路由到 `user_profile.json`；其他 category 进 `long_term_memory.json`。
- **Plan 模式求解器独立**：`_run_plan_solver` 内联方法提取为独立 `PlanSolver` 类（`sub_agents/plan_solver.py`），iGraph 构造时注入。每个 plan step 拥有**隔离上下文**（`step_messages`），仅包含规划总览、前序步骤的 `[PLAN_STEP_RESULT]` 摘要和当前步骤 prompt，不携带前步的中间工具调用细节；步骤间结果通过 `[PLAN_STEP_RESULT]` 消息串联写回共享消息链。
- **短期记忆压缩**：每轮 user turn 开始后评估 token 使用量，超过 `CONTEXT_COMPRESSION_TRIGGER_PCT`（默认 90%）× `token_limit` 时调用 `ContextCompressor` sub-agent 压缩旧上下文；压缩摘要以 `role=user` 注入，保护 API 消息顺序约束；失败时 fallback 到文本拼接摘要。
- 协议重试、推理模式 system msg 注入、图元数据本地缓存均同前版。

### 7.7 工具执行 (`_run_tool`)

1. 解析 `name`、`args`、`call_id`
2. 最多尝试 2 次（首次失败且为 `ClosedResourceError` 时自动重连）
3. **`train` 工具走专用进度路径**（`_execute_train_tool_with_progress`）：
   - 注入 `__async=True` 提交异步作业，拿到 `job_id`
   - 轮询 `get_job_status`（每 0.5s），读取 `job.detail`（最新 epoch 进度）
   - 签名去重：detail 不变时不重复 emit progress 事件
   - 作业完成时，若 `job.detail.kind` 尚未是终态（`train_done`/`train_early_stop`），补发一条终态 progress 事件
   - 返回 `job.result` 作为工具结果（交给 `_format_mcp_tool_result` 序列化）
4. 其他工具：直接调用 `_mcp_client.call_tool(name, args)`
5. 格式化结果：提取 MCP 响应中 `content` 列表的 `text` 块；plain dict 走 `json.dumps` fallback
6. 更新本地图状态缓存：`load_graph` / `load_graph_from_upload` / `load_dataset` / `build_graph_from_text` / `undo_graph` / `to_undirected` 成功 → `_has_graph = True` + 更新 `_graph_node_count` / `_graph_edge_count` / `_graph_is_directed` / `_graph_has_weights`；`clear_graph` → 全部重置为零
7. 调用 `logger.set_graph_meta()` 将图元数据写入 session_meta.json

### 7.8 模型切换 (`set_model`)

动态切换模型时：
1. 解析新模型名 → provider / api_key / base_url
2. 更新 `self.provider` / `self.model`
3. 调用 `_provider_gateway.set_model_endpoint()` 重建 OpenAI 客户端
4. **保留当前会话历史与图状态**（不重建 ContextManager 和 MCP 连接）

### 7.9 规划模式切换 (`set_planning_mode`)

`planning_mode` 控制是否使用显式规划流程：

- `"never"`：始终走 Direct ReAct 循环，不调用 QueryAnalyzer / Planner。
- `"auto"`（默认）：由 `QueryAnalyzer.should_plan()` 判断（estimated_tool_calls ≥ 3 时进入 Plan 模式）。
- `"always"`：始终进入 Plan 模式，无论 QueryAnalyzer 判断结果如何。

调用 `PATCH /api/sessions/{sid}/planning-mode {"planning_mode": "never"}` 动态切换，无需重建 Agent。

### 7.10 推理模式切换 (`set_reasoning_mode`)

两种模式下 `<think>` 都是必须输出的，区别在于内容深度：

- `"staged"`（推理模式）：`<think>` 要充分展开，规划求解路径或反思工具结果是否充分
- `"direct"`（直接模式）：`<think>` 尽量简短（1-2 句），或输出空 `<think></think>`

**模式提示注入方式（重构后）**：

- 旧版：`[回答模式] ...` 追加到每条用户消息末尾，污染对话历史。
- 新版：`build_mode_notice(reasoning_mode)` 返回的提示文本作为 system message 动态注入到每轮 `build_model_messages()` 的前缀中，用户消息保持干净。

---

## 8. API Gateway（API 网关）

### 8.1 定位

API Gateway 是 Web UI 的后端，聚合了前端页面、会话管理、文件上传和流式聊天接口。

### 8.2 目录结构

```
backend/api_gateway/
├── __init__.py
├── __main__.py     # 启动入口 + 环境变量解析
└── app.py          # ApiGatewayServer 类 + 路由 + 工厂函数
```

### 8.3 ApiGatewayServer 类

```python
class ApiGatewayServer:
    config: ApiGatewayConfig
    _proc_supervisor: Optional[ProcessSupervisor]   # 子进程托管（可选）
    logger: Logger
    _agent_factory: AgentFactory                     # iGraph 工厂
    _agents: Dict[str, IGraph]                   # session_id → 活跃 Agent
    _uploaded_files: Dict[str, Dict[str, dict]]      # session_id → {file_token → info}
    _memory_manager: Optional[MemoryManager]         # 全局共享记忆管理器（MemoryManager(BuiltinMemoryProvider(engine))）
    app: FastAPI
```

### 8.4 HTTP API 端点

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 返回 `index.html` 前端页面 |
| GET | `/api/sessions` | 列出全部历史会话（元数据列表） |
| POST | `/api/sessions` | 创建新会话（指定 model + reasoning_mode） |
| POST | `/api/sessions/{sid}/activate` | 激活/恢复历史会话（含对话上下文） |
| GET | `/api/sessions/{sid}/messages` | 获取可见聊天消息列表 |
| DELETE | `/api/sessions/{sid}` | 删除会话（清理 Agent + 图数据 + 上传文件记录） |
| GET | `/api/sessions/{sid}/info` | 查询会话信息（model/has_graph/reasoning_mode） |
| PATCH | `/api/sessions/{sid}/model` | 切换模型 |
| PATCH | `/api/sessions/{sid}/reasoning-mode` | 切换推理模式 |
| PATCH | `/api/sessions/{sid}/planning-mode` | 切换规划模式（`never` / `auto` / `always`） |
| POST | `/api/sessions/{sid}/chat` | 同步聊天（返回完整回复） |
| POST | `/api/sessions/{sid}/chat/stream` | **流式聊天**（SSE 推送） |
| POST | `/api/sessions/{sid}/upload` | 上传图文件 |
| GET | `/api/datasets` | 列出数据集（代理到 GraphData `/datasets`） |
| POST | `/api/sessions/{sid}/load-dataset` | 从数据集注册表加载图 |
| POST | `/api/sessions/{sid}/save-dataset` | 将当前图保存到数据集注册表 |
| GET | `/api/export/sft` | SFT 数据导出（`format=alpaca\|sharegpt\|raw`） |
| GET | `/api/memory` | 查询记忆条目（`scope` / `query` / `limit`） |
| POST | `/api/memory` | 手动写入记忆条目 |
| PATCH | `/api/memory/{memory_id}` | 更新指定条目 |
| DELETE | `/api/memory/{memory_id}` | 软删除（status → archived） |
| GET | `/api/memory/events` | 查看 Memory 审计日志 |
| GET | `/api/memory/usage` | 快照使用情况（字符数 / frozen 状态） |

### 8.5 流式聊天实现

```python
@app.post("/api/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, req: ChatReq):
    async def event_generator():
        async for evt in agent.chat_stream(message):
            yield f"data: {json.dumps(evt)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

事件类型（前端消费）：

| 事件 type | 数据 | 用途 |
|-----------|------|------|
| `thinking_start` | `{phase}` | 思考块开始（仅 staged 模式） |
| `thinking` | `{phase, content}` | 思考内容增量 |
| `thinking_end` | `{phase}` | 思考块结束 |
| `token` | `{content}` | 最终回答增量文本 |
| `tool_call` | `{tool, args, call_id}` | 工具调用开始 |
| `tool_result` | `{tool, call_id}` | 工具调用完成 |
| `tool_progress` | `{tool, call_id, progress, step_id?}` | 长时工具（train）逐 epoch 进度更新；`progress` 含 `kind`/`epoch`/`epochs`/`loss`/`val_acc` 等字段 |
| `feedback_resolved` | `{request}` | 用户确认/拒绝高风险工具后立即 emit，`request` 含 `status`/`patched_args`/`comment` |
| `plan_ready` | `{plan_id, steps}` | 分析计划生成完毕（Plan 模式） |
| `step_start` | `{step_id, goal}` | 计划步骤开始执行 |
| `step_done` | `{step_id, status, summary}` | 计划步骤执行完毕 |
| `plan_complete` | `{plan_id, answer}` | 全部步骤完成 |
| `context_usage` | `{current_tokens, token_limit, percent, status, excludes, compression_count}` | token 使用量（每轮 turn 开始时 + 压缩后 emit） |
| `context_compressing` | `{usage, threshold}` | 即将压缩旧上下文（压缩开始前预告） |
| `context_compressed` | `{before, after, stats}` | 上下文压缩完成，`stats` 含 `compression_count`/`preserved_recent_user_turns` |
| `await_user_feedback` | `{request}` | 高风险工具调用（train/infer/clear_graph）等待用户确认 |
| `done` | `{has_graph, context_usage}` | 对话完成（`context_usage` 为本轮最终 token 状态） |
| `error` | `{error}` | 错误 |

### 8.6 文件上传流程

1. 前端 `POST /api/sessions/{sid}/upload` 上传文件
2. API Gateway 保存到 `uploads/` 目录，文件名格式 `{sid}_{8hex}_{sanitized_name}`
3. 生成 `file_token`（UUID hex）
4. 调用 GraphData Service `/sessions/{sid}/uploads/register` 注册文件映射
5. 返回 `file_token` 和 `filename` 给前端
6. 前端自动发送聊天消息并附带 `attachments: [{file_token, filename}]`
7. `_resolve_attachments()` 将附件信息转化为 `[附件文件]` 块追加到消息中
8. LLM 收到消息后调用 `load_graph_from_upload(filename=...)` 工具

---

## 9. 前端（index.html）

### 9.1 技术栈

- **布局框架**: Tailwind CSS（CDN）
- **Markdown 渲染**: marked.js v13（CDN）
- **框架**: 无框架，纯 Vanilla JS + 手动 DOM 操作

### 9.2 状态管理

全局 `state` 对象：

```javascript
const state = {
    sessionId: null,         // 当前会话 ID
    messages: [],            // 消息历史 [{role, content, tools[], thinking[]}]
    loading: false,          // 是否正在请求中
    creating: false,         // 是否正在创建会话
    uploading: false,        // 是否正在上传文件
    switchingModel: false,   // 是否正在切换模型
    switchingReasoning: false, // 是否正在切换推理模式
    hasGraph: false,         // 当前会话是否已加载图
    error: null,             // 错误信息
    model: 'gpt-5-2',       // 当前模型
    reasoningMode: 'direct', // 推理模式
    currentPlan: null,       // 当前分析计划（Plan 模式）
    // 流式临时状态
    streamBuf: '',           // 回答文本累积
    streamTools: [],         // 工具调用状态
    streamThinking: [],      // 思考块状态
    streamThinkingPhase: null,
};
```

### 9.3 渲染策略

- **初始渲染**: `render()` 全量重建 DOM
- **流式更新**: 使用 `patchXxx()` 系列函数进行**局部 DOM 更新**，避免全量 re-render 导致的闪烁
  - `patchStreamingTools()` — 更新工具 badge
  - `patchStreamingThinking()` — 更新思考块
  - `patchInputState()` — 更新按钮禁用状态
  - `patchGraphBadge()` — 更新「图已加载」badge
  - `patchErrorBanner()` — 更新错误提示
- **流式结束**: `finalizeStreamingBubble()` 将流式 DOM 节点转化为静态节点（移除 `id` 属性）

### 9.4 UI 组件

| 组件 | 说明 |
|------|------|
| 气泡消息 | 用户蓝色渐变气泡 + Agent 白色气泡 |
| 思考块 | 可折叠，三种相位：规划（绿）/ 反思（紫）/ 思考（灰） |
| 工具 badge | 执行中黄色旋转 / 完成绿色勾选 |
| 模型选择器 | Header 中的下拉框 |
| 推理模式按钮 | 开/关切换，紫色高亮 |
| 文件上传按钮 | 支持 `.txt` / `.csv` / `.edgelist` / `.dat` / `.json` / `.xlsx` / `.pdf` |
| 分析计划面板 | Plan 模式时展示步骤列表（pending/running/done/failed/skipped 五态） |
| 示例卡片 | 空状态时展示三个示例操作 |

### 9.5 SSE 消费

```javascript
async function doStreamChat(message) {
    const res = await fetch(`/api/sessions/${sid}/chat/stream`, { method: 'POST', ... });
    const reader = res.body.getReader();
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // 解析 "data: {...}\n\n" 格式
        for (const line of lines) {
            if (line.startsWith('data: '))
                handleSseEvent(JSON.parse(line.slice(6)));
        }
    }
}
```

**SSE 事件完整列表**：

| 事件类型 | 关键字段 | 触发场景 |
|---------|---------|---------|
| `thinking_start` | `phase` | 推理块开始 |
| `thinking` | `phase`, `content` | 推理文本增量 |
| `thinking_end` | `phase` | 推理块结束 |
| `token` | `content` | answer 文本增量 |
| `tool_call` | `tool`, `args`, `call_id`, `server` | 工具调用 |
| `tool_result` | `tool`, `call_id` | 工具完成 |
| `model_turn_start` | `step`, `attempt` | 模型轮次开始（含重试计数） |
| `model_turn_retry` | `step`, `attempt`, `reason` | 协议解析失败触发重试 |
| `plan_step` | `plan_id`, `step` | 计划步骤声明（Planner 流式输出时） |
| `plan_ready` | `plan` | 完整分析计划就绪 |
| `solve_step_start` | `step_id`, `goal` | Plan solver 开始执行某步骤 |
| `solve_token` | `step_id`, `content` | Plan solver 步骤回答增量 |
| `solve_thinking` | `step_id`, `phase`, `content` | Plan solver 步骤推理 |
| `solve_tool_call` | `step_id`, `tool`, `args`, `call_id` | Plan solver 工具调用 |
| `solve_tool_result` | `step_id`, `tool`, `call_id` | Plan solver 工具完成 |
| `tool_progress` | `tool`, `call_id`, `progress`, `step_id?` | 长时工具进度更新（plan 路径时携带 `step_id`） |
| `feedback_resolved` | `request` | 用户确认/拒绝后立即 emit，resume 流继续 |
| `solve_step_done` | `step_id`, `status`, `summary` | Plan solver 步骤完成 |
| `context_usage` | `current_tokens`, `token_limit`, `percent`, `status`, `compression_count` | token 使用量（每轮开始 + 压缩后） |
| `context_compressing` | `usage`, `threshold` | 即将压缩旧上下文 |
| `context_compressed` | `before`, `after`, `stats` | 压缩完成 |
| `await_user_feedback` | `request` | 等待用户确认高风险工具调用 |
| `done` | `has_graph`, `context_usage` | 本轮结束（含最终 token 状态） |
| `error` | `error` | 错误 |

`turn_end` 为内部事件，iGraph 消费后不向前端透传。

---

## 10. 进程管理（ProcessSupervisor）

### 10.1 定位

`ProcessSupervisor` 统一管理 GraphData Server 和 MCP Server 的子进程生命周期，避免在多个入口（CLI / API Gateway）重复实现进程管理逻辑。GraphML 是 GraphData 的内置子模块，随 GraphData 自动启动，无需单独管理。

### 10.2 实现

```python
class ProcessSupervisor:
    base_dir: str
    graph_data_port: int    # 默认 1111
    mcp_port: int           # 默认 1110
    graph_data_url: str     # 外部 GraphData URL（可复用已运行实例）
    _graph_data_proc: Optional[Popen]
    _mcp_proc: Optional[Popen]
    _local_graph_data_started: bool
```

### 10.3 启动顺序 (`start_all`)

```
1. start_graph_data_server()
   ├─ 若 GRAPH_DATA_SERVER_URL 配置的地址已就绪（GET /health），直接复用（不拉起子进程）
   ├─ 否则检查本地端口是否已占用，若是则复用本地实例
   ├─ 否则启动 python -m graph_data_service（start_new_session=True 独立进程组）
   └─ 轮询 /health（最多 30 次，间隔 0.3s）

2. start_mcp_server(transport="streamable-http")
   ├─ 启动 python -m mcp_service {transport}（start_new_session=True 独立进程组）
   └─ 轮询端口可连接（最多 30 次，间隔 0.3s）
```

### 10.4 就绪探测

- **HTTP 就绪**: `_wait_http_ready(url)` — 使用 `urllib.request.urlopen` 轮询
- **端口就绪**: `_wait_port_ready(host, port)` — 使用 `socket.create_connection` 轮询
- **幂等启动**: 每个 `start_xxx()` 方法先检查进程是否已存活 / 端口是否已被占用，避免重复启动

### 10.5 停止与清理

- `stop_all()`: 依次终止 MCP → GraphData 子进程
- `_stop_proc(proc)`: 向整个进程组发送 `SIGTERM`（`os.killpg(pgid, SIGTERM)`）+ `wait(5s)`，确保子进程（ML job worker 等）一并退出并释放端口；超时后 fallback 直接 `kill()` + `wait(2s)`
- `atexit.register(self.stop_all)`: 主进程退出时自动清理子进程

---

## 11. 日志与会话持久化

### 11.1 架构

```
backend/logging/
├── __init__.py
├── logger.py          # Logger（全局）+ SessionLogger（会话级）+ ServiceLogger（服务级）
└── session_store.py   # SessionStore — 会话历史持久化
```

日志根目录由 `IGRAPH_LOGS_DIR` 环境变量控制（默认 `logs/`）。

### 11.2 SessionStore — 会话历史持久化

`SessionStore` 是会话跨进程恢复的核心，为每个 session 维护一个独立目录：

```
logs/
└── {session_id}/
    ├── message_list.jsonl      # append-only，每行一条 message record
    ├── session_meta.json       # 会话元数据
    ├── trace.jsonl             # 调试 trace 事件
    ├── plan_{plan_id}.json     # 分析计划快照（Plan 模式时生成）
    └── report_{plan_id}.json   # 分析报告（可选）
```

**message_list.jsonl 记录字段**：

| 字段 | 说明 |
|------|------|
| `seq` | 自增序号 |
| `ts` | ISO8601 时间戳 |
| `session_id` | 会话 ID |
| `visible` | `true` = 可见消息（展示给用户），`false` = 内部协议消息 |
| `include_in_context` | `true` = 恢复时回灌到 LLM 上下文 |
| `kind` | 消息类型（`user_turn` / `assistant_protocol` / `tool_observation_batch` 等） |
| `message` | 原始消息 `{role, content}` |
| `display_content` | 用户可见展示文本（与 `content` 可能不同） |

**session_meta.json 字段**：`session_id` / `title`（首条用户消息前 24 字）/ `last_message_preview` / `model` / `provider` / `reasoning_mode` / `message_count` / `visible_message_count` / `last_has_graph` / `graph_node_count` / `graph_edge_count` / `graph_is_directed` / `graph_has_weights` / `graph_version` / `created_at` / `updated_at` / `archived` / `deleted`

**核心方法**：
- `load_context_messages(sid)` — 筛选 `include_in_context=true`，仅保留最新 system prompt + 所有非 system 消息（用于恢复）
- `load_chat_messages(sid)` — 筛选 `visible=true` 的 user/assistant 消息（用于前端展示）
- `list_sessions(limit)` — 按 `updated_at` 降序列出所有会话

### 11.3 Logger — 全局日志

- 写入 `logs/server.log`（INFO 级别）
- 控制台同步输出
- 持有 `SessionStore` 实例（`self.store`）
- 工厂方法 `create_session_logger()` 创建会话级日志
- 代理方法：`list_sessions()` / `get_session_meta()` / `load_context_messages()` / `load_chat_messages()`

### 11.4 SessionLogger — 会话级日志

每个会话的消息和 trace 均通过 `SessionStore` 写入 `logs/{session_id}/`：

**日志事件类型**:

| 事件 | 说明 |
|------|------|
| `SESSION_START` | 会话创建，记录 provider/model |
| `USER_MESSAGE` | 用户输入 |
| `ASSISTANT_MESSAGE` | Agent 最终回复 |
| `TOOL_CALL` | 工具调用（名称 + 参数） |
| `TOOL_RESULT` | 工具结果 |
| `TOOL_EXEC_RAW` | MCP 工具原始执行信息（含耗时 ms） |
| `THINKING` | 思考/规划/反思内容 |
| `MODEL_REQUEST` | 发送给 LLM 的完整消息列表 |
| `MODEL_RESPONSE_RAW` | LLM 原始输出 |
| `MODEL_RESPONSE_PARSED` | 协议解析结果（think/tool_calls/answer） |
| `PROTOCOL_NORMALIZED` | 协议归一化信息 |
| `PROTOCOL_WARNING` | 协议异常警告 |
| `PROVIDER_REQUEST_RAW` | Provider SDK 原始请求 payload |
| `PROVIDER_RESPONSE_RAW` | Provider SDK 原始响应 |
| `CONTEXT_SNAPSHOT` | 完整消息列表快照（含序号和内容预览） |
| `TOOLS_SNAPSHOT` | MCP 工具列表快照 |
| `MCP_CONNECTED` | MCP 连接建立 |
| `ERROR` | 错误信息 |
| `SESSION_END` | 会话结束 |

---

## 12. 会话生命周期

### 12.1 创建

```
POST /api/sessions {model, reasoning_mode}
    │
    ▼
ApiGatewayServer._evict_idle_agents()   ← LRU 淘汰（超时 600s / 上限 5 个）
    │
    ▼
ApiGatewayServer._create_agent()
    ├─ uuid4()[:8] 生成 session_id
    ├─ Logger.create_session_logger()
    └─ IGraph(session_logger, model, reasoning_mode, planning_mode, mcp_url, memory_manager)
         ├─ ProviderGateway(model, api_key, base_url)
         │   └─ AsyncOpenAI client
         ├─ ContextManager(session_id, reasoning_mode, provider_gateway, logger)
         │   ├─ messages = [system_prompt]
         │   ├─ 写入 SessionStore（message_list.jsonl）
         │   └─ create_driver() → TextProtocolDriver 或 NativeFCDriver（内部化）
         ├─ MemoryManager.load_and_freeze(DEFAULT_SCOPE)  ← 冻结 _memory_prompt_block
         └─ RawMCPClient(mcp_url)
              └─ 尚未连接（延迟到首次 chat）
```

### 12.2 激活/切换历史会话（惰性，无副作用）

```
POST /api/sessions/{sid}/activate
    │
    ├─ 若 sid 在 _agents 中（已是活跃 Agent）
    │     └─ 直接返回当前 agent 状态（has_graph/model/reasoning_mode）
    │        + load_chat_messages() 的可见历史
    │
    └─ 若不在 _agents 中
          ├─ 读取 session_meta.json（model/reasoning_mode/last_has_graph）
          ├─ load_chat_messages() 的可见历史
          └─ 返回 {restored: true, has_graph, messages, ...}
             ← 不创建 Agent，不建立 MCP 连接，不触发淘汰
```

**Agent 在第一次 chat_stream 时才惰性创建**（`_restore_agent_if_needed`）：读取上下文消息并重建 `ContextManager`，建立 MCP 连接并注册 session。

图数据现支持快照持久化：GraphDataService 启动时自动从磁盘恢复（`_restore_sessions_from_disk`）。若快照损坏或不存在，`requires_graph_reload=True` 时前端提示用户重新加载图。

### 12.3 首次对话（惰性 Agent 初始化）

```
chat_stream(user_input)
    │
    ▼
_restore_agent_if_needed(sid)   ← 若不在内存，此时才创建 Agent
    ├─ load_context_messages(sid) → 重建上下文消息
    └─ _create_agent(restored_messages, restored_has_graph=False)
    │
    ▼
_ensure_connected()
    ├─ RawMCPClient.open() → initialize RPC
    ├─ list_tools → _tool_specs / _visible_tool_specs / _tool_map
    └─ call_tool("register_session", {agent_session_id})
         ├─ MCP: _session_registry.register(mcp_key, sid)
         └─ GraphData: POST /sessions/{sid} → 创建 GraphSession
```

### 12.4 对话中

```
每轮 chat_stream:
    user message → LLM → <think>/<tool_call>/<answer>
                          │
    <tool_call> ──────────▼──────────────
         │
    MCP Server: _sid(ctx) → session_id
         │
    GraphData Client / GraphCompute / GraphML Client
```

### 12.5 销毁

```
DELETE /api/sessions/{sid}
    │
    ▼
agent.cleanup()
    ├─ logger.log_session_end()
    ├─ context_manager.messages.clear()
    ├─ _has_graph = False
    ├─ _cleanup_mcp()
    │   ├─ call_tool("delete_graph_session")
    │   │   ├─ MCP: _session_registry.pop(key)
    │   │   ├─ MCP: _compute.invalidate(sid)
    │   │   └─ GraphData: DELETE /sessions/{sid}
    │   └─ _mcp_client.close()
    └─ 停止后台事件循环
```

### 12.6 关键端到端时序

以下为高频操作的紧凑时序图，方便快速排查问题。

**新建会话**
```
POST /api/sessions {model, reasoning_mode}
  → ApiGatewayServer._create_session()
  → SessionStore.create_session(model, ...)
  → 返回 {session_id, model, reasoning_mode}
  （Agent 尚未创建，MCP 尚未连接）
```

**激活/切换历史会话（无副作用）**
```
POST /api/sessions/{id}/activate
  → 若 Agent 已在内存 → 直接返回当前状态
  → 否则 → 只读 session_meta.json → 返回 {messages, model, has_graph, ...}
  → Agent 不创建，其他会话不受影响
```

**首次聊天（惰性 Agent 初始化）**
```
POST /api/sessions/{id}/chat/stream
  → _restore_agent_if_needed(id)
      → load_context_messages() + 创建 iGraph
      → IGraph.__init__
          → MemoryManager.load_and_freeze()   ← 冻结 memory snapshot
      → _ensure_connected()
          → RawMCPClient.open() → list_tools()
          → LLMToolSelector 初始化
          → register_session
  → _chat_stream_impl(user_input)
```

**工具调用链路（以 shortest_path 为例）**
```
IGraph._run_tool(tc)
  → RawMCPClient.call_tool("shortest_path", args)
  → MCP Server: _invoke_graph_tool(name, ctx, kwargs)
  → GraphDataClient.call_tool(sid, name, args)
  → GraphData POST /sessions/{sid}/tools/shortest_path
  → GraphDataService.dispatch_tool() → GraphCompute.shortest_path()
  → 结果逐层返回 → iGraph → SSE token 推送前端
```

**Virtual tool 调用链路（以 skill_view 为例）**
```
IGraph._run_tool({"name":"skill_view","args":{...}})
  → _handle_virtual_tool("skill_view", args)   ← 不走 MCP
  → handle_skill_tool_call("skill_view", args, _skill_registry)
  → SkillRegistry.get(name) → 读文件 → config 变量替换
  → 返回 skill body 字符串
```

**会话删除**
```
DELETE /api/sessions/{id}
  → agent.cleanup()（若在内存）
      → delete_graph_session → MCP → GraphData.DELETE /sessions/{sid}
      → _mcp_client.close()
  → SessionStore.update_meta({deleted: True})
GET /api/sessions → 过滤 deleted=True，该会话不再返回
```

---

## 13. Memory、Skills 与 Tools Selector

> 详细实现见 `docs/SKILLS_MEMORY.md`，本节补充设计背景与关键取舍。

### 13.1 知识三层分离原则

| 层 | 存放内容 | 典型判断 | 载体 |
|----|---------|---------|------|
| **Memory** | 稳定事实：用户偏好、项目约定、环境约束 | "以后需要记住什么" | `MemoryEngine` |
| **Skills** | 可复用流程：工具顺序、踩坑、验证方式 | "以后遇到同类任务该怎么做" | `SkillRegistry` |
| **Session** | 过程历史：任务进展、一次性结果 | "这次做到了哪里" | `ContextManager` / `SessionStore` |

三层严格分离：会话进展**不**写入 Memory；Memory 的事实**不**变成 Skill；Skill 的操作流程**不**混入 Memory。

### 13.2 Memory — MemoryManager + BuiltinMemoryProvider + MemoryEngine

**架构层次**：

```
ApiGatewayServer（单例）
  ├─ MemoryManager（manager.py）
  │    ├─ BuiltinMemoryProvider（builtin_provider.py）
  │    │    └─ MemoryEngine（memory_engine.py）
  │    │         ├─ MemoryPathResolver（paths.py）  ← 按 scope 解析 ~/.igraph/memory/ 层次
  │    │         ├─ long_term_memory / profile / playbooks（持久化在 scope_dir 下）
  │    │         ├─ memory_events.jsonl（审计日志）
  │    │         └─ PlaybookReviewer（skills/playbook_reviewer.py）← validated playbook 自动晋升
  │    └─ OptionalExternalProvider（可选，失败不阻塞）
  └─ MemoryService（memory/service.py）← REST governance，供 /api/memory/* 端点调用
```

`MemoryManager` 实例由 `ApiGatewayServer` 全局唯一创建，注入到每个 `iGraph`（所有会话共享同一记忆库，按 `category` 和 `key` 去重）。

**Frozen Snapshot 模式（替代旧版 per-turn recall）**：

```
旧版：每 turn 重新 recall → Top-K 注入 memory prompt → system prompt 每轮变化 → prefix cache 失效
新版：会话开始时 MemoryManager.load_and_freeze() → 冻结 _memory_prompt_block → 整 session 不变
      后台写入 → 内存 + 磁盘更新，本 session system prompt 不刷新，下次 session 生效
```

**写入决策**（sub-agent LLM）：

每轮 answer 产生后，`_schedule_memory_update()` 异步串行调用 `MemoryEngine.maybe_update_from_turn()`：
1. `_build_sub_agent_payload()`：包含 existing_memories + **existing_profile** + existing_playbooks + recent_messages
2. 调用辅助 LLM（`invoke_text`，step=100000+），输出：
   ```json
   {
     "should_write": true, "reason": "...",
     "operations": [{"op": "upsert", "category": "profile|preference|goal|...", "key": "...", "value": "...", "confidence": 0.9}],
     "should_write_playbook": true, "playbook_reason": "...",
     "playbook_operations": [{"op": "upsert_playbook", "problem_type": "...", "approach": "...", ...}]
   }
   ```
3. `_apply_operations()`：`category=="profile"` → `user_profile.json`；其余 → `long_term_memory.json`
4. `_apply_playbook_operations()` → `problem_solving_playbooks.json`
   - playbook lifecycle：`candidate` → `validated`（evidence≥2）→ `promoted_to_skill`（approve 后）
   - `PlaybookReviewer.review_snapshot()`：对 `validated` playbook 自动生成 review_queue 候选 skill
5. 原子写入（temp+fsync+os.replace）+ 审计 JSONL append

**UserProfile 独立存储（新）**：

- 旧版：profile 类别写进 `long_term_memory.json`，与其他事实混存
- 新版：`category=="profile"` 路由到 `user_profile.json`，由 `UserProfile` 数据类渲染
- `to_snapshot_lines()` 按字段优先级截断（language/expertise_level 优先），上限 1500 字符

**安全保障**：

- 所有写入前经 `scan_memory_content()`（`safety.py`）检测注入 pattern；命中 → `memory_reject` 事件，不入快照
- JSON 写入走 `atomic_json_write()`（temp+fsync+os.replace）
- JSONL append 走 `append_jsonl()`（fcntl.flock + fsync）
- 迁移幂等：旧路径 `memory/memory/default_user/` 在首次 init 合并，写 `.migrated` flag，不重复执行

**字符预算**（确保全量加载不超出 prefix cache 价值范围）：

| 内容 | 上限 | 裁剪依据 |
|------|------|---------|
| user_profile | 1500 字符 | 字段优先级顺序截断 |
| long_term_memory | 8000 字符 | confidence × 0.5 + recency × 0.3 + hit_rate × 0.2 降序 |
| playbooks | 4000 字符 | 同上（只保留 active 状态） |

**Memory Virtual Tools**：

| 工具 | 实现 | 描述 |
|------|------|------|
| `memory_read` | `handle_memory_tool_call()` | 关键词评分查询 long_term / playbook / user_profile；无 embedding |
| `memory_delete` | `handle_memory_tool_call()` | 软删除（status→archived）+ 原子写 + 审计事件 |

`memory_read` 是核心工具集成员（`CORE_TOOL_NAMES`），每轮自动加载。

**容量管理**：active 条目超 120 条时，`_prune_snapshot()` 按 confidence + updated_at 降序保留前 120 条（最少 20 条），archived 保留最近 100 条。

### 13.3 Skills — SkillRegistry + 渐进披露

**设计目标**：技能不再硬编码判断，通过 frontmatter 声明激活条件；全文不再注入 system prompt，只注入 compact index，按需 `skill_view` 加载。

**SKILL.md frontmatter 结构**：

```yaml
---
name: graph_machine_learning
description: ...
domain: graph_ml
risk_level: high
metadata:
  igraph:
    requires_tools: [train, infer]  # AND
    any_tools: [train, infer]                             # OR
    requires_graph_state: none
    activates_on_intents: [graph_ml_train, graph_ml_infer]
    config:                   # P1.4：声明配置变量键
      - key: ml.default_epochs
---
```

**激活条件评估**（`_is_active(spec, names, graph_state, intent)`）：
- `requires_tools`（AND）：全部工具名需在 `visible_tool_specs` 中
- `any_tools`（OR）：任一工具名存在即激活
- 基于**全量可见工具**（`_visible_tool_specs`），与 `LLMToolSelector` 入选子集无关

**compact index**（`build_skills_system_prompt()`，≤ 2000 字符）：

```
## Available Skills
- **graph_machine_learning**: GraphML 任务执行规范...
...
调用 skill_view(name) 加载完整指引
```

**新增技能只需**：新建目录 + `SKILL.md`，无需修改任何 Python。

**三类来源**：`bundled`（代码内置，只读）/ `user`（`~/.igraph/skills/user/`，用户自建）/ `review_queue`（`~/.igraph/skills/.review_queue/`，playbook 自动晋升候选）。

**Virtual Tools**：

| 工具 | 说明 |
|------|------|
| `skills_list(domain?)` | 列出 compact index，可按 domain 过滤 |
| `skill_view(name, file_path?)` | 加载完整 body；frontmatter `config` 声明的 `${key}` 替换为 `~/.igraph/config.yaml` 的值 |
| `skill_manage(action, name, ...)` | 创建/维护/审批用户或候选 skill（`create`/`write_file`/`remove_file`/`approve`/`reject`，只写 user/review_queue） |

**Playbook → Skill 晋升管道**：`PlaybookReviewer` 在 `MemoryEngine.maybe_update_from_turn()` 末尾自动触发，将 `lifecycle_status=validated` 的 playbook 生成 review_queue 候选 skill，等待用户通过 `skill_manage(action="approve")` 批准后移入 user 目录。playbook 随即标记为 `promoted_to_skill`，不再出现在记忆快照。

**安全**：`guard.py` — skill body 过注入扫描；supporting file 路径保护（阻止 `../../etc/passwd`、SKILL.md 自引用）；`SkillManager` 只写 user/review_queue，无法修改 bundled 技能。

### 13.4 Tools LLM Selector — 替换 embedding

**背景**：旧版 `embedding.py` 的 `ToolSelector` 使用 sentence-transformers 向量相似度过滤工具，存在：不可解释、不感知图状态、冷启动慢、依赖 torch 包 等问题。

**新设计**（`backend/agent/tools/selector.py`）：

```python
class LLMToolSelector(SubAgent):
    def __init__(self, tool_specs, *, provider_gateway, audit_path, model_version, ...): ...

    def set_turn_id(self, turn_id: str) -> None:
        """新 turn 开始时清空 per-turn 缓存，同一 turn 多步复用一次决策。"""

    async def select(self, req: ToolSelectionRequest, k: int = 8) -> ToolSelectionResult:
        """LLM 选工具；失败时 fallback 到 _keyword_select()（不返回全量）。"""
```

**核心工具集**（`CORE_TOOL_NAMES`，永不进 selector 裁剪）：
```python
{"get_graph_info", "skills_list", "skill_view", "tool_expand", "memory_read"}
```

**tool_expand 逃生通道**：selector 漏选时，主 agent 可调用 `tool_expand(name)` 将目标工具 schema 加入后续上下文，加入 `_expanded_tool_names` 集合。

**审计**：每次 selector 调用写 `logs/tool_selection_events.jsonl`，记录候选集、选中集、reasoning、latency、fallback 标记。

**删除内容**：`backend/agent/embedding.py`（`ToolSelector` + `EmbeddingBackend` + `SemanticMemoryIndex`）完整删除，零 sentence-transformers / torch 依赖（GraphML 训练/推理相关的 torch 依赖保留于 graph_data_service）。

### 13.5 System Prompt 组装顺序（最终版）

```
[1] Role system prompt      ← 整 session 不变（messages[0]）
[2] Reasoning mode rule     ← system msg，每轮动态注入
[3] Memory frozen snapshot  ← 整 session 不变（load_and_freeze() 冻结）
    = user_profile（1500 字符上限）
    + long_term_memory（8000 字符上限）
    + validated playbooks（4000 字符上限）
[4] Selected tools schema   ← LLMToolSelector 入选集 + CORE_TOOL_NAMES（text 模式写 system；native 走 tools 参数）
[5] Skills compact index    ← 每轮按全量可见工具 + 图状态计算，≤ 2000 字符
[6] 对话历史
```

**[3] vs [5] 稳定性**：[3] 整 session 不变，保护 prefix cache；[5] 每轮重算，有 LRU 内存缓存加速。

---

## 14. 协议驱动器设计（双模式）

### 14.1 设计动机与双模式选择

本系统支持两种协议模式，通过环境变量 `LLM_TOOL_PROTOCOL` 切换：

| | 文本协议（text，默认） | 原生 FC（native） |
|---|---|---|
| **工具描述位置** | system prompt 中的 JSON | API `tools` 参数 |
| **LLM 输出格式** | `<think>/<tool_call>/<answer>` 文本标签 | `tool_calls` 结构化字段 |
| **上下文透明性** | 完全透明（所见即所得） | 黑盒（API 内部处理） |
| **SFT/RL 数据采集** | 适合（上下文 = 训练样本） | 不适合 |
| **解析可靠性** | 依赖 LLM 标签遵从性 | API 契约保证 |
| **推理 token 展示** | `<think>` 块 | `reasoning_content` 字段（推理模型） |

两种模式对外产生**完全相同的 SSE 事件流**，前端无感知。

### 14.2 文本协议规则（text 模式）

每轮 LLM 输出必须是以下**双标签格式之一**：

```xml
<think>思考/规划/反思内容</think><tool_call>{"tool_calls":[...]}</tool_call>
<think>思考/规划/反思内容</think><answer>最终回答（Markdown）</answer>
```

**约束**：
- 每轮恰好一个 `<think>` + 一个动作块，标签外不得有正文
- `</tool_call>` 或 `</answer>` 出现后必须立即结束输出
- `<tool_call>` 内只能是 JSON，不能有解释文字
- `<answer>` 内是中文 Markdown 正文

**模式提示注入（重构后）**：不再追加到用户消息，而是作为 system message 动态注入到每轮 `build_model_messages()` 前缀中：

- `staged`（推理）：`<think>` 充分展开，规划求解路径或反思工具结果
- `direct`（直接）：`<think>` 简短（1-2 句）或空 `<think></think>`

### 14.3 StatefulStreamParser（文本协议核心解析器）

文件：`backend/agent/context/text_protocol_driver.py`

替代原有的 `ProtocolStreamParser` + `normalize_model_output` + `parse_model_output` 三步链：

**一次解析，同时产生事件 + 积累结果**：

```
LLM token 流
    │
    ▼
StatefulStreamParser.feed(chunk)
    │
    ├─ <think> 打开  → thinking_start 事件；积累到 _think_chunks
    ├─ think 块文本  → thinking 事件；积累到 _think_chunks
    ├─ </think> 关闭 → thinking_end 事件，phase=None，继续解析（不关闭）
    │
    ├─ <tool_call> 打开 → 进入 tool_calls phase（不输出事件）
    ├─ tool_call 块文本 → 积累到 _tool_call_chunks（JSON 内容）
    ├─ </tool_call> 关闭 → _action="tool_calls"，parser.closed=True
    │
    ├─ <answer> 打开   → 进入 answer phase
    ├─ answer 块文本   → token 事件；积累到 _answer_chunks
    └─ </answer> 关闭  → _action="answer"，parser.closed=True

流结束后直接读取（不再二次 parse）：
    parser.action       # "tool_calls" | "answer"
    parser.think_text   # str
    parser.answer_text  # str
    parser.tool_calls   # list[dict]，已解析和标准化
    parser.raw_text     # 完整原始文本（供日志记录）
```

**内建容错**（替代原 `normalize_model_output`）：

| 情况 | 处理 |
|---|---|
| 无任何标签 | `flush()` 时全文作为 answer |
| 只有 `<think>` 无后续 | `flush()` 时 think 内容作为 answer |
| 多个动作标签 | 只保留第一个 |
| 标签未闭合 | `flush()` 时自动处理 |

### 14.4 原生 FC 模式（native 模式）

文件：`backend/agent/context/native_fc_driver.py`

- 工具通过 `ProviderGateway.create_streaming_completion(messages, tools=[...])` 传入
- 流式读取各类 delta：
  - `delta.reasoning_content`（推理模型 o3/o4 等）→ thinking 事件
  - `delta.content`（普通 content）→ token 事件（纯回答）
  - `delta.tool_calls`（工具调用）→ 积累后 turn_end 时解析
- `finish_reason="tool_calls"` → turn_end action="tool_calls"
- `finish_reason="stop"` → turn_end action="answer"

### 14.5 tool observation 消息（双模式）

工具调用结果格式因驱动器不同：

**text 模式**（TextProtocolDriver）：转换为 `role=user` 自然语言消息：

```
以下是本轮工具调用结果，请基于结果继续回答：
shortest_path 工具调用结果为：{...}
```

**native 模式**（NativeFCDriver）：使用 OpenAI 原生 `role=tool` 消息：

```python
[{"role": "tool", "tool_call_id": "call_1", "content": "{...}", "name": "shortest_path"}]
```

---

## 15. GraphData 数据模型

### 14.1 核心字段

`GraphData` 是系统的统一图数据容器，支持同质图与异质图，只读不可变。

| 字段 | 类型 | 说明 |
|------|------|------|
| `_num_nodes` | `int` | 节点总数 N |
| `_edge_index` | `np.ndarray [2, E] int64` | 边索引，`[0]`=源节点 `[1]`=目标节点 |
| `_directed` | `bool` | 是否有向图 |
| `name` | `str` | 图名称 |
| `_id2node` | `Dict[int, str]` | 节点 ID → 名称 |
| `_node2id` | `Dict[str, int]` | 节点名称 → ID |
| `_edge_names` | `np.ndarray [E] str` | 边名称（可选） |
| `_edge_weight` | `np.ndarray [E] float32` | 边权重（可选） |
| `_node_feat` | `np.ndarray [N, *] float32` | 节点特征（可选） |
| `_edge_feat` | `np.ndarray [E, *] float32` | 边特征（可选） |
| `_graph_desc` | `str` | 图文本描述（可选） |
| `_node_label` | `np.ndarray [N] / [N, C]` | 节点标签（可选） |
| `_node_type` | `np.ndarray [N] object` | 节点类型名（可选） |
| `_edge_label` | `np.ndarray [E] / [E, C]` | 边标签（可选） |
| `_edge_type` | `np.ndarray [E] object` | 边类型名（可选） |
| `_graph_label` | `str / int / np.ndarray` | 图级标签（可选） |

### 14.2 快速查找表

在构造时一次性建立，后续查询均为 O(1)：

| 查找表 | Key | Value | 用途 |
|--------|-----|-------|------|
| `_edge_lookup` | `(src_id, dst_id)` | `{edge_idx, weight, feat, name, label, type}` | 边属性查询 |
| `_edge_name_lookup` | `edge_name` | `(src_id, dst_id)` | 按边名查找端点 |
| `_node_type_lookup` | `type_name` | `List[node_id]` | 按类型查找节点 |
| `_edge_type_lookup` | `type_name` | `List[edge_idx]` | 按类型查找边 |

### 14.3 查询接口

GraphData 提供丰富的查询接口，均有 `by_id` 和 `by_name` 两种变体：

- **节点**: `id_to_name` / `name_to_id` / `has_node_name` / `has_node_id`
- **节点特征**: `get_node_feat_by_id` / `get_node_feat_by_name` / `get_node_feats_by_ids`
- **节点标签/类型**: `get_node_label_by_id` / `get_node_type_by_id` / `get_nodes_by_type`
- **边查询**: `get_edge` / `has_edge` / `get_edge_weight` / `get_edge_feat` / `get_edge_name` / `get_edge_label` / `get_edge_type`
- **拓扑**: `neighbors` / `predecessors` / `in_degree` / `out_degree` / `degree` / `get_incident_edges`
- **枚举**: `all_edges` / `all_edges_by_name`

### 14.4 图变换

| 方法 | 说明 |
|------|------|
| `subgraph(node_ids)` | 提取子图，ID 重新映射，所有字段裁剪 |
| `to_undirected()` | 转为无向图，反向边复制正向属性，去重 |
| `to_networkx()` | 转为 NetworkX 图（委托 `adapters.py`） |

### 14.5 序列化

支持两种持久化格式（委托 `io.py`）：

| 格式 | 方法 | 适用场景 |
|------|------|---------|
| `.npz` | `np.savez` / `np.load` | 高效二进制存储，适合大图 |
| `.json` | `graph_data.v1` schema | 人类可读，适合调试和交换 |

---

## 16. GraphData JSON 格式与序列化

### 15.1 graph_data.v1 JSON Schema

```json
{
  "schema": "graph_data.v1",
  "num_nodes": 6,
  "directed": true,
  "name": "example",
  "graph_desc": "一个示例图",
  "id2node": {"0": "A", "1": "B", "2": "C", "3": "D", "4": "E", "5": "F"},
  "edge_index": [[0, 0, 1, 1, 2, 3, 4], [1, 2, 2, 3, 3, 4, 5]],
  "edge_weight": [4.0, 2.0, 1.0, 5.0, 8.0, 2.0, 3.0],
  "edge_names": null,
  "node_feat": null,
  "edge_feat": null,
  "node_label": null,
  "node_type": null,
  "edge_label": null,
  "edge_type": null,
  "graph_label": null
}
```

### 15.2 .npz 格式

使用 NumPy 的 `np.savez` 存储，key 约定：

| Key | 说明 |
|-----|------|
| `_num_nodes` | 节点数（标量） |
| `_directed` | 是否有向（标量） |
| `_name` | 图名称（字符串标量） |
| `_graph_desc` | 图描述（可选） |
| `edge_index` | `[2, E]` int64 |
| `map_ids` / `map_names` | 节点 ID/名称映射数组 |
| `edge_weight` | `[E]` float32（可选） |
| `node_feat` | `[N, D]` float32（可选） |
| `edge_feat` | `[E, D]` float32（可选） |
| `node_label` / `edge_label` | 标签数组（可选） |
| `node_type` / `edge_type` | 类型字符串数组（可选） |
| `graph_label` / `_graph_label_type` | 图级标签 + 类型标记（可选） |

---

## 17. MCP 工具清单与调用数据流

> **架构说明**: 计算层（`GraphCompute`）已迁移至 GraphData Service 内部，MCP Service 是纯代理层。所有图工具均通过 `GraphDataClient.call_tool()` → `POST /sessions/{sid}/tools/{tool_name}` 在 GraphData Service 内执行。

### 16.1 完整工具列表

| 工具名 | 分类 | 说明 | 可见 |
|--------|------|------|------|
| `register_session` | 会话管理 | 注册 MCP↔Agent 会话映射 | 否 |
| `delete_graph_session` | 会话管理 | 删除会话图状态 | 否 |
| `load_graph` | 数据操作 | 从文本边列表加载图 | 是 |
| `load_graph_from_upload` | 数据操作 | 从上传文件加载图 | 是 |
| `clear_graph` | 数据操作 | 清空当前图 | 是 |
| `undo_graph` | 数据操作 | 回滚到上一版本快照 | 是 |
| `to_undirected` | 数据操作 | 转为无向图 | 是 |
| `list_datasets` | 数据集 | 列出所有已注册数据集 | 是 |
| `load_dataset` | 数据集 | 从数据集加载图到当前会话 | 是 |
| `save_as_dataset` | 数据集 | 将当前图保存为数据集 | 是 |
| `build_graph_from_text` | 建图 | 从自然语言文本分块抽取三元组并建图（支持长文本、LLM 辅助、去重、追加） | 是 |
| `build_graph_from_triples` | 建图 | 从结构化三元组列表建图（relation→边类型，evidence→边文本，confidence→边特征；支持覆盖/追加） | 是 |
| `upload_document` | RAG | 上传文档到会话知识库 | 是 |
| `retrieve_context` | RAG | 语义检索相关文档片段 | 是 |
| `get_graph_info` | 图查询 | 查询图全局信息 | 是 |
| `get_node_info` | 图查询 | 查询节点详细信息 | 是 |
| `get_edge_info` | 图查询 | 查询边详细信息 | 是 |
| `check_edge_exists` | 图查询 | 检查边是否存在 | 是 |
| `search_node` | 图查询 | 节点实体搜索（名称/文本/语义/类型/度数过滤，返回 best + candidates） | 是 |
| `search_edge` | 图查询 | 边实体搜索（名称/文本/语义/端点/类型/权重过滤，返回 best + candidates） | 是 |
| `shortest_path` | 图算法 | 最短路径（单对 / 单源全目标 / Yen K-shortest） | 是 |
| `all_shortest_paths` | 图算法 | 所有等长最短路径 | 是 |
| `maximum_flow` | 图算法 | 最大流 | 是 |
| `minimum_cut` | 图算法 | 最小割 | 是 |
| `minimum_spanning_tree` | 图算法 | 最小生成树/森林 | 是 |
| `community_detection` | 图算法 | 社区检测（7 种算法：louvain/greedy_modularity/label_propagation/leiden/girvan_newman/asyn_fluidc/label_propagation_asyn） | 是 |
| `node_centrality` | 图算法 | 节点中心性（10 种类型：degree/betweenness/closeness/pagerank/eigenvector/harmonic/katz/load/voterank/percolation） | 是 |
| `graph_connectivity` | 图算法 | 连通性分析 | 是 |
| `find_cycles` | 图算法 | 环检测 | 是 |
| `topological_sort` | 图算法 | 拓扑排序 | 是 |
| `degree_analysis` | 图算法 | 度分布分析 | 是 |
| `subgraph_extraction` | 图算法 | 子图提取（ego_graph/k_core/k_truss/k_corona/node_subset） | 是 |
| `link_prediction_score` | 图算法 | 链路预测评分（jaccard/adamic_adar/resource_allocation/preferential_attachment） | 是 |
| `path_analysis` | 图算法 | 路径分析（all_simple_paths/has_path/average_shortest_path_length） | 是 |
| `clique_analysis` | 图算法 | 团分析（find_cliques/clique_number/number_of_cliques） | 是 |
| `reachability` | 图算法 | 可达性分析（transitive_closure/descendants/ancestors） | 是 |
| `global_metrics` | 图算法 | 全局图指标（density/diameter/radius/transitivity/average_clustering/degree_assortativity） | 是 |
| `get_job_status` | 作业 | 查询异步作业状态 | 是 |
| `cancel_job` | 作业 | 取消长时间作业 | 是 |
| `visualize_model_architecture` | 图 ML | 可视化模型结构（Mermaid + 层级摘要） | 条件 |
| `train` | 图 ML | 训练 GNN 模型 | 条件 |
| `infer` | 图 ML | 模型推理 | 条件 |

### 16.2 工具调用数据流

以 `shortest_path` 为例（重构后）：

```
Agent (iGraph)
  │  call_tool("shortest_path", {"source":"A","target":"F"})
  │
  ▼
RawMCPClient
  │  POST http://localhost:1110/mcp
  │  {"jsonrpc":"2.0", "method":"tools/call", "params":{"name":"shortest_path", ...}}
  │
  ▼
MCP Server (FastMCP)
  │  @mcp.tool() shortest_path(source, target, ctx)
  │  sid = _sid(ctx)  → "abc12345"
  │  _invoke_graph_tool("shortest_path", ctx, {"source":"A","target":"F","use_weight":True})
  │
  ▼
GraphDataClient.call_tool(sid, "shortest_path", arguments)
  │  POST http://localhost:1111/sessions/abc12345/tools/shortest_path
  │  body: {"arguments": {"source":"A","target":"F","use_weight":true}}
  │
  ▼
GraphData Server: execute_tool("abc12345", "shortest_path", arguments)
  │  _require_graph(session_id)
  │  ├─ GraphSession.get_graph() → GraphData 对象
  │  └─ GraphCompute.resolve_graph(sid, version, gd)
  │      ├─ 缓存命中 → 返回 nx.DiGraph
  │      └─ 缓存未命中 → gd.to_networkx()
  │
  ▼
GraphCompute.shortest_path(nx_graph, "A", "F", True)
  │  nx.shortest_path(g, "A", "F", weight="weight")
  │
  ▼
返回 JSON dict → HTTP 响应 → GraphDataClient → MCP → RawMCPClient → Agent
```

**对比旧版变化**:
- 旧版: MCP → `GET /graph/document` 获取完整图文档 → MCP 本地解码 → MCP 本地 NetworkX 计算
- 新版: MCP → `POST /tools/{name}` → GraphData Service 内部直接访问 GraphData 对象并计算

---

## 18. 图机器学习任务详解

### 17.1 节点分类

**训练流程**:
1. `parse_node_labels()`: 解析 GraphData 中的 `node_label`
   - 整数标签 → 直接使用
   - 字符串标签 → 自动编码为整数
   - one-hot 矩阵 → argmax
2. `split_indices()`: 划分 train/val/test（默认 70/10/20）
3. 构建 GNN backbone（GCN/GAT/SAGE）+ softmax 分类头
4. 标准交叉熵损失训练
5. 保存模型权重 + meta.json

**推理流程**:
1. 加载模型权重 + meta.json
2. 前向传播获取 logits
3. argmax 或 softmax 输出类别/概率
4. 支持指定 `query.nodes` 仅推理部分节点

### 17.2 链接预测

**训练流程**:
1. `collect_positive_edges()`: 收集正样本边
2. `sample_negative_edges()`: 随机采样负样本
3. 构建 GNN backbone，输出节点嵌入
4. 链接分数 = 源节点嵌入 · 目标节点嵌入（点积）
5. BCE 损失训练
6. 评估指标: AUC + 最佳阈值

**推理流程**:
1. 前向传播获取节点嵌入
2. 对查询边对计算点积分数
3. 返回分数 + 二值预测

### 17.3 预训练

**Node2Vec**:
- 基于随机游走的节点嵌入方法
- 不需要 GNN 模型
- 输出 `embedding.npy` + `node_names.json`

**对比学习 (contrastive_learning)**:
- 使用 GNN backbone
- 图增强 + 对比损失
- 输出节点嵌入

**掩码自编码 (mask_autoencoding)**:
- 随机遮蔽部分节点特征
- GNN 编码 + 重建损失
- 输出节点嵌入

预训练产生的嵌入可作为下游任务的输入特征（`feature_source="embedding"` + `upstream_model_id`）。

---

## 19. 多 LLM 后端支持

### 18.1 支持的模型

| 模型 | Provider | 环境变量 (API Key) | 环境变量 (Base URL) |
|------|----------|-------------------|-------------------|
| `claude-opus-4-6` | anthropic | `LLM_API_KEY` | `LLM_BASE_URL` |
| `claude-sonnet-4-6` | anthropic | 同上 | 同上 |
| `gpt-5-4` | openai | `LLM_API_KEY` | `LLM_BASE_URL` |
| `gpt-5-2` | openai | 同上 | 同上 |
| `qwen3.6-plus` | qwen | `QWEN_API_KEY` / `DASHSCOPE_API_KEY` / `LLM_API_KEY` | `QWEN_BASE_URL` / `DASHSCOPE_BASE_URL` / `LLM_BASE_URL` |
| `qwen3.7-max` | qwen | 同上 | 同上 |

### 18.2 ProviderGateway — 统一接入层

所有 LLM 都通过 OpenAI SDK 的 **OpenAI-compatible 端点** 调用：

```python
class ProviderGateway:
    _client: AsyncOpenAI    # 统一使用 openai SDK
```

**消息归一化** (`_to_provider_messages`):
1. `role="tool"` → 转换为 `role="user"` + 自然语言结果文本
2. `role="assistant"` → 清洗 `<tool_call>` 中的空 `call_id`（避免上游报错）
3. Claude 系列：请求末尾若为 assistant，追加 user 续写指令（Claude 不支持 assistant prefill）

**流式/非流式**:
- `stream_text()`: 流式生成，逐 chunk yield delta 文本
- `invoke_text()`: 非流式调用，返回完整文本（用于强制 answer 兜底）

### 18.3 超时配置

- 默认 180s（`LLM_TIMEOUT_SECONDS` / `OPENAI_TIMEOUT_SECONDS`）
- 所有 SDK 调用均设置 timeout

---

## 20. 环境变量与配置

### 19.1 LLM 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_API_KEY` | OpenAI / Anthropic provider 使用的 API Key（必填） | — |
| `LLM_BASE_URL` | OpenAI / Anthropic provider 使用的 Base URL（必填） | — |
| `QWEN_API_KEY` / `DASHSCOPE_API_KEY` | 通义千问 API Key（Qwen 模型可选，否则也用 `LLM_API_KEY`） | — |
| `QWEN_BASE_URL` / `DASHSCOPE_BASE_URL` | 通义千问端点（Qwen 模型可选） | — |
| `LLM_TIMEOUT_SECONDS` | LLM 调用超时秒数 | `180` |

### 19.2 服务端口

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `API_SERVER_HOST` | API Gateway 监听地址 | `0.0.0.0` |
| `API_SERVER_PORT` | API Gateway 端口 | `1109` |
| `MCP_HOST` | MCP Service 监听地址 | `0.0.0.0` |
| `MCP_PORT` | MCP Service 端口 | `1110` |
| `GRAPH_DATA_HOST` | GraphData Service 监听地址 | `0.0.0.0` |
| `GRAPH_DATA_PORT` | GraphData Service 端口 | `1111` |

GraphML 是 GraphData 内置子模块，无独立端口（`GRAPH_ML_HOST`/`GRAPH_ML_PORT` 已移除）。

### 19.3 服务连接

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MCP_SERVER_URL` | MCP Service URL | `http://localhost:1110/mcp` |
| `GRAPH_DATA_SERVER_URL` | GraphData Service URL | `http://localhost:1111` |

### 19.4 功能开关

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_TOOL_PROTOCOL` | 协议模式：`text`（透明文本协议）或 `native`（原生 FC） | `text` |
| `MCP_ENABLE_GRAPH_ML_TOOLS` | 强制启用/禁用 ML 工具（`1/true/yes/on` 或 `0/false/no/off`） | 自动探测 |
| `API_UPLOAD_DIR` | 上传文件目录 | `uploads` |
| `GRAPH_DATA_UPLOAD_DIR` | 图快照存储目录（服务重启后自动恢复） | `uploads/graph_data` |
| `GRAPH_DATASET_DIR` | 数据集存储目录 | `datasets` |
| `GRAPH_ML_ARTIFACT_ROOT` | ML 模型产物根目录 | `artifacts/graph_ml` |
| `IGRAPH_LOGS_DIR` | 日志与会话历史根目录 | `logs` |

---

## 21. 部署模式

### 20.1 单机开发模式（推荐）

```bash
python agent_server.py   # 或 python -m backend.api_gateway
```

API Gateway 自动托管 GraphData + MCP 子进程。GraphML 随 GraphData 自动可用（需安装 torch/torch-geometric 才能使用 train/infer）。

访问 `http://localhost:1109` 即可使用 Web UI。

### 20.2 CLI 模式

```bash
python main.py
```

不启动 HTTP 服务，直接在终端进行同步对话。自动启动 GraphData + MCP 子进程。

### 20.3 分布式部署

三个服务独立启动：

```bash
# 服务器 A（图数据 + GraphML，可部署至 GPU 节点）
GRAPH_DATA_PORT=1111 python -m graph_data_service

# 服务器 B
MCP_PORT=1110 GRAPH_DATA_SERVER_URL=http://A:1111 python -m mcp_service streamable-http

# 服务器 C（API Gateway，不托管子进程）
MCP_SERVER_URL=http://B:1110/mcp GRAPH_DATA_SERVER_URL=http://A:1111 python -m backend.api_gateway
```

### 20.4 Claude Desktop 集成

```json
{
  "mcpServers": {
    "graph-agent": {
      "command": "python",
      "args": ["-m", "mcp_service"],
      "env": {
        "GRAPH_DATA_SERVER_URL": "http://localhost:1111"
      }
    }
  }
}
```

需要单独启动 GraphData Service（以及可选的 GraphML Service）。MCP Server 以 stdio 传输运行。

---

## 22. 安全与边界处理

### 21.1 macOS 代理兼容

macOS 系统代理会拦截 localhost 请求。系统集中在启动入口处调用 `setup_no_proxy()`（`backend/runtime/proxy_compat.py`），确保 `localhost` 和 `127.0.0.1` 不经过系统代理：

```python
# 在各入口的 main() 开头调用一次
from backend.runtime.proxy_compat import setup_no_proxy
setup_no_proxy()
```

入口：`backend/api_gateway/__main__.py`、`mcp_service/__main__.py`、`main.py`

### 21.2 文件上传安全

- 文件名清洗: `re.sub(r"[^A-Za-z0-9._-]", "_", base)` 移除特殊字符
- 文件名前缀: `{session_id}_{8hex}_{sanitized}` 确保唯一性
- 路径限制: LLM 看到的只有原始文件名，不包含服务器内部路径
- 消息注入: `[附件文件]` 块明确指示「回答中不得输出服务器内部路径」

### 21.3 Memory 写入安全（新增）

`safety.py` 的 `scan_memory_content()` 在以下三个层面拦截注入：

- **Memory upsert**：`key / value / summary` 全部扫描
- **Playbook upsert**：`problem_type / approach / trigger_terms / tool_sequence` 全部扫描
- **Skill 加载**：所有 `SKILL.md` body 在 `SkillRegistry` 注册时扫描

命中 `INJECTION_PATTERNS`（含 "ignore previous instructions"、不可见 Unicode、curl env 泄露等）时写 `memory_reject` 事件，**不进入快照或 skill 注册**。

Supporting file 路径保护（`guard.py`）：阻止 skill_view 的 file_path 参数穿越到 skill 目录之外（防御 `../../etc/passwd`、SKILL.md 自引用）。

### 21.3 工具调用安全

- `_MAX_STEPS = 16`: 防止 LLM 无限循环调用工具
- 超出步数限制: 强制发送 user 消息要求只输出 `<answer>`
- 工具不存在: 返回 `"错误：未知工具 {name}"` 而非抛异常
- 断线重连: MCP 连接断开时自动重连并重试一次（仅限 `ClosedResourceError`）

### 21.4 协议容错

- 无标签 → 自动包裹为 `<answer>`
- 多标签 → 仅保留首个
- 标签未闭合 → 提取已有内容 + 记录 parse_error
- `<tool_calls>`（旧协议） → 明确报错提示改用 `<tool_call>`
- 空 `call_id` → 清洗掉避免上游 API 报错

### 21.5 图版本一致性

GraphML 训练和推理都支持 `graph_version` 校验：
- 训练时记录当前图版本到 meta.json
- 推理时默认要求版本一致，不一致时报错
- 可通过 `infer_config.allow_version_mismatch=true` 强制推理

---

## 23. 关键设计决策与取舍

### 22.1 为什么使用文本双标签协议而非原生 function calling？

**决策**: 所有 LLM 都通过 `<think>+<tool_call>` 或 `<think>+<answer>` 双标签文本格式进行工具调用。

**原因**:
- OpenAI / Anthropic / Qwen 的 function calling 格式各不相同
- 文本协议统一了接口，只需一套 system prompt 和解析逻辑
- 便于调试：LLM 的原始输出直接可读
- 便于约束：可以在 system prompt 中精确控制输出格式
- 双标签格式（`<think>` 必须存在）让 LLM 的推理过程始终可见，同时避免了单标签时需要整轮仅输出一个标签的严格约束，格式更接近自然的思考-行动模式

**代价**:
- 需要维护 `ProtocolStreamParser` 和 `normalize_model_output` 两层容错
- LLM 偶尔不遵守格式（通过归一化层自动补齐双标签）

### 22.2 为什么图数据存储在独立的 GraphData Service 中？

**决策**: 图数据不存储在 Agent 或 MCP 进程中，而是在 GraphData Service 中按 session 隔离。

**原因**:
- MCP Server 可能以 stdio 模式运行（如 Claude Desktop），进程生命周期不可控
- 图数据需要在多种访问方式间共享（Agent 工具调用 + GraphML 训练/推理）
- 独立服务便于独立扩展和监控

**代价**:
- 每次图算法计算需要一次 HTTP 调用获取图文档（通过版本化缓存缓解）
- 增加了系统复杂度（多进程管理）

### 22.3 为什么图算法在 GraphData Service 内执行？

**决策**: `GraphCompute` 计算层已迁移到 GraphData Service 内部，MCP 是纯转发代理。

**原因**:
- GraphData Service 直接持有 `GraphData` 对象，就地计算避免跨进程传输整个图文档
- `GraphCompute` 的版本化 NetworkX 缓存在同一进程内效率最高
- MCP 只负责工具语义暴露和会话路由，职责单一
- 便于未来将 GraphData Service（含计算层）部署到 GPU 节点

### 22.4 为什么所有 LLM 都通过 OpenAI SDK 调用？

**决策**: 即使是 Anthropic 和 Qwen 模型，也通过 `openai.AsyncOpenAI` 的 OpenAI-compatible 端点调用。

**原因**:
- 只需维护一套 SDK 调用代码
- Anthropic 和 Qwen 都提供 OpenAI 兼容接口
- 统一的消息格式和错误处理

**代价**:
- Claude 不支持 assistant prefill，需要额外的 user 续写指令处理
- 无法使用各 provider 的高级专有特性

### 22.5 为什么 Agent 不再维护独立后台事件循环？

**决策**: 重构后 iGraph 不再维护 `_sync_loop` / daemon 线程，直接运行于 uvicorn 事件循环。

**原因**:
- uvicorn 使用单一事件循环，Agent 实例与 session 1:1 绑定，不存在跨 loop 问题
- 消除了跨线程 `queue.Queue` 桥接的复杂度
- MCP 连接断开时通过 `_reconnect_mcp()` 自动重连，无需 loop 持久化
- CLI 模式通过 `asyncio.run()` 独立创建 loop，与 Web 模式互不干扰

**代价**:
- CLI 同步接口 `chat()` 需要 `asyncio.run()` 包装，不能与已运行的 loop 共存（但 CLI 模式下不存在此问题）

### 22.6 为什么 GraphML Service 是可选的？

**决策**: GraphML Service 不是系统必须组件，MCP 工具在 ML 服务不可用时自动隐藏。

**原因**:
- PyTorch + PyG 依赖较重，不是所有用户都需要 ML 功能
- 基础图分析（最短路径、社区检测等）不需要 GPU
- 独立部署便于在 GPU 机器上单独运行

### 22.7 为什么彻底删除 embedding.py，改用 LLM sub-agent 选工具？

**决策**: 删除 `backend/agent/embedding.py`（`ToolSelector` + `EmbeddingBackend` + `SemanticMemoryIndex`），用 `LLMToolSelector`（LLM sub-agent）替代语义向量过滤，用全量 frozen snapshot 替代语义记忆召回。

**原因**:
- sentence-transformers + torch 是重量级依赖，冷启动慢（模型下载 + 首次推理），在纯图分析场景代价高
- 向量相似度不感知图状态（节点数/有向性/权重）和用户意图，漏选率高
- 决策不可解释（为什么选了这个工具？），审计困难
- Memory 场景下，当前 KB 级记忆库全量加载（~16k chars）比语义召回更直接可靠，召回误差风险 > 全载 token 成本
- LLM sub-agent 选工具可以用上下文（图状态 + 用户意图）做决策，precision/recall 都更好

**代价**:
- 每 turn 增加一次小模型调用（预算 5s / $0.001 量级），通过 per-turn 缓存（`set_turn_id`）控制调用次数
- LLM 解析失败时需要 fallback（keyword matching），不如向量检索稳定

### 22.8 为什么 Memory 改为 Frozen Snapshot 而非每轮 recall？

**决策**: 会话开始时 `load_and_freeze()` 一次性注入全量记忆快照，整 session system prompt 不再变化。

**原因**:
- 每轮动态 recall → system prompt 每轮变化 → prefix cache 完全失效 → 每轮全量 prefill（成本高、延迟高）
- 当前记忆规模（KB 级）远低于字符预算上限（8k + 4k + 1.5k），全量加载没有信息损失
- Frozen snapshot 使 [1]～[3] 在整 session 内稳定，prefix cache 可在多 turn 内高复用
- 写分离设计：sub-agent 写入 → 内存+磁盘；session prompt → 始终读冻结副本

**代价**:
- 本 session 内写入的记忆在下次 session 才生效（可接受：记忆本就是跨 session 的）
- memory_read virtual tool 读到的是实时磁盘数据，与 frozen snapshot 可能存在轻微不一致（设计上可接受）

---

## 24. 稳定性与可维护性设计

1. **工具单点维护**：`tool_catalog.py` 是工具规格的唯一来源，MCP 注册、system prompt、native FC 参数均从此派生，新增工具只需改一个文件。
2. **双模式协议**：`LLM_TOOL_PROTOCOL=text|native` 切换，前端接口不变，满足不同场景（数据采集 vs 纯推理），代价是需维护两套 driver。
3. **StatefulStreamParser 状态化**：合并流式解析与结果提取，消除二次 parse；内建容错覆盖常见 LLM 格式违规。
4. **Agent 惰性创建**：`activate_session` 只读 meta，不创建 Agent；Agent 在首次 `chat_stream` 时才创建；内存和 MCP 连接只在实际对话时分配，避免切换历史会话时意外分配资源。
5. **ProcessSupervisor**：统一托管策略，避免 CLI / API Gateway 两个入口重复实现进程管理逻辑。
6. **各服务均提供 `create_app(...)` 工厂**：便于单测注入，不依赖全局状态。
7. **Frozen Memory Snapshot**：system prompt 中的 [1]～[3] 在 session 内不变，prefix cache 可以在多 turn 之间稳定命中；[4][5] 动态更新但有 LRU 缓存加速。
8. **Virtual tools 不走 MCP**：`skills_list` / `skill_view` / `skill_manage` / `memory_read` / `memory_delete` / `tool_expand` 在 iGraph 层截获，不污染图工具目录，两种协议（text/native）下均可用。
9. **技能文件即配置**：新增 Skill 只需 `mkdir + SKILL.md`，无需改 Python；frontmatter 校验失败只记录日志，不崩溃。

---

## 25. 已知边界与待完善项

1. **工具调用串行**：同轮多工具调用按 `tool_calls` 数组顺序串行执行，尚不支持并行。
2. **图快照跨进程不恢复**：图数据支持磁盘快照（服务重启后自动恢复），但 Agent 重启后 `_has_graph` 和 `_graph_node_count` 等本地缓存需等到首次 `get_graph_info` 调用后才同步。
3. **GraphML 依赖为可选**：PyTorch + PyG 较重，未安装时 train/infer 工具不暴露。基础图分析不需要 GPU / PyTorch。
4. **文本协议依赖模型标签遵从性**：`StatefulStreamParser` 内建容错，但极端情况下仍可能解析失败并触发重试（最多 `_MAX_PROTOCOL_RETRIES` 次）。
5. **native FC 不适合数据采集**：native 模式工具描述在 API tools 参数中而非 system prompt，对话日志不包含工具 prompt 结构，不适合直接用于 SFT/RL 数据构造。
6. **TextExtractor 建图**：`TextGraphExtractor` sub-agent（`text_graph_extractor.py`）已集成 LLM 辅助三元组抽取，支持长文本分块、去重与置信度过滤；但 `build_graph_from_text` 直接调用路径与 LLM 辅助路径的选择逻辑仍需完善。
7. **Neo4j 后端为骨架**：`storage/neo4j_backend.py` 尚未接入 `GraphSession`，图数据仍为内存态。
8. **Memory 单作用域**：当前 `MemoryScope` 仍硬编码为 `default_user`，多用户/多工作区的作用域隔离是计划中的后续迭代（见 `plans/hermes_skills_memory_plan_opus.md` P3.3）。
9. **记忆仅在下次会话生效**：Frozen Snapshot 模式下，本会话写入的记忆下次启动才生效；当前会话内无法反映最新写入。
10. **LLM Selector 延迟**：每 turn 新增一次小模型调用（约 300-500ms），通过 per-turn 缓存（`set_turn_id()`）控制总量；若延迟不可接受，可改为关键词匹配模式（无 LLM）。

---

## 附录 A：文件索引

```
iGraph/
├── main.py                              # CLI 入口
├── agent_server.py                      # Web 入口（委托 api_gateway.__main__）
├── index.html                           # 前端单页应用（含分析计划面板）
├── backend/
│   ├── agent/
│   │   ├── igraph.py               # Agent 核心（Plan-aware 推理 + virtual tool 截获）
│   │   ├── protocol.py                  # parse/normalize 工具函数
│   │   ├── model_registry.py            # 模型名称注册表（模型列表/归一化/provider 映射）
│   │   ├── provider_gateway.py          # LLM SDK 网关（含辅助 LLM 参数重载）
│   │   ├── context/                     # 上下文管理包
│   │   │   ├── context_manager.py       # 对话历史 + System Prompt + stream_turn 代理；build_model_messages 支持 working_messages 参数
│   │   │   ├── protocol_driver.py       # ProtocolDriver ABC + 工厂函数
│   │   │   ├── text_protocol_driver.py  # 文本协议驱动（StatefulStreamParser）
│   │   │   ├── native_fc_driver.py      # 原生 Function Calling 驱动
│   │   │   └── token_counter.py         # token 计数（tiktoken + CJK fallback）+ context_usage()
│   │   ├── sub_agents/                  # LLM 子代理包（辅助 LLM 调用统一入口）
│   │   │   ├── base.py                  # SubAgent 基类（ProviderGateway 注入 + invoke_text/stream_text）
│   │   │   ├── memory.py                # MemoryUpdateSubAgent（记忆写入决策）
│   │   │   ├── planner.py               # Planner（结构化分析计划，依赖 LLMToolSelector）
│   │   │   ├── query_analyzer.py        # QueryAnalyzer（查询复杂度判断）
│   │   │   ├── skill_generator.py       # SkillGenerator（从对话生成 SKILL.md 候选）
│   │   │   ├── tool_selector.py         # LLMToolSelector（per-turn 缓存 + keyword fallback）
│   │   │   ├── context_compressor.py    # ContextCompressor（旧上下文 → 中文摘要；120K 字符上限）
│   │   │   ├── plan_solver.py           # PlanSolver（逐步执行分析计划；每步隔离上下文）
│   │   │   ├── playbook_dedup.py        # PlaybookDedup（语义重复 playbook 检测 + 合并/归档方案）
│   │   │   └── text_graph_extractor.py  # TextGraphExtractor（文本分块三元组抽取；LLM 辅助）
│   │   ├── memory/                      # 记忆包（详见 SKILLS_MEMORY.md）
│   │   │   ├── memory_engine.py         # MemoryEngine：frozen snapshot + MemoryUpdateSubAgent 写入 + PlaybookReviewer
│   │   │   ├── manager.py               # MemoryManager（编排 builtin + extra provider）
│   │   │   ├── provider.py              # MemoryProvider ABC
│   │   │   ├── builtin_provider.py      # BuiltinMemoryProvider（委托 MemoryEngine）
│   │   │   ├── scope.py                 # MemoryScope 数据类
│   │   │   ├── paths.py                 # MemoryPathResolver（~/.igraph/memory/ scope 层次）
│   │   │   ├── user_profile.py          # UserProfile 数据类 + 优先级截断渲染
│   │   │   ├── tools.py                 # memory_read / memory_delete virtual tool handler
│   │   │   ├── safety.py                # INJECTION_PATTERNS + scan_memory_content
│   │   │   ├── store.py                 # atomic_json_write + append_jsonl（flock）
│   │   │   └── service.py               # MemoryService（REST governance，供 /api/memory/* 端点）
│   │   ├── skills/                      # 技能包（详见 SKILLS_MEMORY.md）
│   │   │   ├── models.py                # SkillSpec / SkillCondition 数据类
│   │   │   ├── registry.py              # SkillRegistry（扫描 bundled/user/review_queue + frontmatter）
│   │   │   ├── skill_loader.py          # build_skills_system_prompt()（compact index）
│   │   │   ├── tools.py                 # skills_list / skill_view / skill_manage virtual tool handler
│   │   │   ├── guard.py                 # injection scan + supporting file 路径保护
│   │   │   ├── config_resolver.py       # ~/.igraph/config.yaml + ${key} 替换
│   │   │   ├── paths.py                 # bundled_skills_dir / user_skills_dir / review_queue_dir
│   │   │   ├── manager.py               # SkillManager（create/write_file/remove_file/approve/reject）
│   │   │   ├── playbook_reviewer.py     # PlaybookReviewer（validated playbook → review_queue skill）
│   │   │   ├── data_ingestion_table/    # 表格上传加载流程
│   │   │   ├── data_ingestion_text/     # 文本三元组抽取建图流程
│   │   │   ├── dataset_management/      # 数据集保存/加载/版本流程
│   │   │   ├── experiment_reporting/    # 分析结果报告流程
│   │   │   ├── graph_analysis_centrality/ # 中心性分析流程
│   │   │   ├── graph_analysis_community/  # 社区检测流程
│   │   │   ├── graph_ml_inference/      # GraphML 推理流程
│   │   │   ├── graph_ml_training/       # GraphML 训练流程
│   │   │   ├── graph_path_connectivity/ # 路径与连通性分析流程
│   │   │   ├── graph_rag/               # 文档检索与 RAG 流程
│   │   │   └── graph_state_check/       # 图加载状态检查流程
│   │   └── tools/                       # 工具常量包
│   │       └── core_set.py              # CORE_TOOL_NAMES（5 项，永不进入 selector 裁剪）
│   ├── api_gateway/
│   │   ├── app.py                       # API Gateway 服务 + 路由 + SFT 导出
│   │   └── __main__.py                  # 启动入口
│   ├── logging/
│   │   ├── logger.py                    # Logger + SessionLogger（含 set_graph_meta）
│   │   ├── session_store.py             # SessionStore（含 Plan / Report 持久化）
│   │   └── sft_exporter.py              # SFT 数据导出（Alpaca / ShareGPT）
│   └── runtime/
│       └── process_supervisor.py        # 子进程管理器
├── graph_data_service/
│   ├── server.py                        # Uvicorn 入口
│   ├── app.py                           # FastAPI app + GraphDataService + 统一工具端点
│   ├── tool_catalog.py                  # 工具规格唯一真相源（28+ 图工具 + 5 ML 工具）
│   ├── algorithm_catalog.py             # 算法知识层（AlgorithmMeta + TASK_TYPE_MAP）
│   ├── compute.py                       # GraphCompute（50+ 算法，含版本化 NetworkX 缓存）
│   ├── graph_data.py                    # GraphData 核心数据容器
│   ├── session.py                       # GraphSession（图状态 + 快照 + undo + 数据集绑定）
│   ├── dataset_registry.py              # 数据集注册中心（CRUD + 版本管理）
│   ├── rag.py                           # 轻量 RAG（文档分块 + numpy 向量检索）
│   ├── job_manager.py                   # 异步作业管理器
│   ├── codec.py                         # JSON 编解码
│   ├── adapters.py                      # NetworkX 适配
│   ├── io.py                            # .npz / .json 持久化
│   ├── data_pipeline/                   # 数据建图管线
│   │   ├── graph_builder.py             # 边列表/三元组 → GraphData
│   │   └── text_extractor.py            # 文本三元组抽取
│   ├── storage/                         # 图后端抽象层（预留）
│   └── graph_ml/                        # GraphML 内置子模块
│       ├── module.py                    # GraphMLModule（集成入口）
│       ├── config_catalog.py            # 训练/推理参数规格
│       ├── schemas.py                   # Pydantic schema
│       ├── artifact_store.py            # 模型产物存储
│       ├── models/                      # GNN Backbone（GCN/GAT/SAGE）
│       ├── tasks/                       # 任务数据准备
│       ├── trainer/                     # 训练器
│       └── infer/                       # 推理器
├── mcp_service/
│   ├── server.py                        # FastMCP 工具注册（动态注册，转发到 GraphData）
│   ├── config.py                        # MCPConfig
│   ├── session_registry.py              # MCP session 映射
│   ├── graph_data_client.py             # GraphData HTTP 客户端（含 call_tool）
│   └── raw_client.py                    # RawMCPClient（Agent 侧）
├── tests/                               # 测试
│   ├── servers/                         # 服务级集成测试
│   └── test_*.py                        # 单元测试
├── plans/                               # 迭代计划与 review 文档
├── docs/                                # 项目文档
└── logs/                                # 运行时日志（自动创建）
```

## 附录 B：数据流总览

```
用户输入 "A到F的最短路径"
    │
    ▼
[API Gateway] POST /api/sessions/{sid}/chat/stream
    │  message = "A到F的最短路径"
    │
    ▼
[iGraph] chat_stream(message)
    │
    ├─ _ensure_connected() → MCP handshake + register_session
    │
    ├─ QueryAnalyzer.should_plan() → (False, "direct analysis is enough")
    │    ← 简单查询，走 Direct（ReAct）模式
    │
    ├─ _run_react_loop()
    │   ├─ _select_tools_for_turn() → LLMToolSelector.select(k=8) + CORE_TOOL_NAMES
    │   │
    │   ├─ context_manager.build_model_messages()
    │   │   → [role_prompt, mode_rule, memory_snapshot, selected_tools_schema, skills_index, ...history, user_msg]
    │   │   （[3] memory frozen snapshot 整 session 不变；[4] LLMToolSelector 入选集）
    │   │
    │   ├─ ProtocolDriver.stream_turn() → StatefulStreamParser 流式解析
    │   │   → "<think>需要计算最短路径</think><tool_call>{...}</tool_call>"
    │   │
    │   ├─ _run_tool({"name":"shortest_path","arguments":{"source":"A","target":"F"}})
    │   │   │
    │   │   ▼
    │   │   [RawMCPClient] → POST http://localhost:1110/mcp (JSON-RPC tools/call)
    │   │       │
    │   │       ▼
    │   │   [MCP Server] shortest_path(source="A", target="F", ctx)
    │   │       │  sid = _sid(ctx) → "abc12345"
    │   │       │  _invoke_graph_tool("shortest_path", ctx, {...})
    │   │       │
    │   │       ▼
    │   │   [GraphDataClient].call_tool("abc12345", "shortest_path", {source,target,use_weight})
    │   │       │  POST http://localhost:1111/sessions/abc12345/tools/shortest_path
    │   │       │
    │   │       ▼
    │   │   [GraphData Service] dispatch_tool("abc12345", "shortest_path", arguments)
    │   │       │  _require_graph(session_id)
    │   │       │  ├─ GraphSession.get_graph() → GraphData（含快照恢复）
    │   │       │  └─ GraphCompute.resolve_graph(sid, version, gd)
    │   │       │      ├─ 缓存命中 → 返回 nx.DiGraph
    │   │       │      └─ 缓存未命中 → gd.to_networkx()
    │   │       │
    │   │       ▼
    │   │   GraphCompute.shortest_path(nx_graph, "A", "F", True)
    │   │       │  nx.shortest_path(g, "A", "F", weight="weight")
    │   │       │
    │   │       ▼
    │   │       {"success":true, "path":["A","C","D","E","F"], "total_length":9.0, ...}
    │   │
    │   ├─ yield {"type":"tool_call", "tool":"shortest_path", ...}
    │   ├─ yield {"type":"tool_result", "tool":"shortest_path"}
    │   │
    │   ├─ 追加 tool observation 到 messages
    │   │
    │   ├─ 第二轮 LLM 调用 → "<think>结果清晰</think><answer>A到F最短路径为 A→C→D→E→F，距离9.0。</answer>"
    │   │
    │   └─ yield {"type":"token", "content":"A到F最短路径为..."}
    │
    ├─ _schedule_memory_update() → MemoryManager.sync_turn()（异步后台，串行链）
    │     → MemoryEngine.maybe_update_from_turn()
    │       → profile ops → user_profile.json
    │       → 其他 ops → long_term_memory.json
    └─ yield {"type":"done", "has_graph":true}
    │
    ▼
[API Gateway] SSE → data: {"type":"token","content":"..."}
    │
    ▼
[Browser] handleSseEvent() → 实时更新 DOM
```
