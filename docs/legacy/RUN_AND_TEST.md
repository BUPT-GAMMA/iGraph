# iGraph 运行与测试指南（历史版本）

> 本文保留重构前的启动与测试命令用于行为对照。当前四模块入口和部署方式请参阅
> [`AGENTOS_FOUR_MODULE_REFACTOR.md`](../architecture/AGENTOS_FOUR_MODULE_REFACTOR.md)。本次重构按要求未在本机运行测试。

本文档覆盖：

- 依赖安装与 `.env` 配置
- 一体化启动与拆分启动
- API 联调示例
- 测试命令与常见问题排查

---

## 1. 环境准备

### 1.1 Python 版本

建议：`Python 3.10+`（当前代码已在 3.10/3.12 下验证）。

```bash
python -V
```

### 1.2 虚拟环境（推荐）

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
```

### 1.3 依赖安装

```bash
pip install fastapi uvicorn pydantic python-dotenv httpx mcp openai numpy networkx
```

若启用 GraphML 训练/推理：

```bash
pip install torch torch-geometric
```

说明：

- `mcp`：FastMCP（MCP Server 框架）。
- `openai`：统一调用 OpenAI-compatible 端点（含 Claude / Qwen 的兼容层）。
- `httpx`：异步 HTTP 客户端（上传注册、MCP 内部调用）。
- `torch-geometric`：不同系统安装方式不同，参考官方 wheel 说明。

### 1.4 `.env` 配置

在项目根目录创建 `.env`（参考 `.env.example`）：

```bash
# ── API Gateway ──────────────────────────────────────
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=1109
API_UPLOAD_DIR=uploads

# ── 各服务监听地址 ────────────────────────────────────
MCP_HOST=0.0.0.0
MCP_PORT=1110
GRAPH_DATA_HOST=0.0.0.0
GRAPH_DATA_PORT=1111

# ── 服务访问 URL ──────────────────────────────────────
MCP_SERVER_URL=http://127.0.0.1:1110/mcp
GRAPH_DATA_SERVER_URL=http://127.0.0.1:1111

# ── 协议模式（新增）──────────────────────────────────
# text（默认）：工具描述写入 system prompt，上下文透明，适合 SFT/RL 数据采集
# native：工具通过 API tools 参数传入，结构化工具调用，不适合数据采集
LLM_TOOL_PROTOCOL=text

# ── 模型超时 ─────────────────────────────────────────
LLM_TIMEOUT_SECONDS=180

# ── 模型接入 ─────────────────────────────────────────
# OpenAI-compatible / Anthropic-compatible（统一配置）
LLM_API_KEY=...
LLM_BASE_URL=...

# Qwen-compatible（可选；也可用上面的 LLM_API_KEY / LLM_BASE_URL）
QWEN_API_KEY=...
QWEN_BASE_URL=...
```

**注意**：所有服务 URL/port 变量均需显式配置，无默认值。

支持的模型白名单（见 `backend/agent/model_registry.py`）：

| 模型 ID | 类型 |
|---|---|
| `claude-opus-4-6` | Anthropic |
| `claude-sonnet-4-6` | Anthropic |
| `gpt-5-4` | OpenAI |
| `gpt-5-2` | OpenAI |
| `qwen3.6-plus` | Qwen |
| `qwen3.7-max` | Qwen |

GraphML 训练/推理随 GraphData 自动可用，无需额外配置（需安装 `torch`/`torch-geometric`）。

---

## 2. 启动方式

### 2.1 一体化启动（推荐开发）

```bash
python -m backend.api_gateway
# 或
python agent_server.py
```

`ProcessSupervisor` 自动托管/探测 GraphData、GraphML、MCP。访问：`http://localhost:1109`

### 2.2 拆分启动（推荐部署）

**Terminal A: GraphData（含 GraphML 子模块）**
```bash
python -m graph_data_service
```

**Terminal B: MCP**
```bash
python -m mcp_service streamable-http
```

**Terminal C: API Gateway**
```bash
python -m backend.api_gateway
```

说明：

- GraphML 随 GraphData 自动可用，无需单独启动。
- 若需要禁用 ML 工具暴露：设置 `MCP_ENABLE_GRAPH_ML_TOOLS=0`。

### 2.3 CLI 启动

```bash
python main.py
```

CLI 启动本地 supervisor，在控制台直接对话（同步模式）。

### 2.4 Docker Compose 启动

Docker 模式复用同一份 `.env`，无需把其中的本地 `127.0.0.1` 服务 URL 改成容器名；Compose 会仅在容器内部覆盖监听地址与服务 URL。

```bash
docker compose up --build -d
docker compose ps
```

访问：`http://localhost:21108`。停止服务：

```bash
docker compose stop
```

默认运行四个容器：Frontend、Application（Backend + Agent Runtime 同进程）、MCP、GraphData。只发布 Frontend 端口；`FRONTEND_PORT` 可覆盖默认的 `21108`。

---

## 3. 健康检查

```bash
curl -sS http://localhost:1111/health   # GraphData（{"status":"ok","sessions":N}）
curl -sS http://localhost:1109/         # API Gateway 前端
```

GraphData 工具列表端点（确认工具注册正常）：

```bash
curl -sS "http://localhost:1111/tools" | python -m json.tool | head -30
curl -sS "http://localhost:1111/tools?include_graph_ml=true" | python -m json.tool
```

---

## 4. API 联调示例

### 4.1 列出历史会话

```bash
curl -sS http://localhost:1109/api/sessions
```

返回：`sessions` 数组，按 `updated_at` 倒序，每项含 `session_id`、`title`、`last_message_preview`、`model`、`active`。

### 4.2 新建会话

```bash
curl -sS -X POST http://localhost:1109/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5-2","reasoning_mode":"direct"}'
```

返回：`session_id`、`model`、`reasoning_mode`。

### 4.3 激活历史会话

```bash
curl -sS -X POST http://localhost:1109/api/sessions/<session_id>/activate
```

返回：`restored`（是否需要惰性恢复 Agent）、`requires_graph_reload`（是否曾有图）、`messages`（可见聊天历史）。

**注意**：此操作不创建 Agent，不影响其他会话，可安全频繁调用。

### 4.4 流式聊天（SSE）

```bash
curl -N -X POST http://localhost:1109/api/sessions/<session_id>/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"A到C最短路径是什么？"}'
```

SSE 事件（`data:` 行，JSON 格式）：

| 事件类型 | 含义 |
|---|---|
| `thinking_start` | 推理开始 |
| `thinking` | 推理文本增量（`content` 字段） |
| `thinking_end` | 推理结束 |
| `tool_call` | 工具调用（`tool`、`args`、`call_id` 字段） |
| `tool_result` | 工具完成（`tool`、`call_id` 字段） |
| `token` | 回答文本增量（`content` 字段） |
| `plan_ready` | 分析计划生成完毕（`plan_id`、`steps` 字段） |
| `step_start` | 计划步骤开始执行（`step_id`、`goal` 字段） |
| `step_done` | 计划步骤执行完毕（`step_id`、`status`、`summary` 字段） |
| `plan_complete` | 全部步骤完成（`plan_id`、`answer` 字段） |
| `context_usage` | token 使用量快照（`current_tokens`、`token_limit`、`percent`、`status`、`compression_count`） |
| `context_compressing` | 即将压缩旧上下文（`usage`、`threshold` 字段） |
| `context_compressed` | 上下文压缩完成（`before`、`after`、`stats` 字段） |
| `await_user_feedback` | 等待用户确认高风险工具调用（`request` 字段） |
| `done` | 本轮结束（`has_graph`、`context_usage` 字段） |
| `error` | 错误（`error` 字段） |

### 4.5 同步聊天

```bash
curl -sS -X POST http://localhost:1109/api/sessions/<session_id>/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"请加载以下图:\nA B 1\nB C 2\nA C 3"}'
```

返回：`response`（完整回答文本）、`has_graph`。

### 4.6 上传并加载图文件

```bash
curl -sS -X POST http://localhost:1109/api/sessions/<session_id>/upload \
  -F 'file=@/path/to/graph.json'
```

上传成功后，Gateway 在消息中注入文件名提示，模型会调用 `load_graph_from_upload(filename=...)`。

支持格式：`.txt`、`.json`（`graph_data.v1` 或文本边列表均可）、`.npz`、`.csv`、`.edgelist`、`.xlsx`、`.pdf`

### 4.7 切换模型

```bash
curl -sS -X PATCH http://localhost:1109/api/sessions/<session_id>/model \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6"}'
```

### 4.8 切换规划模式

```bash
curl -sS -X PATCH http://localhost:1109/api/sessions/<session_id>/planning-mode \
  -H 'Content-Type: application/json' \
  -d '{"planning_mode":"never"}'
```

可选值：`never`（始终直接 ReAct）/ `auto`（默认，自动判断）/ `always`（始终生成分析计划）。

### 4.9 数据集操作

```bash
# 列出所有数据集
curl -sS http://localhost:1109/api/datasets

# 从数据集加载图到当前会话
curl -sS -X POST http://localhost:1109/api/sessions/<session_id>/load-dataset \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"<dataset_id>","version":1}'

# 将当前图保存为数据集
curl -sS -X POST http://localhost:1109/api/sessions/<session_id>/save-dataset \
  -H 'Content-Type: application/json' \
  -d '{"name":"my_graph","description":"测试图"}'
```

### 4.10 删除会话

```bash
curl -sS -X DELETE http://localhost:1109/api/sessions/<session_id>
```

后端写入 `deleted=true`，之后 `GET /api/sessions` 不再返回该会话。

---

## 5. GraphData 直连调试

```bash
# 创建图会话
curl -sS -X POST http://localhost:1111/sessions/demo

# 文本加载
curl -sS -X POST http://localhost:1111/sessions/demo/graph/load_text \
  -H 'Content-Type: application/json' \
  -d '{"edge_text":"A B 1\nB C 2"}'

# 图信息
curl -sS http://localhost:1111/sessions/demo/graph/info

# 工具执行（与 MCP 调用路径一致）
curl -sS -X POST http://localhost:1111/sessions/demo/tools/shortest_path \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"source":"A","target":"B"}}'

# 工具规格列表（tool_catalog）
curl -sS http://localhost:1111/tools
```

---

## 6. GraphML 直连调试

GraphML 现在是 GraphData 的内置子模块，所有调用通过 GraphData 统一工具端点进行：

```bash
# 训练
curl -sS -X POST http://localhost:1111/sessions/demo/tools/train \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"task":"node_classification","model":"GCN"}}'

# 推理
curl -sS -X POST http://localhost:1111/sessions/demo/tools/infer \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"model_id":"<上一步返回的model_id>"}}'
```

**注意**：调用 train/infer 前需确保会话已加载图数据（先执行第 5 节的文本加载步骤）。

---

## 7. 测试命令

### 7.1 全量单元测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

当前测试文件：

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_protocol.py` | 原协议解析器（保留兼容性） |
| `tests/test_stateful_stream_parser.py` | StatefulStreamParser（含容错路径） |
| `tests/test_context_manager.py` | ContextManager 消息组装 + compact skill index 注入 |
| `tests/test_provider_gateway.py` | ProviderGateway 消息格式 |
| `tests/test_memory_engine.py` | MemoryEngine：frozen snapshot、注入拒绝、playbook 写入、路径迁移 |
| `tests/test_planner.py` | Planner 计划生成/解析 + LLMToolSelector fallback |
| `tests/test_skills_guard.py` | skills guard：路径穿越、SKILL.md 自引用、injection scan |
| `tests/test_memory_tools.py` | memory virtual tools：memory_read / memory_delete 集成测试 |
| `tests/test_tool_selector.py` | LLMToolSelector：per-turn 缓存、fallback、JSON 解析容错 |
| `tests/servers/test_api_gateway_app.py` | Gateway API |
| `tests/servers/test_graph_data_app.py` | GraphData Service（含新增算法/数据集/RAG 测试） |
| `tests/servers/test_graph_data_json_load.py` | GraphData JSON 文件加载（v1/fallback） |
| `tests/servers/test_graph_ml_config_catalog.py` | GraphML 配置规格 |
| `tests/servers/test_mcp_modules.py` | MCP 动态注册 |
| `tests/servers/test_process_supervisor.py` | ProcessSupervisor |

### 7.2 Agent/协议层测试

```bash
python -m unittest \
  tests.test_stateful_stream_parser \
  tests.test_context_manager \
  tests.test_provider_gateway \
  tests.test_protocol \
  tests.test_memory_engine \
  tests.test_planner \
  tests.test_skills_guard \
  tests.test_memory_tools \
  tests.test_tool_selector \
  -v
```

### 7.3 快速语法检查

```bash
python -m py_compile \
  backend/agent/igraph.py \
  backend/agent/context/protocol_driver.py \
  backend/agent/context/text_protocol_driver.py \
  backend/agent/context/native_fc_driver.py \
  backend/agent/context/context_manager.py \
  backend/agent/provider_gateway.py \
  backend/agent/model_registry.py \
  backend/agent/memory/memory_engine.py \
  backend/agent/memory/manager.py \
  backend/agent/memory/builtin_provider.py \
  backend/agent/memory/tools.py \
  backend/agent/skills/registry.py \
  backend/agent/skills/guard.py \
  backend/agent/skills/config_resolver.py \
  backend/agent/sub_agents/tool_selector.py \
  backend/api_gateway/app.py \
  graph_data_service/app.py \
  graph_data_service/tool_catalog.py \
  mcp_service/server.py \
  && echo "所有文件语法正确"
```

---

## 8. 常见问题

### 8.1 `.json` 文件上传后加载失败

现象：返回 `JSON 缺少必填字段: num_nodes` 等错误。

说明：

- 当前支持两种 JSON 格式：`graph_data.v1`（含 `schema`/`num_nodes`/`edge_index` 字段）和普通文本边列表。
- `.json` 文件会先尝试 `graph_data.v1` 格式，失败后自动 fallback 到文本边列表解析。
- 若两种格式均失败，会返回解析错误。

### 8.2 MCP 工具调用报 `kwargs Field required`

现象：`1 validation error for XXXArguments kwargs Field required`

说明：

- 这是旧版动态注册的 bug（使用 `**kwargs` 导致 FastMCP 生成错误 schema）。
- 已在重构版本中修复，MCP Server 现在用 `inspect.Signature` 生成真实参数名的函数签名。
- 确认运行的是最新代码版本。

### 8.3 GraphML 工具不出现

检查：

1. `MCP_ENABLE_GRAPH_ML_TOOLS` 是否被显式设为 `0/false`。
2. GraphData 中 GraphML 工具是否已注册：`curl "http://localhost:1111/tools?include_graph_ml=true" | python -m json.tool | grep train`
3. 确认 `torch` / `torch-geometric` 已安装（GraphML 工具不依赖 PyTorch 注册，但调用 train/infer 需要）。

### 8.4 切换会话时其他会话被销毁

现象：日志出现 `[session_id] 会话销毁`。

说明：

- 已在重构版本中修复。`activate_session` 现在只读 meta，不创建 Agent，不触发淘汰。
- 会话销毁只在以下情况触发：
  - `DELETE /api/sessions/{id}`（用户显式删除）
  - `create_session` 时若内存中 Agent 超过 5 个（LRU 淘汰最老的）

### 8.5 历史会话列表顺序一直变

说明：

- 已修复。当前按 `updated_at` 倒序，只在真正发消息或修改模型时更新。
- 单纯切换会话不更新 `updated_at`，列表顺序稳定。

### 8.6 协议模式切换（LLM_TOOL_PROTOCOL）

- `text`（默认）：工具描述在 system prompt 中，所有 LLM 均可用，适合数据采集。
- `native`：工具通过 API `tools` 参数传入，需要模型原生支持 function calling。
- 切换后需重启服务生效（环境变量在启动时读取）。
