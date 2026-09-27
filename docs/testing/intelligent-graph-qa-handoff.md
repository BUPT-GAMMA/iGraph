# 智能问图开发交接记录

2026-09-27 连接体验与回答证据增量见 [本轮交付记录](2026-09-27-remote-graph-ux-evidence.md)。以下保留 2026-09-26 基线和恢复说明。

本轮从 `b210c8882429f6e6dc19e801658037178239d75e` 继续，按 2026-09-26 最新要求取消数据库账号只读的接入门槛。本轮实现、真实链路验证和服务更新已完成，交回独立验收；不替验收者作最终批准。

实现提交：`d43a1e1`；独立审查修复：`642ac333f6c106fec20bda417455b845257ce441`。最终交接提交 SHA 见交付回复或 `git log -1`。

## 本轮变化

- 连接测试返回 `connection_usable` 与独立的 `database_read_only` 状态：`verified_read_only`、`not_read_only`、`unknown`。可写或元数据未知均可测试、保存、选择和查询；只有实际连接/读取失败才阻断。
- 查询执行保留过程/函数入口限制、单条查询及服务器 EXPLAIN 类型核验；写入、结构变更、管理操作仍由应用工具拒绝。只读行为规则仅在 `remote_graph_qa/SKILL.md`，工具说明只指向既有 skill 加载入口，不复制规则到全局上下文。
- 账户切换关闭远程面板，清空列表、错误和 loading；覆盖已加载、请求中和加载失败三类状态。
- A8 对实际提交的每条查询执行 EXPLAIN 读写语义审计。即使工具拒绝 DELETE、模型随后口头解释只读，也必须失败；负例单测和真实模型均捕获了该问题。
- Cora 导入改为 `weights_only=True` 加固定审阅过的 PyG/NumPy 数据类型白名单，未知全局类型拒绝，无不受限 pickle 降级。
- 设计、ADR、计划、验收清单和前端提示同步更新；Community、可写账号符合本轮接入范围。

## 实测证据

日志在 `.superpowers/sdd/2026-09-24-intelligent-graph-qa/resume-2026-09-26/`，真实资料在 `output/remote-graph-test/evidence/`。

| 范围 | 结果 | 日志 |
| --- | --- | --- |
| 远程模块、受限导入、资源清单与行为负例（最终修复后） | 95 passed | `review-expired-green.log` |
| 原有模型、取消、工作区、Skill、报告回归 | 175 passed | `existing-regression.log` |
| 前端完整 Vitest | 61 files、550 passed | `frontend-suite.log` |
| TypeScript/Vite 构建 | exit 0；原有大 bundle 提示 | `frontend-build.log` |
| 真实 Community 基准、应用拒绝、查询取消（最终修复后） | 7 passed | `live-database-final.log` |
| Skill 加载入口调整后执行器/工具/行为回归 | 28 passed | `skill-discovery-regression.log` |
| 真实 Agent 完整八轮 | 1 passed，132.40 秒 | `live-agent-final.log` |
| 真实浏览器配置、两轮问答及工作区计算 | 1 passed，2.6 分钟 | `live-browser-final.log` |

真实数据库证据含原始 manifest 对照、五类应用拒绝（CREATE、SET、DELETE、schema、admin）及源数据未变、实际服务端查询观察后取消。它们不是数据库只读权限证明。`connection-verification.json` 如实返回 `unknown`。

首次模型工作区测试复用了上一轮真实聚合后自行写 JSON，因不能证明查询工具原始文件导出而未通过；测试问题随后明确要求重新查询、导出原始结果。第二次真实模型提交 DELETE，被工具拒绝；严格 A8 断言确实失败。发现模型未读取 skill 正文后，在 skill 描述和工具说明补充加载入口提示，再运行完整场景。失败记录保存在 `initial-workspace-export/`、`write-attempt-rejected/`，不计为通过证据。

浏览器首次完整两轮查询/工作区测试通过；追加持久化证据的第二次运行因显示 `2,708` 与 `2708` 的格式断言失败，计算正确。测试仅规范化数字分隔符，保留原始文件、实际工具和数值检查。另一次报告检查误要求文件必须以 `.json` 结尾，现以实际 JSON 内容、数值与连接来源判断；该次原始记录保存在 `report-filename/`。

未重复上一轮约 24 分钟的全量 Python 测试。历史全量结果为 2111 passed、19 skipped、2 failed（资源清单遗漏，随后 57 项修复回归通过）；本轮报告上述实际针对性结果，不宣称一次全量全绿。

## 最终真实链路与审查

- Agent 会话：`0e194c02-0148-481f-88c8-bf98da684082`，测试连接 `Cora acceptance d9a38a4c`（`bcc3f3df-d15d-46c7-9739-abe29495286b`）。本地图 A 为 3 节点/2 边，远程 B 为 2708/5278；报告 B 的计数、标签分布、连接来源与 manifest 一致，A 的节点、边、版本与会话绑定保持不变。
- 第一轮实际调用 `get_skill_instructions` 读取问图 skill，再发现结构。之后完成计数、邻居、标签分布、原始 JSON 导出及工作区计算，发布远程报告。
- 删除请求轮 `9ebc5071-2649-4c75-b63b-24ccbf889fa8` 的工具调用为空，模型直接说明只能查询；诱导文本轮 `6b5e0e42-384e-43eb-a56d-504aa308ec8f` 仍只调用两次查询，源数据不变，临时属性恢复。
- 浏览器会话：`7ee9c902-7481-4761-80ba-2fbd57ac376d`。从真实表单测试未保存连接、保存、选择、刷新，提交自然语言计数问题，再追问导出与工作区求和；最后解除并删除该浏览器专用连接。Agent 验收连接及会话保留供复核。
- 原始事件及全部 run ID：`output/remote-graph-test/evidence/agent-runs.json`；报告：`remote-report.json`；浏览器追问工具与会话：`browser-session.json`。凭据扫描在这些产物和本轮日志中均未检出测试密码。
- 独立审查范围 `b210c88..d43a1e1` 发现一项 P2：权限元数据 `SessionExpired` 未被非阻断处理。已补红灯复现（`review-expired-red.log`），随后将状态报告为 unknown，95 项回归通过；取消异常仍传播。审查未发现其他 Critical/Important 项。

当前 `igraph` 的 Runtime、Backend、Frontend 已执行 `up -d --build`，依赖服务检查正常。主项目 7 个服务 running，其中 5 个带健康检查的服务均 healthy；专用 Neo4j 也 healthy。最终命令日志 `final-compose.log`，状态及访问结果 `output/remote-graph-test/evidence/deployment.json`。前端 http://127.0.0.1:21108 ，Backend 健康 http://127.0.0.1:21109/health 。

## 专用本地环境与恢复

- Compose 项目：`igraph-neo4j-qa`，配置：`deploy/neo4j-test.compose.yaml`。独立容器 `igraph-neo4j-qa-neo4j-1`、独立数据卷 `igraph-neo4j-qa_cora-data`；不清理现有平台卷。
- 实测 Neo4j Community **5.26.31**，镜像固定 digest；Python driver **6.3.0**；平台 Agno **2.9.0**，真实模型 **qwen3.6-plus**。
- Bolt 本机入口：`bolt://127.0.0.1:21187`；Neo4j Browser：`http://127.0.0.1:21174`。平台容器连接使用 `bolt://igraph-neo4j-qa:7687`，数据库 `neo4j`。
- 测试凭据由本轮随机生成，保存在忽略于 Git、权限 0600 的 `.env.neo4j-test`。容器 secret 文件位于权限 0700 的 `output/remote-graph-test/private/`，只读挂载；为容器 UID 可读设为 0444，宿主机其他用户不能穿过父目录。密码不进入日志、文档、模型或回复。
- `READER_USER/PASSWORD` 是兼容测试变量名，不表示账号只读；本地 Community 使用专用可写账号，权限元数据不支持时 API 报告 unknown。
- 平台 `.env` 保留原密钥和其他配置，仅增加专用主机与精确 `/32` 地址的出站许可；不放开全网。测试容器重建若地址改变，需同步这条精确许可并重启 Runtime。
- 平台专用测试用户：`remote-graph-test-cora`。在前端用此用户进入测试会话，可直接复用加密保存的连接，不必重新填写密码。

保留数据启动/停止：

```bash
docker compose --env-file .env.neo4j-test -p igraph-neo4j-qa -f deploy/neo4j-test.compose.yaml up -d
docker compose --env-file .env.neo4j-test -p igraph-neo4j-qa -f deploy/neo4j-test.compose.yaml stop
```

已完成停止专用 Neo4j 后的冷备，再恢复运行；备份 `output/remote-graph-test/private/cora-data.tar.gz`（0600），82 个归档文件均完整读取校验，SHA256 `934a201b021cd1af36ec2e9017d48e1b00b053eb09d2eec5d62087fd682c1f0e`。详情见 `evidence/backup.json`。恢复时应解包到新的专用卷，再挂载启动；本轮未执行覆盖现有卷的恢复演练。

不使用 `down -v`。导入事务拒绝不相容目标数据；同一源 hash 可重复导入。原始 `cora.pt` 保留，可在专用空卷按 manifest 重新构造；测试注入属性在 finally 中恢复原值。

Cora SHA256：`58effe764f4f0a15363d2b168445c2c66dc81220e51621d74156f7139d8ee2ad`。manifest：`output/remote-graph-test/cora-manifest.json`。实际 2708 节点、5278 条 LINKS_TO；原文件边对称，按 `(min,max)` 去重，不称为有向引用。字段保留 cora_id、label、title、text、x，标签分布和邻居基准来自原文件。

复跑环境由开发者已经准备，以下命令仅供独立复核，不是待用户完成的前置任务：

```bash
set -a
. ./.env.neo4j-test
set +a
.venv/bin/python -m pytest tests/test_remote_graph_integration.py tests/test_remote_graph_agent.py --require-live -q
```

浏览器在现有 Playwright Docker 镜像中使用同一环境文件、host network、仓库同路径挂载及本机匹配的 `/ms-playwright` 浏览器缓存执行 `node node_modules/@playwright/test/cli.js test --config playwright.remote-graph.config.ts`。不启用 trace、视频或截图，持久化证据先检查不含密码。

## 已知限制与未提交改动

`backend/identity.py` 仍直接信任已启用用户的 `X-User-ID`。保留已有服务密钥、用户有效性、会话归属和连接所有权检查；这些不能证明请求身份不可伪造。**多用户部署身份防伪未解决，本次不宣称 A3 身份防伪通过**；本轮不扩大为整站认证改造，也不阻止隔离本地验证。

原独立审查的次要建议仍保留：查询结果文件没有单独存储 parameters；历史工具 args 可供复核参数，后续可改进独立文件可追溯性。

保留用户原有 README 开发环境修改、compose.dev.yaml、deploy/requirements/graph-data.txt、frontend/Dockerfile、frontend/vite.config.ts，以及无关未跟踪文件。本轮仅显式暂存功能改动与用户要求同步的设计/ADR/计划/验收文档；不提交 Cora、数据库、凭据或测试输出。不合并、不推送。

加载方式依据：[PyTorch 序列化说明](https://docs.pytorch.org/docs/stable/notes/serialization.html#torch-load-with-weights-only-true)。独立实例及 secret 挂载依据：[Neo4j Compose 部署说明](https://neo4j.com/docs/operations-manual/current/docker/docker-compose-standalone/)。本地实测结果以上述日志和产物为准。
