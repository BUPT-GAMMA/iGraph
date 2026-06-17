# iGraph 部署手册

本文档聚焦部署维度：

- 服务拆分与职责
- 端口与环境变量
- 单机托管与微服务拆分两种部署策略
- GraphML 可选启用机制

---

## 1. 服务清单

| 服务 | 启动命令 | 必需 |
|---|---|---|
| API Gateway | `python -m backend.api_gateway` | 是 |
| GraphData Server | `python -m graph_data_service` | 是 |
| MCP Server | `python -m mcp_service streamable-http` | 是 |

GraphML 是 GraphData Server 的**内置子模块**，随 GraphData 自动启动，无需单独进程。

默认端口（可通过 `.env` 覆盖）：

- API: `1109`
- MCP: `1110`
- GraphData: `1111`

---

## 2. 部署模式

### 2.1 模式 A：Gateway 托管（开发/小规模）

```bash
python -m backend.api_gateway
# 或
python agent_server.py
```

特点：

- `ProcessSupervisor` 自动启动 GraphData 与 MCP。
- GraphML 随 GraphData 自动可用，无需额外配置。
- 部署简单，适合本地开发或单机演示。

### 2.2 模式 B：服务拆分（生产推荐）

分别部署 3 个进程/容器，推荐启动顺序：

1. GraphData（含 GraphML 子模块，若需要 GPU 训练则部署至 GPU 节点）
2. MCP（按 `MCP_ENABLE_GRAPH_ML_TOOLS` 决定是否注册 train/infer 工具）
3. API Gateway

优点：

- 服务故障隔离
- GraphData 可独立扩缩容
- Gateway 可以无状态扩展（会话上下文持久化在日志目录）

---

## 3. 环境变量

**说明**：所有服务 URL/port 变量均需在 `.env` 中显式设置，无默认值。

### 3.1 API Gateway

| 变量 | 说明 |
|---|---|
| `API_SERVER_HOST` | 监听地址（如 `0.0.0.0`） |
| `API_SERVER_PORT` | 监听端口（如 `1109`） |
| `API_UPLOAD_DIR` | 上传文件目录（相对或绝对路径） |
| `MCP_PORT` | MCP 端口（供 ProcessSupervisor 探测） |
| `GRAPH_DATA_PORT` | GraphData 端口（供 ProcessSupervisor 探测） |
| `MCP_SERVER_URL` | MCP 访问 URL，如 `http://127.0.0.1:1110/mcp` |
| `GRAPH_DATA_SERVER_URL` | GraphData 访问 URL |

### 3.2 GraphData

| 变量 | 说明 |
|---|---|
| `GRAPH_DATA_HOST` | 监听地址 |
| `GRAPH_DATA_PORT` | 监听端口 |
| `GRAPH_DATA_UPLOAD_DIR` | 图快照存储目录（默认 `uploads/graph_data`，服务重启后自动恢复） |
| `GRAPH_DATASET_DIR` | 数据集存储目录（默认 `datasets`） |

### 3.3 MCP

| 变量 | 说明 |
|---|---|
| `MCP_HOST` | 监听地址 |
| `MCP_PORT` | 监听端口 |
| `GRAPH_DATA_SERVER_URL` | GraphData 访问 URL |
| `MCP_ENABLE_GRAPH_ML_TOOLS` | `1/true`：强制启用 GraphML 工具；`0/false`：强制禁用；未设置：自动探测（`/tools?include_graph_ml=true`） |

### 3.4 GraphML（GraphData 内置子模块）

GraphML 随 GraphData 运行，无独立进程端口。

| 变量 | 说明 |
|---|---|
| `GRAPH_ML_ARTIFACT_ROOT` | 模型产物存储目录（默认 `artifacts/graph_ml`，建议持久化） |

### 3.5 模型调用（Agent）

所有 provider 统一走 OpenAI-compatible 端点，按模型前缀匹配：

| 变量 | 适用模型 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` | `gpt-*`、`claude-*`（必填） |
| `QWEN_API_KEY` / `QWEN_BASE_URL` | `qwen*`（可选，回退到 `LLM_API_KEY`/`LLM_BASE_URL`） |
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | `qwen*` 备用变量 |
| `LLM_TIMEOUT_SECONDS` | 请求超时（默认 180 秒） |

### 3.6 协议模式

| 变量 | 说明 |
|---|---|
| `LLM_TOOL_PROTOCOL` | `text`（默认）：透明文本协议，适合 SFT/RL 数据采集；`native`：原生 Function Calling |

- `text` 模式：工具描述写入 system prompt，LLM 输出 `<think>/<tool_call>/<answer>` 标签，上下文对训练完全透明。
- `native` 模式：工具通过 API `tools` 参数传入，结构化工具调用，不依赖文本解析。
- 切换后需重启所有服务生效。

### 3.7 短期记忆与上下文压缩（Agent）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SESSION_CONTEXT_TOKEN_LIMIT` | 单 session 上下文 token 上限 | `192000` |
| `CONTEXT_COMPRESSION_TRIGGER_PCT` | 触发压缩的使用率阈值（百分比整数） | `90`（即 90%）|
| `CONTEXT_COMPRESSION_PRESERVE_RECENT_USER_TURNS` | 压缩时保留的最近 user turn 数 | `3` |
| `CONTEXT_COMPRESSOR_MAX_TOKENS` | 压缩子代理回复 token 上限 | `8192` |

当 `current_tokens ≥ SESSION_CONTEXT_TOKEN_LIMIT × CONTEXT_COMPRESSION_TRIGGER_PCT%` 时自动触发压缩。
压缩由 `ContextCompressor` sub-agent 执行，失败时 fallback 到文本摘要。
前端通过 `context_compressing` / `context_compressed` SSE 事件感知压缩进度（详见 `SHORT_TERM_MEMORY.md`）。

### 3.8 日志与会话历史

| 变量 | 说明 |
|---|---|
| `IGRAPH_LOGS_DIR` | 日志与会话历史根目录（默认 `logs/`） |

会话历史结构：

```
{IGRAPH_LOGS_DIR}/{session_id}/
├── message_list.jsonl   # 消息记录（用于会话恢复）
├── session_meta.json    # 元数据（model/title/updated_at/deleted 等）
└── trace.jsonl          # 调试 trace（工具调用/协议解析）
```

注：删除的会话在 `session_meta.json` 中写入 `deleted: true`，日志目录不会被删除（便于审计）。

长期记忆存储：`backend/agent/memory/default_user/long_term_memory.json`

---

## 4. 健康检查

```bash
curl -sS "${GRAPH_DATA_SERVER_URL}/health"   # {"status":"ok","sessions":N}
curl -sS "http://${API_SERVER_HOST}:${API_SERVER_PORT}/" | head
```

GraphData 工具规格验证（确认 MCP 可以拉到正确的工具列表）：

```bash
curl -sS "${GRAPH_DATA_SERVER_URL}/tools" | python -m json.tool | grep '"name"'
```

---

## 5. GraphML 工具可选启用策略

GraphML 内置于 GraphData，工具是否对 LLM 暴露通过 MCP 动态注册机制控制：

1. MCP 启动时查询 `GET /tools?include_graph_ml=true`（或读取 `MCP_ENABLE_GRAPH_ML_TOOLS` 环境变量）。
2. 若 GraphML 工具可用（catalog 中包含 `GRAPH_ML_TOOLS`），从 `tool_catalog.GRAPH_ML_TOOLS` 动态注册。
3. 若 `GRAPH_ML_ARTIFACT_ROOT` 不可写或 PyTorch 未安装，GraphData 调用 train/infer 会返回友好错误，但不影响其他工具。

结果：

- `MCP_ENABLE_GRAPH_ML_TOOLS=0`：MCP 只注册图分析工具，不暴露 ML 工具。
- 未设置或 `=1`：自动从 catalog 注册 `train`/`infer`/`visualize_model_architecture` 等工具。

---

## 6. 资源与运维建议

1. 若需要 GPU 训练，将 GraphData（含 GraphML 子模块）部署至 GPU 节点，API Gateway 与 MCP 可部署在 CPU 节点。
2. `API_UPLOAD_DIR` 需可写，定期清理旧上传文件（会话删除后文件不自动清理）。
3. `GRAPH_ML_ARTIFACT_ROOT` 建议挂载持久卷，避免重启丢失模型产物（默认 `artifacts/graph_ml`）。
4. `IGRAPH_LOGS_DIR` 需持久化，保留会话历史（用于跨进程恢复对话上下文）。
5. `backend/agent/memory/default_user/long_term_memory.json` 与 `problem_solving_playbooks.json` 包含用户长期记忆，建议纳入备份策略。

---

## 7. 常见部署问题

### 7.1 MCP 启动后没有 train/infer

排查：

1. `MCP_ENABLE_GRAPH_ML_TOOLS` 是否被显式设为 `0/false`。
2. MCP 日志中查看 `Dynamic tool registration complete` 行，确认注册了多少个 graph_ml tools。
3. GraphData 工具列表是否包含 ML 工具：`curl "http://localhost:1111/tools?include_graph_ml=true" | python -m json.tool | grep train`

### 7.2 Gateway 上传成功但加载失败

排查：

1. `API_UPLOAD_DIR` 是否是绝对路径或与 Gateway 运行目录一致。
2. GraphData 进程是否能访问该路径（跨容器部署时需共享卷）。
3. 文件格式：`.json` 文件优先尝试 `graph_data.v1` 格式，失败后 fallback 到文本边列表。`.npz` 必须是标准 GraphData 格式。

### 7.3 切换协议模式后工具列表不变

说明：`LLM_TOOL_PROTOCOL` 在进程启动时读取，切换后需重启服务。切换 `native` 模式后：

- MCP Server 的工具注册不变（MCP 仍向 Agent 暴露相同的工具列表）
- iGraph 的 `NativeFCDriver` 会把工具 spec 转为 OpenAI `tools` 参数格式传入 LLM
- 工具 spec 来源不变，仍从 MCP `tools/list` 获取

### 7.4 会话恢复后图数据丢失

说明：GraphData 现已支持图快照持久化，服务重启后自动从 `GRAPH_DATA_UPLOAD_DIR/{session_id}/graph_current.npz` 恢复。

若仍遇到图丢失：

- 检查 `GRAPH_DATA_UPLOAD_DIR` 是否可写且持久化（容器部署时需挂载卷）。
- 快照文件损坏时会静默跳过（日志中可见），此时需用户重新加载图。
- `activate_session` 返回 `requires_graph_reload: true` 时，前端会提示用户重新加载。
