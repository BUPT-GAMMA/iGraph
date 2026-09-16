# iGraph

iGraph 采用四个一级业务模块：Frontend、Backend、Agent Runtime 和 Graph Engine。
Agent Runtime 使用开源 Agno AgentOS；Session、Run、Memory、Learning、Skills、Approval
和 Trace 不再由 Backend 的进程内对象维护。

表格数据的 PK/FK 构图、独立 LLM 子代理、跨任务领域策略记忆与查询验收使用方法见[表格构图子代理文档](docs/TABULAR_CONSTRUCTION_SUBAGENT.md)。

## 架构

```text
Frontend
   │ HTTP / SSE
   ▼
Backend ───────────────────────► Graph Engine HTTP API
   │                                  ▲
   │ AgentOS API / resumable SSE      │ Graph tools
   ▼                                  │
Agent Runtime ─────── MCP ────────────┘
   │
   ▼
PostgreSQL + pgvector
```

### Frontend

- 页面、聊天和图可视化；
- 保存当前 `session_id/run_id/event_index`；
- 网络中断后通过 Backend 调用 AgentOS `/resume`；
- 不保存权威 Session、Memory 或图数据。

### Backend

- 前端唯一公共 API；
- 用户目录、启用/禁用、角色和模型权限；
- 保留当前 `/api/*` 契约；
- 代理 AgentOS Session/Run/Memory/Skills；
- 代理 Graph Engine 数据集、上传和图接口；
- 不创建或缓存活动 Agent。

默认入口：`python -m backend`。

### Agent Runtime

- 原生 `AgentOS + AgentFactory + Agent`；
- PostgreSQL Session/Run/Event/Trace；
- `LearningMachine`：UserProfile、UserMemory、SessionContext、DecisionLog；
- LearnedKnowledge 使用用户命名空间和 PROPOSE 模式；
- `LocalSkills` 加载内置和已审批的用户 Skills；
- `MCPTools.header_provider` 向 Graph Engine 传递用户、会话和 Run 身份；
- `background=true + stream=true + /resume` 提供断点重连。

默认入口：`python -m agent_runtime`。

### Graph Engine

- 图数据、GraphML、图算法、图机器学习、数据集、上传、Job 和制品；
- 同一业务模块提供 HTTP 与 MCP 两个进程适配器；
- MCP 优先使用 `X-User-ID/X-Session-ID/X-Run-ID`，旧 `register_session` 只用于兼容。

入口：

```text
python -m graph_engine api
python -m graph_engine mcp http
```

## 目录

```text
frontend/                 Frontend 模块
backend/                  Backend/BFF 与用户管理
agent_runtime/            Agent Runtime/AgentOS
graph_engine/             Graph Engine 一级入口与基础能力
graph_data_service/       Graph Engine HTTP 实现兼容命名空间
mcp_service/              Graph Engine MCP 实现兼容命名空间
deploy/                   四模块镜像和依赖
tests/                    接口契约、构图、DAG 并行、会话与回归测试
docs/                     设计、迁移和部署文档
```

`backend/api_gateway`、`backend/agent` 和旧 `agent_runtime/agent` 仅作为迁移期兼容代码保留，
默认 Compose 和默认入口不再使用进程内 Runtime。

## 数据归属

| 数据 | 唯一所有者 |
|---|---|
| 用户、角色、模型权限 | Backend |
| Session、Run、消息、Memory、Learning、Trace | Agent Runtime / AgentOS PostgreSQL |
| 已批准和候选 Skills | Agent Runtime Skills 目录 |
| 图、数据集、上传、图任务和制品 | Graph Engine |

## 部署

复制 `.env.example` 为 `.env`，配置一个模型供应商的 API Key/Base URL，并设置
`IGRAPH_CONTROL_SERVICE_KEY`。使用下面的命令生成一次随机服务密钥，把输出填入
`.env` 对应配置项；Backend、Agent Runtime、Graph Engine API 和 MCP 使用同一个值。

```sh
python -c 'import secrets; print(secrets.token_hex(32))'
```

服务密钥是完整部署的必填项，`IGRAPH_RUN_CANCEL_ENABLED=false` 时也需要配置。
Compose 在启动前检查是否填写，各服务启动时检查非空、无空白的 ASCII 格式，
错误消息包含配置项名称和修复提示。修改已有密钥时应同步更新并重启四个服务。
独立使用 Uvicorn 时通过 `--env-file .env` 传入配置。

若需要 LearnedKnowledge 的语义检索，再配置 `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL` 和
`EMBEDDING_MODEL`；未配置 embedding 时 UserProfile、UserMemory、SessionContext 和
DecisionLog 仍启用，只有 LearnedKnowledge 关闭。然后使用：

```text
docker compose up --build -d
```

个人／项目数据集 HTTP API、Agent 工具和旧客户端的迁移约定见
[数据集接口迁移](docs/DATASET_API_MIGRATION.md)。旧 HTTP 入口目前保留兼容，
响应头和 OpenAPI 会标注弃用；Agent 数据集工具继续维护。

Compose 启动六个进程容器，但仍是四个业务模块：

```text
frontend
backend
agent-runtime
graph-engine-api
graph-engine-mcp
postgres
```

Graph Engine 的 HTTP/MCP 是同一代码模块的两个协议适配器；PostgreSQL 是基础设施。

## 依赖清单

| 部署单元 | 依赖文件 | 主要依赖 |
|---|---|---|
| Backend | `deploy/requirements/backend.txt` | FastAPI、Uvicorn、Pydantic、HTTPX、python-multipart |
| Agent Runtime | `deploy/requirements/agent-runtime.txt` | `agno[os,mcp,openai,postgres,pgvector,opentelemetry]==2.9.0`、psycopg、SQLAlchemy、pgvector |
| Agent / Graph 共用计算栈 | `deploy/requirements/compute-cpu.txt` / `compute-gpu.txt` | 默认 CPU；可选 PyTorch 2.14.0 + CUDA 13.0、PyG、NumPy、SciPy、NetworkX |
| Graph Engine HTTP | `deploy/requirements/graph-data.txt` | FastAPI、OpenAI SDK、NetworkX、iGraph、Leiden、Nebula、PyTorch/PyG |
| Graph Engine MCP | `deploy/requirements/mcp.txt` | FastMCP、MCP、HTTPX |
| Frontend | `frontend/Dockerfile` | Nginx；页面依赖 D3、Marked、KaTeX、Mermaid、Tailwind CDN |

根目录 `requirements.txt` 是一次性开发环境的依赖合集；Agent Runtime 和 Graph Engine 镜像
预装同版本的计算栈，Backend/MCP 使用各自的轻量依赖。

工作区默认 Python 可直接运行 Torch。所有 Agent session 共用镜像中的计算栈；
`workspace_env_ensure` 创建继承系统包的会话环境，仅为额外依赖安装包。与共享计算栈
版本冲突的依赖安装会失败，不支持 session 单独切换 Torch 版本。基础 Compose 使用 CPU
计算栈，适用于没有 NVIDIA 设备的本机与 CI。

GPU 主机使用 `compose.gpu.yaml` 显式启用 CUDA 计算栈与 NVIDIA CDI 设备：

```bash
docker compose -f compose.yaml -f compose.gpu.yaml build graph-engine-api agent-runtime
docker compose -f compose.yaml -f compose.gpu.yaml up -d
```

该配置需要支持 CDI（`nvidia.com/gpu=all`）的 Docker 和 NVIDIA Container Toolkit；
验证配置为 RTX 5090、驱动 580.173.02。
沙箱保留网络及文件隔离，并传入容器已获授权的 NVIDIA 设备。GPU 为服务器共享资源，
目前没有 session 级显存配额或 GPU 调度，显存不足时 Agent 应报告失败。

重启或重建容器不会下载 Torch。普通应用代码更新复用计算依赖镜像层；修改计算依赖、
基础镜像或清除构建缓存才需要重装。BuildKit 的 pip 下载缓存可用于再次构建。

## 源码热更新开发

基础镜像构建一次后，启动挂载源码的 Python 服务：

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d backend
npm --prefix frontend run dev
```

前端访问 `http://localhost:21108`，Vite 将 API 转发到本机 `21109`。
前端修改即时更新；Python 服务修改会自动重载，正在执行的任务可能中断。
若之前启动了静态前端容器，先执行 `docker compose stop frontend` 释放 `21108`。
普通代码修改无需重新构建镜像，依赖或 Dockerfile 变化后再构建对应服务。

## 旧数据迁移

迁移程序支持幂等 manifest 和 dry-run：

```text
python -m agent_runtime.migrations.legacy_data --dry-run
python -m agent_runtime.migrations.legacy_data
```

它会：

- 导入旧 Session 元数据和可见消息；
- 将旧 Memory 写入 AgentOS Memories；
- 将 Skill 名称规范化为小写连字符格式；
- 将已批准 Skill 与候选 Skill 分别迁移到 `users/` 和 `candidates/`；
- 保留旧文件，不执行删除。

Graph Engine 启动时还会将旧的用户图快照从
`data/users/{user}/sessions|uploads/{session}` 幂等复制到专属的
`data/users/{user}/graph_sessions/{session}`。只迁移包含图快照、图版本记录或
Nebula 引用的目录；已存在的目标不覆盖，旧目录不删除。

详细边界和迁移说明见 [AgentOS 四模块重构](docs/AGENTOS_FOUR_MODULE_REFACTOR.md)。
