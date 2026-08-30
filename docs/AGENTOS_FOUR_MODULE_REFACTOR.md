# AgentOS 四模块重构说明

## 决策

项目一级业务模块固定为：

1. Frontend；
2. Backend；
3. Agent Runtime；
4. Graph Engine。

模块数不等于进程数。Graph Engine 具有 HTTP 和 MCP 两个部署进程，PostgreSQL 是
Agent Runtime 的基础设施，不是第五个业务模块。

## 依赖方向

允许：

```text
Frontend -> Backend
Backend -> Agent Runtime
Backend -> Graph Engine HTTP
Agent Runtime -> Graph Engine MCP
Graph Engine MCP -> Graph Engine HTTP/domain
```

禁止：

```text
Backend -> Agent Runtime Python implementation
Graph Engine -> Backend API/session implementation
Agent Runtime -> Backend user database
Frontend -> Agent Runtime/PostgreSQL/Graph Engine directly
```

迁移期的 `graph_data_service` 和 `mcp_service` 是 Graph Engine 的兼容实现命名空间；
新代码只能从 `graph_engine` 入口启动。

## 用户与身份

Backend 的 `UserDirectory` 是用户身份、角色、启用状态和模型权限的唯一来源。现有
`X-User-ID` 契约保留，便于前端无损迁移；后续切换 JWT 时只替换 Backend identity
边界，并让 Backend 把已验证 JWT 传给 AgentOS。

Agent Runtime 只使用 `user_id` 和已验证 claims 做数据隔离及模型授权，不保存用户名、
密码或登录状态。AgentOS UserProfile 是 Agent 学到的用户偏好，不是登录账户资料。

## Session 和断点恢复

AgentOS PostgreSQL 是 Session、Run 和事件的唯一来源。Backend 的 Session API 只是兼容
映射，不保留 `_agents` 或第二份消息日志。

会话激活时 Backend 同时读取 AgentOS Session Runs，将文本、reasoning、工具调用、
`visualize_subgraph` 图卡片、`create_analysis_report` 报告和暂停确认卡片重建为旧前端消息
结构，因此页面刷新不需要 Backend 自己保存第二份聊天/制品日志。

流式运行始终使用：

```text
stream=true
background=true
```

Frontend 保存 `run_id` 和 `event_index`。连接中断后调用：

```text
POST /api/sessions/{session_id}/runs/{run_id}/resume?last_event_index=N
```

Backend 校验 Session 所有权，再代理 AgentOS：

```text
POST /agents/igraph-agent/runs/{run_id}/resume
```

高风险 MCP 工具由 AgentOS 原生 HITL 暂停。Frontend 将 `run_id`、`tool_call_id`、
`tool_name`、原始或修改后的 `tool_args` 和确认结果交给 Backend，再由 Backend 调用
`POST /agents/igraph-agent/runs/{run_id}/continue`；Backend 不在本地复制暂停状态机。

已持久化事件可在进程重启后回放；仍在执行的 live stream 在多副本部署时需要入口粘性
或 `run_id -> instance` 路由。模型调用无法从单个 token 继续，恢复边界是持久化 Run/Event
和幂等工具调用。

## Memory 与 Learning

原数据映射如下：

| 旧概念 | Agno |
|---|---|
| user_profile | UserProfile |
| long_term | UserMemory |
| 短期摘要、目标、计划和进度 | SessionContext |
| playbook | LearnedKnowledge，namespace=user，mode=PROPOSE |
| 关键工具选择与结果 | DecisionLog |
| 文档和大块参考资料 | Knowledge |

Agent 创建时按 planning mode 构造 LearningMachine：`never` 关闭 SessionContext planning，
`auto/always` 开启规划字段。LearnedKnowledge 依赖 pgvector 和 embedding 配置。

当前 `/api/memory` 是为了保持旧前端不变而提供的 AgentOS Memories 管理视图，使用 topic
保存 `scope/category/key`。Agent 运行中自动学习的 UserProfile、UserMemory、
SessionContext、LearnedKnowledge 和 DecisionLog 则由 LearningMachine 各自的原生 store
管理；账户身份数据仍只属于 Backend，不能写入 UserProfile。

## Skills

目录：

```text
/data/agent_runtime/skills/
├─ users/{user_id}/{skill}/SKILL.md
└─ candidates/{user_id}/{skill}/SKILL.md
```

代码内置 Skill 位于 `agent_runtime/resources/skills`。只有代码内置和 `users` 会传给
Agno `LocalSkills`；`candidates` 必须审批后移动到用户活动目录。Skill API 属于 Agent
Runtime，Backend 只代理当前 `/api/skills` 契约。

从 Playbook 生成 Skill 时，Backend 可以读取当前会话的模型设置，但实际生成 Run 始终
放入一次性 AgentOS Session，完成后删除，避免内部生成提示与 SKILL.md 污染用户聊天历史。

## Graph Engine

Graph Engine 同时提供：

- Backend 使用的 HTTP API；
- Agent Runtime 使用的 MCP API。

MCP 调用携带：

```text
X-User-ID
X-Session-ID
X-Run-ID
X-Agent-ID
```

HTTP 传输不再依赖 MCP 连接级 SessionRegistry。旧 `register_session` 仍为 stdio 和旧客户
端保留。长时间 Graph Job 应继续使用 operation/job ID 幂等执行，避免 Run 恢复时重复训练
或重复写图。

Graph Engine 自动执行的旧数据迁移是 copy-only：全局数据集复制到
`data/global/datasets`，用户图快照从旧 `sessions/uploads` 存储复制到
`data/users/{user_id}/graph_sessions`。目标已存在时跳过，旧文件不删除。

## 现有功能保持映射

| 用户可见能力 | 重构后实现 | 权威数据/状态 |
|---|---|---|
| 登录 ID 校验、用户 CRUD、角色与模型白名单 | Backend `identity.py/users.py` | Backend UserDirectory |
| Session 列表、创建、激活、配置、删除 | Backend 编排 AgentOS Session 和 Graph Session | AgentOS + Graph Engine，Backend 无副本 |
| 页面刷新后恢复文本、工具、图卡片、报告 | Backend 从 AgentOS Session Runs 重建旧 UI 契约 | AgentOS Run/Event |
| 断线重连、缺失事件回放 | Frontend 保存 `run_id/event_index`，Backend 代理 `/resume` | AgentOS background Run/Event |
| 高风险工具确认/修改/拒绝 | AgentOS 原生 RunPaused + `/continue` | AgentOS Run requirements |
| Memory 查询与编辑页 | Backend 映射 AgentOS Memories + topics | AgentOS Memory table |
| 用户偏好、长期记忆、会话规划、决策日志 | Agno LearningMachine；ReasoningStep 投影到旧计划面板 | AgentOS/Agno native stores |
| Playbook/可复用经验 | LearnedKnowledge `namespace=user, mode=PROPOSE` | PostgreSQL + pgvector |
| Skill 列表、生成、候选、审批、拒绝 | Agent Runtime Skill API + LocalSkills | Agent Runtime Skills 目录 |
| 图上传、数据集、建图、算法、GraphML、报告 | Backend HTTP 代理 + Agent Runtime MCP 工具 | Graph Engine |
| 长时间建图/训练进度 | Graph Job + Agent 轮询 `get_job_status` + 事件映射 | Graph Engine JobManager |
| Knowledge 指标与 SFT 导出 | Backend 从 AgentOS Memories/Skills/Runs 动态投影 | AgentOS，不写第二份数据 |

创建 Session 是一个 Backend 编排操作：先创建 AgentOS Session，再创建 Graph
Session；Graph 初始化失败时回删新建 AgentOS Session，避免把半成品暴露给前端。

## 依赖

Backend 直接依赖：FastAPI、Uvicorn、Pydantic、HTTPX、python-multipart、python-dotenv。

旧 `/api/metrics/knowledge` 和 `/api/export/sft` 契约继续保留；实现已分别改为统计
AgentOS Memories/Skills，以及从 AgentOS Session Runs 导出 alpaca/sharegpt/raw 数据。

Agent Runtime 直接依赖：

```text
agno[os,mcp,openai,postgres,pgvector,opentelemetry]==2.9.0
psycopg[binary]
sqlalchemy
pgvector
fastapi
uvicorn
httpx
openai
```

Graph Engine MCP 增加 `fastmcp`；Graph Engine API 继续保留 NetworkX、iGraph、Leiden、
Nebula、PyTorch/PyG 和 OpenAI-compatible 文本图抽取依赖。Graph Engine 不依赖 Agno。

## 最终工程结构

```text
igraph/
├─ frontend/                         # 页面、状态投影、SSE 断点续传
│  └─ public/index.html
├─ backend/                          # 公共 BFF：账户、权限、会话编排
│  ├─ app.py                       # /api/* 兼容契约
│  ├─ users.py                     # UserDirectory
│  ├─ identity.py                  # 认证/管理员边界
│  ├─ event_mapper.py              # AgentOS SSE -> 前端事件
│  └─ service_client.py             # Runtime/Graph 服务客户端
├─ agent_runtime/                    # 独立 AgentOS 运行时
│  ├─ os_app.py                     # AgentOS 组装根
│  ├─ agno_factory.py               # 按请求创建 Agno Agent
│  ├─ model_factory.py              # 模型适配与权限约束
│  ├─ learning.py                   # LearningMachine/Memory/Knowledge
│  ├─ skills_runtime.py             # LocalSkills 加载边界
│  ├─ skill_api.py                  # Skill 候选/审批/管理
│  ├─ graph_tools.py                # Graph MCP 工具和 HITL
│  ├─ resources/skills/             # 内置 Skills
│  └─ migrations/legacy_data.py     # Session/Memory/Skill 幂等迁移
├─ graph_engine/                     # 图领域模块与统一入口
│  ├─ api.py                       # HTTP 适配器
│  ├─ mcp.py                       # MCP 适配器
│  ├─ paths.py                     # 图数据唯一路径归属
│  ├─ migration.py                 # 图快照/数据集复制迁移
│  └─ ingestion/                   # 文件判型与文本建图
├─ graph_data_service/                # 迁移期 Graph HTTP/domain 兼容命名空间
├─ mcp_service/                       # 迁移期 Graph MCP 兼容命名空间
├─ deploy/
│  ├─ docker/                      # 四模块镜像
│  └─ requirements/                # 按部署单元拆分的依赖
├─ compose.yaml                       # 四模块 + PostgreSQL/pgvector
└─ requirements.txt                   # 兼容的全量开发依赖
```

`backend/api_gateway`、`backend/agent`、`agent_runtime/agent`、`graph_data_service` 和
`mcp_service` 在当前阶段是迁移兼容层，不是新业务边界。默认入口不再从
Backend 实例化旧 Agent Runtime。Graph HTTP/MCP 的物理文件移动留到可运行
环境回归通过后完成，避免在无测试条件下一次性破坏大量历史 import。

## 重构执行与验收顺序

1. 固定四模块边界和单向依赖；已完成。
2. 建立 AgentOS 独立进程、AgentFactory、PostgreSQL 存储与 Graph MCP；已完成。
3. 将用户/角色/模型权限、Session 编排和旧 `/api/*` 投影留在 Backend；已完成。
4. 将 Memory/Learning/SessionContext/DecisionLog/LearnedKnowledge 替换为 Agno 原生机制；已完成。
5. 将 Skill 加载、用户目录、候选审批和内置 Skill 归属 Agent Runtime；已完成。
6. 将图状态、上传、数据集、长任务和制品收敛到 Graph Engine；已完成逻辑边界，物理兼容命名空间暂保留。
7. 将前端消息、图卡片、报告、HITL 和 `run_id/event_index` 恢复对接 Backend 投影；已完成。
8. 在可运行的独立环境执行数据 dry-run、契约回归、SSE 断线、HITL、图任务幂等和权限隔离验收；待执行。
9. 仅在验收通过后物理删除旧 Runtime/网关并将 Graph 兼容文件移入 `graph_engine`；待执行。

第 8 步是运行态验收，本次按要求不在当前机器执行；因此第 9 步也保持待办，
以便保留回滚路径。

## 兼容与清理顺序

当前默认入口已指向新四模块。旧实现暂时保留用于数据读取和回滚，但不得继续扩展。
完成独立环境验收后按以下顺序删除：

1. 删除 `backend/api_gateway` 旧网关；
2. 删除 `backend/agent` 转发包；
3. 删除旧 `RuntimeSession/LegacyIGraphRuntime`；
4. 删除自研 Memory、ContextManager 和 Skill runtime；
5. 将 `graph_data_service/mcp_service` 文件物理迁入 `graph_engine`，保留短期 import shim；
6. 停止使用旧 JSONL Session/Memory 数据并归档只读副本。

删除前必须在可运行环境验证当前契约测试、AgentOS API、SSE 重连、Memory/Skills 迁移和图任务
幂等性。本次按要求未在当前本机运行测试或启动服务。
