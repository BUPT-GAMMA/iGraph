# iGraph

当前为单用户版：打开网页即进入默认工作区，不提供登录、注册、头像或切换用户入口。
所有公开 API 请求统一归属 `IGRAPH_ADMIN_USER_ID`（默认 `admin`），忽略客户端遗留的用户选择；
部署仍应通过回环地址和 SSH 隧道访问。内部服务密钥校验保持启用。
复用已有数据卷时继续使用该工作区的数据，其他旧用户的数据不删除、不自动合并。

聊天回复跟随发送时的界面语言（中文/英文）；切换语言影响下一轮回复，不翻译历史消息。
工具分析后的回答先给一句话结论，再展开依据和建议，末尾保留总结。

示例卡片、推荐问题和默认示例项目名称随界面语言即时切换，已生成的历史消息保留原文。
模型服务中的 URL 和 API Key 输入框默认留空，不自动加载已保存密钥；留空保留服务器连接。
构图页面提供模型选择、自定义模型 ID、URL 和 API Key 配置，供 TXT 抽取和表格智能构图使用。
构图配置保存至持久卷 `/data/configuration/construction-model.json`，不会写入数据集构图参数或报告。
完整的服务器部署、SSH 隧道、备份及故障排查步骤见 [单用户版部署说明](docs/SINGLE_USER_DEPLOYMENT.zh-CN.txt)。

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
- 超大图使用持久队列、分区快照和独立 `graph-engine-worker`，API/Agent 不全量加载；
- MCP 优先使用 `X-User-ID/X-Session-ID/X-Run-ID`，旧 `register_session` 只用于兼容。

入口：

```text
python -m graph_engine api
python -m graph_engine mcp http
python -m graph_data_service.large_graph.worker
```

超大图边列表/三元组上传文件可显式调用 `prepare_large_graph`；通过
`load_graph_from_upload` 加载时，字节数、流式边计数或固定内存节点基数探测达到任一阈值，
也会在构造 `GraphData` 前自动进入同一准备路径。完成后以返回的 `graph_version` 调用
`run_large_graph_algorithm`。大图能力目录会同步检查小图计算目录的 24 个算法入口、
展开后的 109 个具体算法/模式；全部入口均已接入大图路径，包括路径、流/割、生成树、
社区、全部中心性、连通性、环、拓扑、度分析、子图、链路预测、路径分析、团、可达性、
全局指标，以及 dev 新增的结构、高级连通性、树/DAG、匹配/着色和扩展距离 5 组算子，
并保留四个底层度计算入口。新增分组算子以 SQLite 外存状态为主；平面性、通用加权匹配、
均衡着色采用资源上限内精确内核，超限时不会无界物化整图，近似模式会在结果和能力目录中
明确标记。准备阶段同时限制单条记录、排序批次
字节数和多级归并 fan-in，约束内存与文件描述符；拓扑与算法状态使用节点归属的
SQLite 邻接分片，不回退到
GraphData/NetworkX；社区、采样中心性和语义替代模式
会在能力目录与结果中显式标记 `approximate`、复杂度和实际执行算法。社区传播采用
多进程节点分区与 SQLite 标签归并；可分解的度、Brandes/距离中心性、平均距离、
直径/半径、三角指标、全局最小割、环枚举/最大环、极大团枚举与聚合、传递闭包和
全图链路预测使用多进程 map/reduce；依赖严格顺序的模式使用外存串行内核，并由 capabilities 和
Artifact 的 `parallel_strategy` 如实标记。可物化全量结果由有界队列的并行分区 writer
写出。全图链路预测、Yen 候选路径、团/环枚举和全局最小割默认经过复杂度准入；
精确最大团与逐节点团计数另有更保守的穷举阈值；每个底层作业还受从进程启动时刻
起算、可配置的 wall-time 看门狗约束，超时会终止完整进程树并按租约清理暂存输出；
Yen 的实际常驻候选路径另受硬字节预算保护；`allow_expensive=true`
只绕过对应算法的默认阈值，任务仍受全局工作量和资源预算约束。状态、摘要与全量结果相互
分离：`get_job_status` 查询进度，`get_job_result` 返回有界摘要和 Artifact 引用，
`get_large_graph_artifact` 返回结果文件总数、有界的“下载序号—相对路径”预览和完整序号范围，
结果文件再通过返回的 `download_url_template` 按序号流式下载；需要严格小图语义时可在参数中设置
`strict_semantics=true`，近似映射以及有界枚举/截断模式会在入队前拒绝。
项目数据集自动进入大图路径时，Backend 会持久化准备作业的 `job_id/graph_id/graph_version`
并立即返回分区快照状态；会话恢复只轮询这一作业，发布后把准确版本写入
`session_state.large_graph_context`，不会因普通 `GraphData` 为空而重复准备。React 工作台只展示
有界的节点数、边数、分区数、版本和存储摘要，不尝试把快照装入浏览器画布。快照与 Artifact
发布会依次同步文件、目录和托管祖先目录，再原子改名并同步父目录，确保进程或机器崩溃后目录状态可恢复。

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

## 工作区身份

单用户版不提供登录、注册或退出入口。Backend 的公开 `/api/*` 请求统一归属
`IGRAPH_ADMIN_USER_ID`（默认 `admin`），客户端传入的 `X-User-ID` 会被覆盖；
服务间的 `X-User-ID` 与服务密钥契约保留。旧版多用户注册表和
`authentication.sqlite3` 不会被删除，但不再参与访问控制。

对话回答保存完成后即可继续发送消息。用户记忆与会话摘要由 Agent Runtime 的后台队列整理，
不会延长聊天的“思考中”状态。同一用户的整理任务跨服务实例按顺序执行；连续对话中，只有
新任务包含旧任务完整消息时才合并处理。任务持久化到 PostgreSQL，服务重启后继续处理，
单次整理最长 120 秒，失败最多尝试 3 次。取消、失败和等待确认的回答不会用于后台整理。
后台摘要可能短暂落后于聊天记录，后续回答以最近对话及已确认的工具结果为准。

工作区初次加载模型服务时会获得智增增默认配置，可以查看和修改密钥、地址及模型。
默认密钥来自部署配置（已有部署先读取原共享连接）；这只是初始副本，后续修改、停用、删除不影响部署配置。

没有登录 cookie 保护时，Backend 仍拒绝跨站写请求：带 `Origin` 的写请求必须与当前 Host 同源，
或位于 `BACKEND_AUTH_ALLOWED_ORIGINS` 列表中。前端默认与 Backend 同源，Nginx 保留浏览器请求的 Host。
HTTPS 反向代理部署时，通过 `BACKEND_AUTH_TRUSTED_PROXIES` 配置真实代理地址/CIDR；
显式跨源前端使用 `BACKEND_AUTH_ALLOWED_ORIGINS`，禁止使用 `*`。
相关配置示例见 `.env.example`。

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

### 服务器预置大图数据

浏览器无需上传大文件。管理员可先把支持的
`.txt/.csv/.tsv/.edgelist/.dat/.json/.pt/.npz` 文件放入
`BACKEND_SERVER_DATASETS_DIR`（默认 `data/server_datasets`）。Compose 部署使用持久卷中的
`/data/server_datasets`。建议先复制到隐藏的临时文件，再原子重命名，避免扫描器读到尚未
复制完成的文件：

```text
docker compose exec backend mkdir -p /data/server_datasets
docker compose cp /服务器上的路径/large-graph.edgelist backend:/data/server_datasets/.large-graph.edgelist.uploading
docker compose exec backend mv /data/server_datasets/.large-graph.edgelist.uploading /data/server_datasets/large-graph.edgelist
```

文本边表默认按有向图处理。无向图必须在发布边表前同时放置一个同名方向 sidecar，例如
`U-10M-100M.edgelist.meta.json`：

```json
{"directed": false}
```

建议先把 sidecar 和边表都复制成隐藏临时文件，先发布 sidecar，最后再原子重命名边表；这样
扫描器第一次看到边表时就能按正确方向预处理。方向会进入文件修订标识，修改 sidecar 后会生成
新的不可变预处理快照，不会错误复用旧方向的快照。未提供 sidecar 时可通过
`BACKEND_SERVER_DATASET_DEFAULT_DIRECTED` 设置全局默认值（默认 `true`）。

Backend 默认每 30 秒扫描一次目录。达到 64 MiB 的文本边表会立刻进入服务器级大图预处理
队列；刷新项目页也会触发同一个幂等检查。前端会显示排队、进度、完成或失败状态，只有完成
后才能“添加到项目/使用”。阈值和扫描周期可通过
`BACKEND_SERVER_DATASET_PREPROCESS_MIN_BYTES`、`BACKEND_SERVER_DATASET_SCAN_SECONDS`
调整。

预处理快照位于 `/data/large_graph/snapshots`，同一文件修订只解析、清洗和分区一次。会话使用
时只在目录中创建指向该不可变快照的独立逻辑版本，不传输原文件、不复制分区文件；会话后续
生成的派生版本和算法产物仍归该用户会话所有。删除项目数据集或消费会话不会删除管理员源文件
和仍可复用的服务器快照。小于阈值的文本文件以及 `.json/.pt/.npz` 仍使用原有会话加载路径。

Compose 启动七个进程容器，但仍是四个业务模块：

```text
frontend
backend
agent-runtime
graph-engine-api
graph-engine-worker
graph-engine-mcp
postgres
```

Graph Engine 的 HTTP/MCP/Worker 属于同一代码模块；Worker 与 API 共享持久目录但拥有
独立生命周期，PostgreSQL 是基础设施。

## 依赖清单

| 部署单元 | 依赖文件 | 主要依赖 |
|---|---|---|
| Backend | `deploy/requirements/backend.txt` | FastAPI、Uvicorn、Pydantic、HTTPX、python-multipart |
| Agent Runtime | `deploy/requirements/agent-runtime.txt` | `agno[os,mcp,openai,postgres,pgvector,opentelemetry]==2.9.0`、psycopg、SQLAlchemy、pgvector |
| Agent / Graph 共用计算栈 | `deploy/requirements/compute-cpu.txt` / `compute-gpu.txt` | 默认 CPU；可选 PyTorch 2.14.0 + CUDA 13.0、PyG、NumPy、SciPy、NetworkX |
| Graph Engine HTTP / Worker | `deploy/requirements/graph-data.txt` | 同一镜像；FastAPI、OpenAI SDK、NetworkX、iGraph、Leiden、Nebula、PyTorch/PyG |
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

### 模型提供商管理

在工作台右上角打开「模型服务」，管理提供商连接、发现或添加模型，并为新会话选择默认模型。详情见 [模型服务与提供商管理](docs/MODEL_PROVIDERS.md)。

### 远程图数据库问答（本地链路已验证，待独立验收）

会话可同时保留本地图和远程 Neo4j 连接。输入区或数据集面板中的“远程数据库”用于新建、测试、保存和选择连接；密码不会回显，编辑时留空保留原凭据。远程工具只读，查询卡片显示来源版本、已读取行数和两种截断状态。远程报告通过聊天或工作区交付，不复用会补入本地图统计的报告工具。

远程查询依赖既有运行控制来保存可信连接快照：本地部署需在各服务共享 `.env` 设置 `IGRAPH_RUN_CANCEL_ENABLED=true`，按 `docs/RUN_CANCELLATION.md` 迁移后重启相关服务，并核实 `/api/capabilities` 的 `run_cancel_v1` 为 true。

Runtime 使用 `.env.example` 的 `IGRAPH_REMOTE_GRAPH_*` 配置。必须设置独立的 Fernet 凭据密钥；用户可直接在前端填写数据库 URI、数据库名称、用户名和密码并测试、保存连接，无需预先配置主机或 CIDR 白名单。公网、内网、回环和数据库返回的路由地址均直接尝试连接；原有 `IGRAPH_REMOTE_GRAPH_ALLOWED_HOSTS` / `IGRAPH_REMOTE_GRAPH_ALLOWED_CIDRS` 配置不再生效。密钥缺失不影响普通聊天，不能保存或解析远程凭据。密钥更换后原密文不可用，应先制定迁移方案。当前 driver 固定为 `neo4j==6.3.0`；允许 Community 和可写账号；工具独立阻止写入、结构变更、管理操作。连接测试分别报告连接可用和数据库权限状态（verified_read_only / not_read_only / unknown），未知不等于已验证。单用户版中 Backend 的公开接口统一归属工作区所有者，忽略客户端传入的 `X-User-ID`；历史[交接记录](docs/testing/intelligent-graph-qa-handoff.md)中的裸 `X-User-ID` 限制描述适用于更早的版本。

离线回归：

```bash
.venv/bin/python -m pytest tests/test_remote_graph_catalog.py tests/test_remote_graph_api.py tests/test_remote_graph_backend.py tests/test_remote_graph_policy.py tests/test_remote_graph_results.py tests/test_remote_graph_executor.py tests/test_remote_graph_run_binding.py tests/test_remote_graph_tools.py tests/test_remote_graph_skill.py tests/test_cora_neo4j_import.py -q
(cd frontend && corepack pnpm exec vitest run src/remoteGraph.api.test.ts src/remoteGraph.app.test.tsx src/components/RemoteGraphConnections.test.tsx src/components/RemoteGraphQueryResult.test.tsx)
```

本地验收使用独立 Neo4j Community 容器和数据卷，开发者自行生成测试凭据；无需 Enterprise 或 reader 角色。脚本只接受环境变量 `IGRAPH_TEST_NEO4J_URI/DATABASE/USER/PASSWORD` 中的导入连接，拒绝目标已有不相容数据，不清空数据库；使用 `weights_only=True` 和固定 PyG/NumPy 类型白名单受限加载，不降级为不受限 pickle，不用于公共上传。

```bash
.venv/bin/python scripts/import_cora_neo4j.py --input cora.pt --manifest /path/to/cora-manifest.json
.venv/bin/python -m pytest tests/test_remote_graph_integration.py tests/test_remote_graph_agent.py --require-live -q
(cd frontend && corepack pnpm exec playwright test --config playwright.remote-graph.config.ts)
```

真实测试还需要环境变量：`IGRAPH_TEST_NEO4J_READER_USER/PASSWORD`、`IGRAPH_TEST_NEO4J_DEDICATED=1`、`IGRAPH_TEST_CORA_INPUT`、`IGRAPH_TEST_CORA_MANIFEST`、`IGRAPH_TEST_EVIDENCE_DIR`。Agent/浏览器使用 `IGRAPH_E2E_BASE_URL`、专用 `remote-graph-test-*` 测试账号的 `IGRAPH_E2E_USER_ID` 和平台已有真实模型的 `IGRAPH_TEST_MODEL_ID`，可通过 `IGRAPH_E2E_AUTHORIZATION` 或浏览器 `IGRAPH_E2E_STORAGE_STATE` 提供网关登录状态。不要把凭据写入仓库。浏览器测试禁用 trace、视频和截图以防表单泄密。

`--require-live` 在缺少前置条件时失败；普通离线运行会跳过真实测试，跳过不能作为验收通过。测试会保留专用会话、导入数据和成果以便复核，应用写入拒绝测试检查源数据未变，诱导文本测试会恢复原属性。


## 银行版预置金融图

`feature/user_case` 随镜像包含 `finance-ownership.npz`，无需挂载开发机的 CSV 目录或调用 LLM 构图。Graph Engine 启动时会校验预置资源并首次安装到持久数据卷的公共 Registry 和服务器数据源库；重启不会覆盖已有数据。

部署后新建或打开项目，在项目的数据集面板的服务器数据源库中选择 `finance-ownership`，添加到项目并在会话中选择，即可进行图分析及第一大股东、控制链、集团树、经济权益分析。Agent 也可通过 `list_datasets` 和 `load_dataset` 加载公共数据集 `ds_finance_ownership_v1`。

预置图由 `institution_main.csv`（5,000 行）、`individual_info.csv`（1,000 行）和 `institution_shareholder_relationship.csv`（13,436 行）生成，共 6,000 个节点和 13,436 条有向持股边，方向为股东 → 被投资机构。节点使用 `I:` / `P:` 区分机构和个人；所有原始字段、统计日期和关系行证据均保留。源数据有 13 处持股合计超限，金融工具会继续报告这些质量问题。源文件 SHA-256 和行数见 `graph_engine/resources/finance/manifest.json`。

需要更新预置图时，在仓库根目录执行：

```powershell
python scripts/prepare_finance_preset.py F:\mypaper\data\finance\finance_csv
```

更新预置数据时应同步更新数据集版本／标识，再重新构建镜像；已有部署的持久副本不会自动覆盖。`GRAPH_ENGINE_SERVER_DATASETS_DIR` 和 `BACKEND_SERVER_DATASETS_DIR` 如有自定义，须指向同一共享目录。工具 DAG 默认并发 3，`AGENT_TOOL_MAX_CONCURRENCY` 可设为 1–3，更大的值仍封顶 3。
