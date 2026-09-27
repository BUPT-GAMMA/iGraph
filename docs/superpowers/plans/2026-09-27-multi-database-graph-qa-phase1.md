# 多数据库智能问图第一批实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留 Neo4j 能力的基础上交付 FalkorDB 智能问图，并完成 Memgraph/NebulaGraph 的接入技术验证。

**Architecture:** 保留现有 Agent、两个远程工具及连接管理流程。共用执行模块处理身份、绑定和结果交付，各产品 Adapter 负责原生协议、只读执行和资源清理；不做查询语言翻译。

**Tech Stack:** Python/FastAPI/Agno/SQLAlchemy、现有 Neo4j driver、Redis 异步客户端用于 FalkorDB 协议、React/TypeScript、pytest/Vitest/Playwright、现有 Docker Compose。新增依赖和镜像在 Task 1 实测后固定版本，不使用浮动 latest 作为交付版本。

**Spec:** [多数据库接入设计](../specs/2026-09-27-multi-database-graph-qa-design.md)。执行者必须同时阅读设计、[研究笔记](../../research/2026-09-27-graph-database-coverage.md)及现有交接记录。

## Global Constraints

- 用户已免除逐份设计/计划确认；开发 agent 可完成范围内技术决策，不循环等待批准。新增费用、生产变更或明显扩大范围仍需用户确认。
- 保持“开发 agent 实现，本会话独立验收”的分工；本计划文件不是完成证据。
- 数据库账号不必只读。可写账号及权限未知账号均可接入；能否连接、查询与账号权限状态分别表达。
- 应用执行端仍拦截写入、结构变更、管理操作及危险执行入口；不把 skill、READ 路由或关键词黑名单当作完整保证。
- 一个 Graph Agent，沿用现有 Agent loop；业务请求不变成固定查询模板。
- 任务专属行为说明仅在远程 skill 和按需参考材料中，不加入全局提示词。
- 一轮绑定一个远程连接，保留本地图和工作区；不实现联邦查询。
- 不更换默认模型，不新增答案核验机制；回答质量单列，不要求先消除所有自由表述偏差。
- 保留既有未提交改动；提交采用中文 Conventional Commits，只暂存本任务明确文件，不合并、不推送。
- 本地代码改动完成后重建启动受影响的当前 Compose 服务，检查健康和访问地址；禁止删除现有数据卷。
- 新测试数据库使用专用隔离环境；可自行本地部署、生成私密凭据及导入已授权测试数据，不要求用户重新提供只读账号。不得对用户远程库做写入探针。

## Review Focus

1. 两个 Runtime 同时启动迁移：不得重复加列或丢失旧连接密文；Task 2 验证幂等与并发。
2. 新建表单换类型及旧请求迟到：旧密码和成功提示不得流入新类型；Task 5 验证。
3. 同样节点 ID 出现在不同库、带方向路径及大整数：不能混合来源或损失类型；Task 4/6 验证。
4. 中止等待后服务器仍执行、driver 二次解析绕过地址检查：不得虚报取消或绕过网络策略；Task 1/3/4 验证。
5. 双运行使用同一个 Adapter：超时、连接与凭据不能交叉；Task 3/4 验证取消隔离。

## 文件与责任地图

| 文件 | 责任 |
| --- | --- |
| `agent_runtime/remote_graph/contracts.py`、`catalog.py`、新增 `migrations.py` | 产品类型、连接持久化与旧数据迁移 |
| 新增 `agent_runtime/remote_graph/adapters.py`、`registry.py`、`executor.py` | 内部 Interface、注册、统一执行和结果交付 |
| `agent_runtime/remote_graph/neo4j.py`、新增 `falkordb.py` | 产品执行实现，不含用户目录管理 |
| `agent_runtime/remote_graph/policy.py`、`results.py` | 网络约束与有类型结果预算 |
| `agent_runtime/os_app.py`、`remote_graph/api.py`、`backend/app.py` | 装配和产品类型描述入口 |
| `frontend/src/types.ts`、`api.ts`、`components/RemoteGraphConnections.tsx` 及 CSS | 数据库类型选择和条件表单，复用已验收交互 |
| `agent_runtime/resources/skills/remote_graph_qa/`、`remote_graph/tools.py` | 原生查询说明与按需方言参考 |
| `deploy/falkordb-test.compose.yaml`、`scripts/import_cora_falkordb.py` | 专用测试环境和离线导入，不是 Agent 工具 |
| `tests/test_remote_graph_*.py`、前端现有远程测试 | 共用契约与旧功能回归 |

不开展全仓重构；执行前用 `rg` 核实当前路径与已有模式，局部文件变化记录到交接。

---

### Task 1：验证 FalkorDB 实际执行路径并固定测试环境

**Files:** 新增 `deploy/falkordb-test.compose.yaml`、`tests/test_falkordb_protocol.py`、`docs/testing/2026-09-27-multi-database-phase1-handoff.md`；按需修改 `deploy/requirements/agent-runtime.txt`、`.gitignore`。

**Interfaces:** 产出已实测的镜像 digest、客户端版本、原始协议夹具和服务器超时/取消结论，供 Task 4 使用。测试环境变量采用 `IGRAPH_TEST_FALKORDB_URI`、`IGRAPH_TEST_FALKORDB_PASSWORD`、`IGRAPH_TEST_FALKORDB_GRAPH`，不把值写进报告。

- [ ] 编写 `test_read_query_protocol_with_writable_account`：专用测试库预置节点后，`GRAPH.RO_QUERY` 读出计数；创建、属性更新、删除及索引修改均拒绝且图状态不变。测试夹具的管理连接与被测查询路径明确分开。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_falkordb_protocol.py --require-live -q`，确认未配置时明确失败而非绿色跳过；不得将环境失败当作功能红灯。
- [ ] 根据官方文档锁定镜像和 `redis` 客户端版本，启动独立 Compose 项目 `igraph-falkordb-qa`，以密钥文件配置认证。仅本机映射端口，加入现有后端网络；无资源冲突时使用 21188，否则记录实际端口。不要覆盖 Neo4j 项目。
- [ ] 补充并运行协议测试：参数、Unicode/转义值、空结果、标量、节点/边/路径、超大结果预算、服务端 timeout、客户端断开后任务结束证据。验证 Redis 连接不二次连接未校验地址；检查服务端有效输出/内存预算，不能只在 Python 读完后截断。
- [ ] 记录准入结论。若目标版本不满足安全或资源要求，给出具体失败和范围内替代版本验证；不回退到 `GRAPH.QUERY` 执行模型查询，不伪造已完成。
- [ ] 提交本任务文件：`test(remote-graph): 建立 FalkorDB 协议与只读执行基准`。

### Task 2：产品类型契约及兼容迁移

**Files:** 修改 `contracts.py`、`catalog.py`、`agent_runtime/os_app.py`；新增 `migrations.py`、`tests/test_remote_graph_migrations.py`；扩展 `tests/test_remote_graph_catalog.py`、`tests/test_remote_graph_run_binding.py`。

**Interfaces:** `ConnectionWrite.kind` 默认 `neo4j`，首版支持 `neo4j | falkordb`；`migrate_remote_graph_connections(engine: Engine) -> None`；PublicConnection/RemoteBinding 持久化 kind，其余既有字段保持兼容。

- [ ] 先写失败测试：`test_missing_kind_is_neo4j`、`test_unknown_kind_rejected`、`test_kind_cannot_change_on_edit`、`test_old_binding_loads_as_neo4j`。断言新 FalkorDB 连接保存/选择/冻结后 kind 不丢失，编辑密码留空仍保持凭据。
- [ ] 写真实旧表测试：`test_migration_keeps_ciphertext_id_and_revision`，迁移前后字节级相同；`test_migration_is_idempotent` 连续运行两次。另在专用 PostgreSQL 数据库或测试 schema 中验证并发迁移，禁止清空现有 Runtime 数据库。
- [ ] 执行 `.venv/bin/python -m pytest tests/test_remote_graph_migrations.py tests/test_remote_graph_catalog.py tests/test_remote_graph_run_binding.py -q`，确认预期失败。
- [ ] 实现加列、默认值与旧数据回填；在读取连接前显式迁移。PostgreSQL 使用事务锁协调并发，SQLite 保留单进程测试路径。按 kind 校验 URI：Neo4j 保持原规则；FalkorDB 仅允许 `redis`/`rediss`，拒绝 URI 内凭据及目标/参数注入，图名从 database 传递。
- [ ] 重跑上述测试为绿，核实旧记录无需密文重写或 revision 变化；提交 `feat(remote-graph): 增加数据库类型与兼容迁移`。

### Task 3：抽取内部 Adapter Interface，保持 Neo4j 行为

**Files:** 新增 `adapters.py`、`registry.py`、`executor.py`；修改 `neo4j.py`、`policy.py`、`os_app.py`；新增 `tests/test_remote_graph_registry.py`、`tests/test_remote_graph_adapter_contract.py`；更新现有 executor/tools/API 测试导入。

**Interfaces:**

- `RemoteGraphExecutor(catalog, settings)` 移到 `executor.py`，保持 `test_connection(uid, body)`、`describe(uid, binding, labels=None, relationship_types=None)`、`query(uid, binding, request, artifact_dir=None)` 的外部调用方式。
- `AdapterContext`：`connection: ResolvedConnection`、`deadline: float`（单调时钟）、`max_rows: int`、`max_bytes: int`、`network_policy: NetworkPolicy`；每次调用新建，不保存为共享可变实例状态。
- `AdapterRows`：`columns: list[str]`、`rows: list[list[Any]]`、`returned_rows: int`、`truncated: bool`；rows 已有界且值经过类型编码。
- `GraphAdapter.test(ctx) -> dict`、`describe(ctx, labels, relationship_types) -> dict`、`execute(ctx, request: QueryRequest) -> AdapterRows` 均为 async；取消通过当前 asyncio task 传播，不吞掉 `CancelledError`。
- `get_adapter(kind: str) -> GraphAdapter`、`list_database_types() -> list[dict]`；未知类型抛 `REMOTE_GRAPH_INVALID_CONFIG`，禁用类型不可执行。

- [ ] 写失败测试：`test_dispatch_uses_frozen_kind`、`test_unknown_kind_never_falls_back`、`test_adapters_do_not_share_credentials_or_cancellation`。后者同时运行两个连接，只取消一个，另一个结果和来源不变。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_remote_graph_registry.py tests/test_remote_graph_adapter_contract.py -q`，确认失败。
- [ ] 将 Neo4j 的 driver/只读校验/结构发现移动到 Adapter；将目录解析、时间预算、预览、query_id 和 artifact 保存收敛到共用执行模块。消除 describe 对带身份 executor 的反向依赖，不留两套 Neo4j 实现。
- [ ] 将网络校验结果与 Neo4j Address 包装分开：共用部分返回已核实 IP 与 TLS 主机名，协议 Adapter 实际使用该结果；测试 DNS 重绑定、TLS 名称、目标范围与二次连接拒绝。
- [ ] 运行既有 Neo4j executor、policy、tools、results、API、backend、run_binding 测试及 Task 3 新测试。真实 Neo4j 基准必须仍通过，查询拒绝不能退化为仅正则判定。
- [ ] 提交 `refactor(remote-graph): 抽取数据库适配接口并保留 Neo4j 行为`。

### Task 4：实现 FalkorDB Adapter 与有界结果

**Files:** 新增 `falkordb.py`、`tests/test_falkordb_adapter.py`、`tests/test_falkordb_integration.py`；修改 `registry.py`、`results.py`；新增 `scripts/import_cora_falkordb.py`、`tests/test_import_cora_falkordb.py`。

**Interfaces:** `FalkorDBAdapter` 实现 Task 3 的 GraphAdapter；第一版 `kind='falkordb'`、`query_language='cypher'`。逻辑目标 `database` 表示 graph 名称；不接受模型提供 Redis 命令或 URI。

- [ ] 写失败测试：`test_never_falls_back_to_writable_command`，断言只读请求被拒绝后没有调用可写命令；`test_params_are_encoded_not_concatenated_as_syntax` 覆盖引号、注释、Unicode、列表和空值；`test_nonexistent_graph_test_does_not_create_graph` 验证测试连接无创建副作用。
- [ ] 写失败测试：`test_result_preserves_entity_types_and_large_integers`、`test_sampling_is_explicit`、`test_byte_limit_applies_before_unbounded_materialization`、`test_cancel_closes_owned_connection_only`。真实协议无法提供的类型必须明确拒绝或标记，不用字符串化掩盖精度/结构丢失。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_falkordb_adapter.py -q` 确认失败，再以 Task 1 锁定的异步协议实现。参数编码遵循已验证语法；不以用户值进行字符串插值。结构发现使用已核实只读元数据，采样查询须标 sampled。
- [ ] 执行路径仅发送专用只读查询命令，服务器 timeout 按剩余总体期限设置；按协议层预算读取、错误脱敏，未知过程不放行。Task 1 若证明无法可靠有界读取，则先补该机制，不能以生产不可信结果做无限内存读取。
- [ ] Cora 导入使用现有 `scripts/import_cora_neo4j.py` 的可信加载/转换逻辑，保留 hash、方向和去重 manifest；导入脚本独立使用写权限，不加入 Agent 工具。验证非空且非本批导入目标时拒绝覆盖；建立小型边界夹具。
- [ ] 运行 `.venv/bin/python -m pytest tests/test_falkordb_adapter.py tests/test_import_cora_falkordb.py -q` 及 `tests/test_falkordb_integration.py --require-live -q`。负例在专用库验证可写账号下拒绝所有修改；记录任务结束与服务器期限证据。
- [ ] 提交 `feat(remote-graph): 支持 FalkorDB 原生只读查询`。

### Task 5：接入类型入口与现有连接体验

**Files:** 修改 `remote_graph/api.py`、`backend/app.py`、`frontend/src/types.ts`、`api.ts`、`components/RemoteGraphConnections.tsx`/CSS；扩展 `tests/test_remote_graph_api.py`、`tests/test_remote_graph_backend.py`、`frontend/src/remoteGraph.api.test.ts`、`components/RemoteGraphConnections.test.tsx`、`remoteGraph.app.test.tsx`。

**Interfaces:** `GET /internal/remote-graph-types` 经 `GET /api/remote-graph-types` 代理；`listRemoteGraphTypes()` 返回 `{types: RemoteDatabaseType[]}`。RemoteDatabaseType 字段固定为 kind、display_name、query_language、uri_example、target_label、username_required、support_status；第一版只开放 Neo4j/FalkorDB，不接受服务端下发可执行表单脚本。

- [ ] 写失败测试：类型接口身份验证和受控字段；旧请求不传 kind 仍为 Neo4j；新建类型切换清除旧密码、目标及测试状态；迟到测试响应不能恢复旧类型成功提示。
- [ ] 写失败测试：已有连接类型不可编辑；FalkorDB 显示“图名称”；保存并使用提交 kind；绑定失败重试仍只绑定一次保存结果；账号 unknown/not_read_only 不禁用操作。
- [ ] 运行新增前后端测试确认失败，实现类型选择和条件字段，保持列表、返回、密码留空、键盘焦点、窄屏和账户切换行为。产品类型入口失败时提示重试，不伪造支持列表。
- [ ] 运行 `cd frontend && node node_modules/vitest/vitest.mjs run src/remoteGraph.api.test.ts src/components/RemoteGraphConnections.test.tsx src/remoteGraph.app.test.tsx src/components/RemoteGraphQueryResult.test.tsx`；相关 Python API/backend 测试同样通过。
- [ ] 提交 `feat(remote-graph): 增加数据库类型选择与条件连接表单`。

### Task 6：工具方言材料与真实端到端交付

**Files:** 修改 `remote_graph/tools.py`、`resources/skills/remote_graph_qa/SKILL.md`；新增该 skill 的 `references/neo4j.md`、`references/falkordb.md`；更新 `tests/test_remote_graph_skill.py`、`tests/test_remote_graph_tools.py`；新增 `tests/test_multi_database_agent.py`、`frontend/integration/remote-graph-multidb.spec.ts`；更新第一批交接报告。

**Interfaces:** 保留两个工具名和 query/parameters/save_result；说明改为当前连接原生查询。source.kind 来自运行绑定；按当前 kind 加载技能参考，不开放模型选择任意连接。参考加载使用当前项目既有机制，若未注册 references 则补其加载测试，不能仅创建无人读取的文件。

- [ ] 先写失败测试：工具描述不再承诺所有库都是 Neo4j；绑定类型决定可信方言材料；普通聊天不加载所有材料；不同来源相同 ID 仍区分；导出 JSON 包含查询参数且不含秘密。修改 skill 前按项目技能写作约定操作。
- [ ] 运行相关 skill/tools/results 测试，确认新要求失败，再实现最小更改；保留模型可读中文传输修复，不向全局提示塞入规则。
- [ ] 编写真实模型用例，对 Neo4j/FalkorDB 分别完成结构、计数、参数过滤、追问、工作区处理及修改请求拒绝；数值对照 manifest，逐条检查实际工具调用。不要用固定调用次数/固定措辞代替行为验收。
- [ ] 浏览器实测新建 FalkorDB、测试、保存并使用、追问、编辑保留密码、刷新、解除，以及切回 Neo4j；保留本地图。测试用户独立、密码不截图，保存后绑定失败可以受控故障注入但必须标明。
- [ ] 完整前端测试和 TypeScript/Vite 构建；相关后端全套回归、两个数据库真实测试及新旧 Agent 用例通过。`--require-live` 不可被跳过替代，报告列明实际执行命令与未重演项。
- [ ] 重建当前 `igraph` 项目受影响的 agent-runtime/backend/frontend；仅当实际依赖变化时重建其他服务。重新检查容器健康、本地 21108 页面和 21109/health，不覆盖用户配置或数据卷。
- [ ] 交接 Neo4j/FalkorDB 分项结论、SHA、证据路径、截图、凭据私密位置和回答偏差清单；提交 `test(remote-graph): 验证双数据库智能问图与连接交互`。

### Task 7：Memgraph 与 NebulaGraph 技术验证，独立收尾

**Files:** 新增 `scripts/probes/remote_graph_memgraph.py`、`scripts/probes/remote_graph_nebula.py`、`docs/testing/2026-09-27-memgraph-nebula-feasibility.md`；隔离部署文件放 `deploy/`，私密配置不入库。

**Interfaces:** 仅测试脚本与分产品验证报告；不注册未验证产品，不修改当前默认模型，不将这两项未完成阻塞 Task 6 交付。

- [ ] 各产品先建立可重复的 pass/fail 探针：读取、参数、结构发现、写/DDL/管理/过程拒绝、同连接并发隔离、期限与取消。写入负例仅针对专用可恢复测试库。
- [ ] 锁定官方版本并独立部署，验证可写账号下的执行策略；不能仅借用 Neo4j EXPLAIN 类型或假定 nGQL 是完整 Cypher。缺少可靠分类时报告具体证据，不用只读账号掩盖缺口。
- [ ] 报告分别列出版本、原生查询能力、网络控制、资源预算、验证命令、失败原因、是否可进入产品实现；区分“已证明不可用”和“尚未验证”。
- [ ] 对可进入实现的产品自行补充对应实施计划及验收项，不再逐份请求用户确认；本计划不授权忽略新发现的重大范围变化。对未通过项保留证据并结束该项，不无限提示词试改。
- [ ] 提交 `docs(remote-graph): 记录 Memgraph 与 NebulaGraph 接入验证结论`，最终汇总第一批 A/B 各项状态。

## 执行与验收交接

Task 1→2→3→4→5→6 是工作软件交付路径；Task 7 可以独立执行，但不得并发修改共用产品文件。采用子 agent 时任务交接必须携带本计划、设计和已确定 Interface。

交接记录包含具体产品版本和能力范围，而非“主流数据库已全部支持”。模型解释偏差、身份防伪、云环境未验证继续单列。不要重复询问账号只读要求，也不要为已委托的文档复核反复暂停。

开发 agent 交付最终 SHA 和第一批报告后，本会话独立验收；未通过项给复现和修复建议。第二批、云数据库与 RDF 不混入本批代码提交。
