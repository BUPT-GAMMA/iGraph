# 智能问图开发交接记录

当前为**部分交付，真实验收与认证边界未完成**，不能判定整体通过。任务 1、3、4、5、6 已完成离线交付；任务 2 的身份防伪、任务 7 的真实数据库/Agent 验收，以及任务 8 的最终完成交接仍待补齐。用户尚未确认独立验收通过。

## 提交与工作区

- 分支：`feat/intelligent-graph-qa`；起点：`637b75873f676e029c35b3cd1409fbb03d951009`。
- 当前实现/测试入口末提交：`bf1d3de2a4a2eb23af7a9137a35a7fb7f79f9c4c`；交接文档的提交 SHA 以最终回复及 `git log -1` 为准，尚无“全部完成 SHA”。
- 不合并、不推送。初始用户修改保持未提交：README.md、compose.dev.yaml、deploy/requirements/graph-data.txt、frontend/Dockerfile、frontend/vite.config.ts。
- README 新增的问图说明已独立暂存并提交，原有开发环境说明修改仍在工作区。初始未跟踪文件（包括计划、设计、验收清单、Cora、图片及原测试结果）未纳入提交。
- 已比较起始记录：所有初始未跟踪文件 SHA256 未变；原有五个已跟踪文件的差异内容完全保留。基线和日志保存在 `.superpowers/sdd/2026-09-24-intelligent-graph-qa/`，工作未完成前保留该目录。

| 提交 | 内容 |
| --- | --- |
| `f1ace0c` | 用户连接目录、revision CAS、Fernet 凭据加密与可选密钥 |
| `41609d8` | Runtime 连接 API、Backend 转发、会话独立远程选择 |
| `c2c8ad9` | Neo4j 权限核验、网络限制、只读查询、结果预算与异步取消 |
| `b614b98` | 可信运行快照、原生开始/继续运行接线与远程工具 |
| `0cbc1ee` | 问图 Skill、报告/可视化的数据源边界 |
| `2bedfc1` | 连接管理、选择恢复、本地图共存、查询依据卡片 |
| `b886ec2` | Cora 导入、真实权限/Agent/浏览器验收入口及使用说明 |
| `7ec9746` | 修复当前来源提示与结构过滤顺序 |
| `bf1d3de` | 内置 Skill 资源清单纳入 remote_graph_qa |

差异文件完整清单可用 `git diff --name-only 637b758..bf1d3de` 复核。主要改动在 `agent_runtime/remote_graph/`、运行控制及工厂接线、`backend/app.py`、前端会话/聊天/连接组件和相应测试。没有新增独立 Agent 循环，没有升级 Agno。

## 已实测证据

以下日志路径均相对于 `.superpowers/sdd/2026-09-24-intelligent-graph-qa/`。

| 命令/范围 | 实测结果 | 证据 |
| --- | --- | --- |
| 连接目录 + 模型提供商 | 52 passed | `task-1-tests.log` |
| 目录/API/Backend/既有项目回归 | 89 passed | `task-2-green.log` |
| 远程执行器/策略/结果 | 38 passed | `task-3-tests.log` |
| 远程工具/运行绑定/取消/工作区 | 90 passed | `task-4-tests.log` |
| Skill 加载、Skill API、报告回归 | 55 passed | `task-5-tests.log` |
| 前端完整 Vitest | 61 files、543 passed | `task-6-suite.log` |
| 前端 TypeScript + Vite 构建 | exit 0；存在 bundle >500KB 提示 | `task-6-build.log` |
| 远程功能 + 导入 + 真实测试的普通离线运行 | 83 passed、7 skipped；不是实时验收通过 | `task-7-regression.log` |
| 真实数据库/Agent：`--require-live` | exit 1，7 项前置条件错误 | `task-7-live-required.log` |
| 真实 Playwright 未提供 BASE_URL | exit 1，明确拒绝启动 fake backend | `task-7-browser-required.log` |
| 提供实际 BASE_URL 后 Playwright `--list` | 收集 1 项；仅收集，没有执行真实链路 | `task-7-browser-collect.log` |

曾启动全量 Python 测试，结果为 **2038 passed、12 skipped、1 failed、667 subtests**（1423.47 秒）。失败为 `tests/servers/test_large_graph_pipeline.py::test_stale_recovery_kills_descendants_after_session_leader_exits`，单独重跑 **1 passed**。相关既有源码未修改；保留全量失败记录，不能写成“全量 pytest 通过”。该全量运行开始于任务 2 改动之前。日志为 `task-1-suite.log`、`task-1-suite-failure-rerun.log`。

前端 App 集成测试验证：远程 B 选择/刷新不清空本地图 A；空草稿可创建会话并选择远程连接，无需本地上传。表单测试覆盖测试不保存、失败保留输入、密码留空编辑、删除 revision、选择/解除。结果卡片单独展示读取条数、预览截断及底层截断，只显示允许的相对成果路径。

实际浏览器已访问本机 Compose：新会话可打开连接面板；对未允许的虚拟目标执行测试，显示 `REMOTE_GRAPH_INVALID_CONFIG`，表单保留；刷新后入口正常，连接列表仍为空。`task-6-browser.log` 和 `task-6-browser.png` 保留证据，截图前已清空密码。这里没有成功配置真实数据库或真实模型问图，不替代 A13。

Skill 规则测试采用独立上下文：旧规则会把本地图 A 统计补入 B 报告；新规则改用工作区成果并排除 A 统计，诱导数据库文本不作为指令。该证据只是规则模拟，不能替代真实 Agent 运行。

## 本地部署

当前 Compose 项目是 `igraph`，配置为 `compose.yaml` + `compose.dev.yaml`。每个运行时代码交付点均重建受影响服务，前端交付时执行：

```bash
docker compose -p igraph -f compose.yaml -f compose.dev.yaml up -d --build --wait agent-runtime backend frontend
docker compose -p igraph -f compose.yaml -f compose.dev.yaml ps
curl --fail --silent http://127.0.0.1:21109/health
curl --fail --silent --output /dev/null http://127.0.0.1:21108/
```

最近检查：agent-runtime、backend、graph-engine-api、graph-engine-mcp、postgres 为 running/healthy；frontend 和 graph-engine-worker 为 running（当前开发配置无健康检查）。Backend 返回 `{"status":"ok","module":"backend"}`；前端 HTTP 200。地址：<http://127.0.0.1:21108/>、<http://127.0.0.1:21109/health>。日志 `task-6-compose.log`。

Compose 已通过现有 `env_file: .env` 传入新配置，无需改动用户现有 Compose 文件。Fernet key、目标主机/CIDR 和查询预算见 `.env.example`。默认不允许远程网络目标；未配置密钥时保留普通聊天能力。

首次重建暴露了既有 SQLAlchemy 依赖声明问题：新解析的 SQLAlchemy 2.1.1 未隐式包含 greenlet，Agno 导入失败。已将原声明改为 `sqlalchemy[asyncio]>=2.0,<3`，容器内复现及重建验证通过。本地测试环境补装既有缺失依赖 httpx2、anthropic；前端使用冻结 lockfile 补齐依赖。Agno 保持 2.9.0，Neo4j driver 固定 6.3.0。

## 尚未满足的验收条件

| 项目 | 当前状态与后续动作 |
| --- | --- |
| A3 身份防伪 | `backend/identity.py:current_user` 仍信任浏览器 X-User-ID。连接层按已认证用户隔离，不能阻止浏览器冒用另一有效用户。需要明确可信认证网关及头覆盖保证，或完成真正的登录认证；不能以 mock current_user 测试代替。 |
| A7 数据库端只读 | 尚无已确认许可的专用 Neo4j/reader；没有接受 Enterprise 评估/付费条款，没有以 Community 可写身份替代。 |
| A13 真实模型链路 | 验收脚本已提供，但尚未实际运行；无真实模型版本、run ID、工具事件及成果可交付。 |
| A14 Cora 基准 | 根目录 `cora.pt` 未加载、未导入；等待来源和导入用途确认。原始文件 SHA256 为 `58effe764f4f0a15363d2b168445c2c66dc81220e51621d74156f7139d8ee2ad`。无 manifest 或真实节点/边基准。 |
| A6/A8/A10/A11/A12 的真实部分 | 离线执行与规则测试已有证据；真实 schema、模型修正/追问、数据库取消、工作区后处理和 A/B 报告共存仍需实际模型与数据库验证。 |

测试账号：需部署维护者提供专用 `remote-graph-test-*` 用户及必要网关登录状态。浏览器入口探测使用现有 admin，但真实验收脚本拒绝把它用作测试账号。只读连接名称、server/model 版本、manifest 路径和 Agent run ID 当前均**未生成**，不得填入虚构值。

真实环境准备和命令见 README“远程图数据库问答”节。导入脚本要求 `--trusted-input`，在一个事务内分批参数化写入，仅接受空库或同一 SHA 批次，不清空其他数据。权限探针直接以 reader 发 WRITE 事务，验证数据库拒绝 CREATE/SET/DELETE/schema；只在专用库执行且不提交。诱导文本测试用 importer 身份添加临时属性并在 finally 中恢复。

## 实施决策与限制

- 沿用现有功能分支和开发挂载工作区，以保留用户改动；错误时可根据基线与独立提交拆分回退。
- 用户明确执行请求覆盖计划中旧的“仅验收”说明；未改变验收标准。
- 增加内部会话绑定读取接口，使列表/刷新从服务端目录恢复；有额外有界请求成本。
- 使用保守权限元数据和过程/函数限制，无法验证时拒绝执行；不兼容的数据库权限元数据需要后续适配，不能降级到可写账号。
- Neo4j resolver 固定已检查 IP，并借助 driver 6.3.0 的 `_host_name` 保持 TLS 主机校验；升级 driver 时必须重新验证此私有接口及路由解析。
- 运行前置 hook 注入可信来源事实，工具仍只使用冻结快照；Agno 升级需验证 hook 时序。
- Cora 方向依据实际边对称性记录为去重规则，关系命名 LINKS_TO，不推断成论文引用；导入事务规模随可信文件增长。
- 暂无最终安全/真实验收结论。当前网络默认拒绝与未配置凭据不会补足身份认证缺口。

## 独立代码审查与修复

独立 reviewer 审查 `637b758..b886ec2` 后指出两项 Important 问题，均已先复现测试失败再修复：

1. 编辑当前连接后界面仍显示旧来源，而下一轮查询已使用新版本。现刷新连接目录时同步当前公开绑定，历史查询结果来源保持不变；App 回归 3 passed。
2. 结构过滤在默认 100 行预览之后发生，指定第 101 行关系类型仍无法发现属性。现标签/关系类型及关系属性均在 Cypher 端过滤后再应用预算；执行器回归 15 passed。

审查没有提出 Minor 项。认证边界被保留为阻止整体完成的 Critical 条件；外部许可、文件可信性、真实 DB/模型运行不可能通过代码审查代替。日志为 `review-source-red.log`、`review-source-green.log`、`review-schema-red.log`、`review-preview-red.log`、`review-schema-green.log`。修复后的全量测试和服务状态见下方最终记录。

最终复核补充：审查修复后的前端全量为 **61 files、544 passed**，构建 exit 0（同一 bundle 提示），日志 `final-frontend-suite.log`、`final-frontend-build.log`。最终要求范围的 Python 回归在修复前为 **258 passed**，日志 `final-required-python.log`；修复后执行器 15 passed；最终全量及清单修复复测见下方。Runtime/frontend 再次重建，Backend 依赖检查正常；7 服务均 running，有健康检查的 5 服务 healthy，两个 HTTP 地址通过。状态快照为 `final-compose-status.json`。


## 最终测试与本地配置（2026-09-26）

审查修复后的完整 Python 命令 `.venv/bin/python -m pytest -q` 实测 **2111 passed、19 skipped、2 failed、667 subtests passed、64 warnings**，耗时 1439.99 秒，exit 1。两个失败均为 `tests/test_resource_layout.py` 中旧的显式内置 Skill 名单缺少 `remote_graph_qa`，没有执行器或业务行为失败。将新增 Skill 纳入预期清单后，运行资源布局、远程 Skill、Skill API、报告相关测试，**57 passed，exit 0**，日志 `resource-inventory-fixed.log`。本次仅修改测试清单一行，未重复执行另一次约 24 分钟全量；保留全量 exit 1 与修复后局部通过的原始证据，不改写成一次全量通过。首次全量中出现的旧大图间歇失败本次没有复现。

本地最终部署检查发现原 `.env` 没有启用 `IGRAPH_RUN_CANCEL_ENABLED`，这会使远程工具缺少可信运行绑定。已完成以下实际步骤：

1. 原生运行状态核对为 12 COMPLETED、1 CANCELLED；没有持久化图作业进度记录。
2. 在开关关闭时执行 `docker compose ... exec -T agent-runtime python -m agent_runtime.migrate_execution_controls`，输出迁移完成；不删除控制表或数据卷。
3. 在忽略于 Git 的本地 `.env` 新增 `IGRAPH_RUN_CANCEL_ENABLED=true`。远程目录经数据库查询确认 0 条连接且旧密钥为空后，生成独立 Fernet key，写入 `IGRAPH_REMOTE_GRAPH_SECRET_KEY`；未输出密钥，文件权限为 600。现有其他配置内容保留。后续保存凭据需保留/备份此 key，未对已有密文做轮换。
4. 统一重建启动 Runtime、Backend、Frontend、Graph API、MCP、Worker。`final-controls-compose.log` 记录成功；Postgres 数据卷保留。
5. `/api/capabilities` 实测 `run_cancel_v1=true`、`job_cancel_v1=true`、`edit_latest_turn_v1=true`。模型列表 HTTP 200（10 个启用且支持工具的配置），现有会话列表 HTTP 200；这些读取不等于实际模型调用成功。Backend 健康及前端 HTTP 均通过。

Runtime 容器中实际版本为 Agno **2.9.0**、Neo4j driver **6.3.0**。目标地址白名单仍为空，等待真实数据库目标后按最小范围配置；没有放开全网访问。最终浏览器入口/错误/刷新复核见 `final-browser.log`。初始用户未提交文件仍保留；本地 `.env` 的上述新增配置是本任务额外的未跟踪部署状态。

启用运行控制属于本地开发部署所需前置条件，不宣称生产/GPU 发布验收完成。回退这项本地配置可移除新增开关并重启整组服务，保留控制表和加密密钥；旧模式下远程工具会因无可信运行快照而拒绝执行。
