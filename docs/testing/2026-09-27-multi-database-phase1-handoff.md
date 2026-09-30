# 多数据库智能问图第一批交接

更新：2026-09-28。最终实现 `284f24b` 已完成全分支审查及修复范围复审；[最终独立验收](2026-09-28-multi-database-phase1-acceptance.md)汇总状态、证据与限制。Task 6 产品提交 `023aadd`，行为参数验收修复 `3e8d6e3`，下列按阶段记录保留。保留现有 Graph Agent loop 和配置的 `qwen3.6-plus`，未更换默认模型、未新增答案核验机制。

## 当前分项状态

| 交付项 | 结论 | 可复核证据 |
| --- | --- | --- |
| Neo4j 5.26.31 | 原生 Adapter、真实连接、旧 Agent 回归及新增参数/工作区/拒绝/本地图保留验收通过（verified） | 7 个真实数据库测试；旧 Agent 2 通过；旧浏览器 1 通过；下列 Task 6 记录 |
| FalkorDB 4.20.7 | 有界只读 Adapter、真实结构/计数、参数/工作区/拒绝/本地图保留及双库浏览器通过（verified） | 10 个 Adapter live + 9 个协议测试；参数 schema 实验成功绑定对象；下列 Task 6 记录 |
| 回答证据质量 | **未完全通过** | 统计口径归属、业务释义、属性普遍性及“未发现定义”等无证据断言见逐事实复核 |
| Memgraph 3.13.1 | Task 7 真实探针完成，当前候选不准入；未注册产品入口 | [分产品报告](2026-09-27-memgraph-nebula-feasibility.md)：EXPLAIN 分类假设失败；期限/主动取消通过；结果字节预算未实现 |
| NebulaGraph 3.8.0 | Task 7 真实探针完成，当前候选不准入；未注册产品入口 | [分产品报告](2026-09-27-memgraph-nebula-feasibility.md)：完整副作用分类未解决，服务器期限/主动取消未验证；不推断产品本身不支持 |

## 实施与独立审查证据地图

| 任务 / 提交 | 验证与审查结论 |
| --- | --- |
| Task 1 `14a754e`、`97ceb2f` | 真实协议 9 项通过；独立审查指出缺少原始 RESP 夹具，补充 base64 字节和 SHA 后通过。原始风险与限制保留在本文历史记录。 |
| Task 2 `9b5151b` | 85 项回归通过、主机 PostgreSQL pytest 跳过；另在真实 PG 专用 schema 双线程迁移通过。实际 31 条旧连接的 ID、revision、密文 MD5 与迁移前私密指纹逐字节一致。独立规格/质量审查通过。探针及日志：`output/multi-database-phase1/postgres-concurrent-migration-probe.py`、同名 `.log`；备份/指纹位于私密目录。 |
| Task 3 `0a78afc`、`e1bf96c` | 81 项回归、7 个 Neo4j live 通过；独立审查要求两个查询确实重叠时只取消一个，补充 7 项聚焦测试后通过。 |
| Task 4 `8750275`、`560a0c3` | 原始 120 项、修复后 103 项相关回归与最终 10 个 FalkorDB live 通过；独立审查发现省略投影的列表推导仍可进入，修复后复审通过。真实 TLS、连接/读取取消隔离、有界帧解析及 Cora manifest 验证见历史记录。 |
| Task 5 `47d4786` | 576 个前端测试、50 个 Python 测试通过，1 个已知 PG 主机跳过，TypeScript/Vite 通过；独立规格/质量审查无问题。 |
| Task 6 `023aadd`、`3e8d6e3` | 可信方言引用、参数 schema、真实模型/浏览器及参数实际影响过滤验收；完整独立审查无 Critical/Important，Minor 已修复，控制器确认最终审查通过。下列回答证据质量仍未完全通过。 |
| Task 7 `b530c33`、`2d50759` | Memgraph/NebulaGraph 隔离实例与可重复探针，分别未准入；首次独立审查通过，三项探针判定/清理问题修复并通过范围复审，37 项聚焦回归通过。未将未验证能力注册到产品。 |
| 最终修复 `284f24b` | 全分支审查发现 FalkorDB 隐式堆构造与 Memgraph 断连观察问题，集中修复后两项复审均 ADDRESSED、无新增重要问题；253 项相关后端、26 项真实数据库回归通过，runtime 已重新构建。详情见最终验收记录。 |

私密凭据位置：`.env.neo4j-test`、`.env.falkordb-test` 均为 Git 忽略、0600；FalkorDB 密码文件 `output/multi-database-phase1/private/falkordb-password` 为 0600，父目录 0700。迁移前备份 `output/multi-database-phase1/private/pre-migration-remote-graphs.dump` 和指纹仅供本机维护，不公开内容。

## Task 6：方言与真实端到端

- 保留 `describe_remote_graph` / `query_remote_graph` 名称与 query/parameters/save_result。工具描述使用当前连接原生查询。
- Skills 实例只注册当前服务端冻结 kind 的 reference；未绑定时无远程 reference，普通聊天只看到摘要，未把方言正文塞入全局提示。实际 `get_skill_reference` 读取通过，中文 JSON 保持模型可读。
- 首次真实基线未读取方言，新增单元测试在绑定参数和原生描述处失败；实现后 skill/tools/results 通过。结果参数导出是 Task 4 已有功能，本任务补充原始文件与来源/秘密检查。
- 真实模型首次参数请求失败：传入字符串化对象/数组，重复校验失败后改为字面量，却声称参数化成功。已保留失败证据，未放宽执行端校验或循环保护。
- 局部诊断发现 Agno 将 `dict[str, Any]` 参数值广告为空对象。改为 bare dict 修正 JSON 值 schema 后模型仍字符串化。最终 provider schema 的 nullable anyOf 保持未改写；一次受控实验将两个远程工具的可选结构化字段以 object/array 加省略表达，保留 callable 的 None/省略语义与字符串拒绝。两种真实库均随后传递对象 `{"label":0}`、读到 298，并在工作区计算 596。此为已观察到的兼容改进，不宣称所有模型行为已解决。
- 新浏览器使用真实 21108/21109 服务和配置模型。完成 FalkorDB 新建、测试不保存、保存并使用、失败后仅重试绑定、密码留空编辑、刷新、计数和工作区追问、解除/删除、切回 Neo4j 并重新查询；3 节点/2 边的本地图内容与版本保持。**仅第一次绑定 HTTP PUT 被测试主动 abort**；其余接口、数据库和模型均真实。测试使用既有专用 `remote-graph-test-*` 用户、独立会话；不把同用户会话隔离夸大为新身份认证证明。
- 密码填写前或字段清空后才显式截图；trace/video/自动失败截图关闭。主机 Chromium 缺 libatk，因此最终在既有 Playwright v1.63.0-noble Docker 内执行，挂载匹配浏览器缓存。首个新测试 UID 没有该用户的模型配置导致创建会话失败，最终使用已配置的专用测试账号。

### 执行记录

所有证据位于 `output/multi-database-phase1/`，Git 忽略；仓库内测试与本文保留复现方法。

| 实际命令 / 环境 | 结果与证据 |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_remote_graph_skill.py tests/test_remote_graph_tools.py tests/test_remote_graph_results.py -q` | 初始 2 失败/14 通过 → 16 通过；参数 JSON Schema 新测试再现失败 → 17 通过；后续注册 schema/运行时兼容聚焦 14 通过。日志 `task6-unit/`。 |
| 过滤出所有 `test_remote_graph_*.py`（排除 agent/integration），另加 `test_falkordb_adapter.py test_import_cora_falkordb.py -q -rs` | 最终 **171 通过、1 个主机 PG 跳过**；`task6-regression/backend-units-final.log`（之前 169 项记录也保留）。 |
| 加载两份私有环境后，`pytest tests/test_remote_graph_integration.py tests/test_falkordb_integration.py tests/test_falkordb_protocol.py --require-live -q` | **26 通过、无跳过**；`task6-regression/live.log`，分目录 `task6-live-neo4j/`、`task6-live-falkordb/`。未重演危险 OOM 压力查询。 |
| `cd frontend; node node_modules/vitest/vitest.mjs run`；`npm run build` | **61 文件/576 测试通过**；TypeScript/Vite 通过，仅既有大 chunk 提示；`task6-regression/frontend.log`、`build.log`。 |
| `pytest tests/test_remote_graph_agent.py --require-live -q` | **2 通过，220.47 秒**；`task6-agent-legacy/results.log`。事实复核不等于 pytest 数值门槛。旧夹具文件名复用导致第一例 SSE 未保留，已从持久消息恢复 `legacy-full-session-recovered.json`；原始报告仍保留。夹具已改为逐测试目录并在失败断言前保存事件，原断言保留。 |
| `pytest tests/test_multi_database_agent.py --require-live -q` 与聚焦 `-k parameter` | 初始成功结构/计数 2 项，参数失败证据 `task6-agent/`；schema 修复后证据 `task6-agent-schema/`；可选字段实验 `task6-agent-optional-schema/`，实际对象绑定与计算正确但旧测试只接受 Ephemeral 路径而拒绝合法 Derived 原始副本。最终仅重跑参数/工作区/拒绝/保留两项：**2 通过、2 deselected，135.87 秒**，`task6-agent-final/results.log`；未把分阶段证据写成一次四项全绿。最终新增的完整字段/成功来源断言在聚焦运行导入测试后才补齐，另以当前检查器复核四个真实案例/十轮工具记录（`task6-agent-final/postcheck.json`），不冒称已在该次 pytest 执行。两份数据库原始 JSON 与 API 下载的发布副本进行了独立完整相等比对：`task6-agent-final/artifact-comparison.json`；所有 source/query/parameters/rows/完整性/query_id 及其余字段相等。 |
| Docker 内 `node node_modules/@playwright/test/cli.js test --config playwright.remote-graph.config.ts remote-graph-multidb.spec.ts` | **1 通过，1.2 分钟**；`task6-browser-multidb/results-live.log`、`browser-binding-retry.json`、`browser-session.json`、`neo4j-switch.json`。 |
| 同上命令选择 `remote-graph.spec.ts` | **旧浏览器 1 通过，1.3 分钟**；`task6-browser-legacy/results.log`，同时保留桌面/窄屏中英文截图。 |

复现新 Agent 时另设置 `IGRAPH_TEST_FALKORDB_MANIFEST=output/multi-database-phase1/task4-falkordb/cora-manifest.json`；`IGRAPH_TEST_EVIDENCE_DIR` 为每轮新目录。浏览器的证据和 manifest 使用仓库绝对路径，Docker 使用 host network、仓库同路径挂载、`PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` 与本机浏览器缓存挂载，通过 `-e VARIABLE_NAME` 传递现有环境，勿打印变量值。

截图：[FalkorDB 空密码表单](../../output/multi-database-phase1/task6-browser-multidb/connection-form.png)、[保存后绑定失败](../../output/multi-database-phase1/task6-browser-multidb/saved-not-used.png)、[已选择](../../output/multi-database-phase1/task6-browser-multidb/connection-selected.png)、[查询默认卡片](../../output/multi-database-phase1/task6-browser-multidb/query-collapsed.png)、[展开证据](../../output/multi-database-phase1/task6-browser-multidb/query-expanded.png)、[切回 Neo4j](../../output/multi-database-phase1/task6-browser-multidb/neo4j-switch.png)。

### Task 6 审查补充：参数必须影响实际过滤

独立代码审查无 Critical/Important，提出一项 Minor：原验收只检查参数非空，字面量过滤加未使用参数仍可能通过。已补行为验证：参数值必须匹配请求标签；保持模型生成的 Cypher 原文，仅将匹配的参数值换成 manifest 中计数不同的另一标签，经生产 Adapter 做独立有界只读查询，结果必须随之改变。无需固定参数名、谓词写法或模型调用次数。

- 聚焦回归 `pytest tests/test_remote_graph_behavior_assertions.py tests/test_remote_graph_tools.py -q`：**20 通过**。此前 RED 为 4 失败/4 通过，包含“字面量过滤 + 未使用参数”、只在注释中提及参数、目标值不符及任意参数名的正例。
- 加载两份私有环境后，`PYTHONPATH=. .venv/bin/python output/multi-database-phase1/task6-review-fix/replay.py`：对已有两组成功模型查询各做一次独立读取，参数由 0 换为 1，Neo4j/FalkorDB 均从原计数 298 对应到 418，与 manifest 一致。原始 artifact SHA 不变，没有重跑模型、修改查询原文或写源数据库。
- 复核直接使用生产 Adapter，限制 2 行/8 KiB/8 秒总期限并保留数据库自身期限；仅接受专用 localhost QA 端点和精确 loopback CIDR。首次测试仅配置 allowed_hosts，被现有网络策略拒绝，补上测试专用精确 CIDR 后通过；产品策略未变。
- 证据：`task6-review-fix/red.log`、`green-final.log`、`live.log`、`parameter-replay.json`，保留原 source、query_id、原/替代参数与计数。它是独立验收读取，**不是模型新增工具调用**。此次仅测试/文档变化，沿用 `023aadd` 已重建健康的产品服务。

### 最终本地交付

已按当前 `igraph` 项目的 `compose.yaml + compose.dev.yaml` 执行 `docker compose -p igraph -f compose.yaml -f compose.dev.yaml up -d --build --no-deps agent-runtime backend frontend`；日志 `task6-unit/rebuild-final.log`。图数据库、图计算服务和数据卷未重建/删除。最终健康与 HTTP 状态见 `task6-unit/health-final.json`。访问前端 [http://127.0.0.1:21108](http://127.0.0.1:21108)，后端健康 [http://127.0.0.1:21109/health](http://127.0.0.1:21109/health)。

自查修复共享夹具 sidecar 在切到 FalkorDB 后仍记录最初 Neo4j 连接的问题；实际工具 source 与断言一直来自真实冻结绑定。6 份旧 sidecar 已按持久会话绑定校正，保留 `agent-session-original-fixture.json` 和修正说明，未改写查询证据。

一次回归命令因 shell 展开显式文件而意外纳入旧 Agent 用例，发现后中断；该会话 `d1804e31-b15d-4655-acca-28da713c3c4d` 已通过 API 确认 `active_run=null`、`pending_feedback=null`，没有后台模型任务遗留。此中断运行不计入通过数。后续使用显式过滤文件清单完成 171 项单元回归。

### 回答逐事实复核与局限

| 回答 / 事实 | 与实际工具对照 | 结论 |
| --- | --- | --- |
| 两产品总数 2708 节点 / 5278 关系 | 实际服务器 count 与两份 Cora manifest 一致 | 数值通过；Cora 导入的对称边去重与方向规则仍以导入器说明为准 |
| 新参数过滤 label=0 → 298；工作区两倍 → 596 | 参数对象和原始导出匹配；工作区读取原始 JSON 实际计算 | 最终聚焦两项通过；失败轮不得计为参数通过 |
| FalkorDB 结构摘要 | describe 返回 sampled=true、truncated=true，答复摘要省略覆盖限制 | 回答完整性不足；不能把采样属性清单称全库完整 schema |
| 旧 Neo4j 第一轮表格将全库数量归于 Paper/LINKS_TO | 当轮只查全库 count，没有对应过滤计数 | **统计口径归属无证据**；独立实际数据数量相符不补足模型当轮推断 |
| 旧第二轮 x 非空 2708、长度 1433；label 的 0–6 分组 298/418/818/426/217/180/351 | 非空计数、min/max/avg 与分组工具结果支持 | 数值与覆盖通过；附加“特征维度”业务释义未经定义支持 |
| 旧第三轮每条 LINKS_TO 都有 _igraph_import、库内未发现业务定义 | 没有属性全覆盖统计，也没有业务定义搜索 | **属性普遍性和不存在性断言无证据** |
| 旧第四轮“应用仅允许查询不能证明账号只读” | 与工具执行边界一致，无写入权限探针 | 通过 |
| 初始 FalkorDB 失败轮声称参数化 | 实际反复字符串参数校验失败后改为字面量，导出 parameters={} | 明确失败，保留为可靠性/事实偏差证据；后续成功不抹除记录 |

前端卡片证据、Adapter 正确性、真实模型数值和模型解释质量分别结论。未验证云部署、其他厂商版本、任意 Cypher、集群/Sentinel/重定向。FalkorDB 停止仅保证客户端关闭与服务器有界期限，不保证立即终止服务器任务。全站 X-User-ID 防伪与默认模型质量不是本次修改范围。此前全仓 Python 基线在 graph-data `hiv.pt` 200/404 契约处失败（365 通过、6 跳过后停止）；本次未修复或声称全仓绿色。

# 历史记录：FalkorDB 协议实测（Task 1）

日期：2026-09-27。以下为 Task 1 当时的结论，当前交付状态以上表为准：**有条件的实现候选，尚未准入任意模型查询**。只读命令和部分服务器预算已经实测；单个 Cypher `range(1, 1000000000)` 在配置 8 MiB 查询内存上限、3 秒最大期限及 1 GiB 容器上限时仍使服务进程 OOM 重启。Task 4 必须完成下列限制和有界读取后，才能开放适配器。

## 固定环境与复现

- 官方 server 镜像：`falkordb/falkordb-server:v4.20.7@sha256:538fe1867a0a5dec55462eff5c1ea5edfc36246a9aa571cf1705b8b3f3d0f32c`，实测模块版本 `42007`，内置 Redis `8.10.2`。
- Python 客户端：`redis==8.1.0`（本地 `.venv` 与 agent-runtime requirements 固定）。直接使用 RESP2 `GRAPH.RO_QUERY`，测试的管理员连接只用于构造和核对夹具。
- 独立 Compose 项目：`igraph-falkordb-qa`；端口 `127.0.0.1:21188`，并接入 `igraph-backplane`，别名 `igraph-falkordb-qa`。密码来自 Compose secret 文件，没有写入仓库、文档或命令输出。
- 配置：`TIMEOUT_MAX=3000` 毫秒，`TIMEOUT_DEFAULT=1000` 毫秒，`RESULTSET_SIZE=128` 行，`QUERY_MEM_CAPACITY=8388608` 字节，容器内存 1 GiB。
- 可提交的真实 RESP2 帧：`tests/fixtures/falkordb_compact_path.resp2.json`。其中 Base64 编码的字节和 SHA-256 保留 `$` bulk 长度、`*` 数组数量、CRLF、UTF-8 原始编码，避免 Git 换行转换。专用测试在认证响应消费完毕后才读取 `GRAPH.RO_QUERY --compact` 回复；live 测试解码该文件并与新建测试图的服务器回复比较列和行。它不含密码或认证流量。另有本机 RESP 解码夹具 `output/multi-database-phase1/private/falkordb-protocol/*.json`（Git 忽略）；OOM 事件见同目录 `oom-events.txt`。

复现时自行建立权限为 `0600` 的密码文件，然后设置以下环境变量。密码通过文件读取，不放入 Compose YAML：

```bash
export IGRAPH_TEST_FALKORDB_PASSWORD_FILE=/path/to/private/password
docker compose -p igraph-falkordb-qa -f deploy/falkordb-test.compose.yaml up -d
export IGRAPH_TEST_FALKORDB_URI=redis://127.0.0.1:21188
export IGRAPH_TEST_FALKORDB_PASSWORD="$(cat "$IGRAPH_TEST_FALKORDB_PASSWORD_FILE")"
export IGRAPH_TEST_FALKORDB_GRAPH=igraph_admission
export IGRAPH_TEST_FALKORDB_EVIDENCE_DIR=output/multi-database-phase1/private/falkordb-protocol
.venv/bin/python -m pytest tests/test_falkordb_protocol.py --require-live -q
```

不设置三个必需的 `IGRAPH_TEST_FALKORDB_*` 连接变量时，`--require-live` 实际产生明确的九项 setup error；默认测试运行则跳过此专用服务。

## 协议结果

| 探针 | 实测结果与边界 |
| --- | --- |
| 可写账号只读路径 | 管理连接先创建节点；同一密码的被测连接通过 `GRAPH.RO_QUERY` 读到 1。`CREATE`、`SET`、`DELETE`、`CREATE INDEX` 均返回错误；每次管理连接复核节点数仍为 1，属性仍为原值。没有在失败时回退 `GRAPH.QUERY`。 |
| 参数与字符 | `CYPHER target=...` 绑定含中文、单引号、反斜杠的值，往返相等。参数序列化仍需 Task 4 实现和测试；本探针仅固定服务器协议语法。 |
| 空值、数值、实体 | 默认非 compact 响应中，空结果返回列名和空行集；`42` 为整数，`2.5` 与 `true` 是字符串，`null` 为 null；`9007199254740993` 保留精确整数。节点包含 `id`、labels、properties；边包含 `id`、type、`src_node`、`dest_node`、properties。非 compact 路径是形如 `[(1), [0], (2)]` 的字符串，信息不足。`--compact` 实测返回类型标签：整数 3、浮点 5、布尔 4、null 1、节点 8、边 7、路径 9；路径内含有序节点数组与边数组，边含源/目标 ID。`CALL db.labels()`、`db.relationshipTypes()`、`db.propertyKeys()` 可经 `GRAPH.RO_QUERY --compact` 获取 ID 映射。Task 4 应使用 compact 格式完整解码，不能把非 compact 路径文本作为最终结构。 |
| 结果行预算 | `UNWIND range(1, 100000) RETURN x` 实际只返回 128 行。一个单元格仍可返回 1,048,576 字节字符串；`collect` 可在一行中返回约 688,895 字节值。`RESULTSET_SIZE` **不是字节上限**。 |
| 查询内存 | `UNWIND range(1,300000) AS x RETURN collect(x)` 返回 `Query's mem consumption exceeded capacity`，图仍可读；但 10 亿元素 `range` 曾使 1 GiB 容器产生两次 Docker `oom`/`die`/`start` 事件。`QUERY_MEM_CAPACITY` 对某些聚合有效，**不能视为覆盖所有 Cypher 分配路径**。不得重复该危险压力探针作为常规测试。 |
| 服务器期限 | 两层 `UNWIND range(1,10000)` 聚合、命令 `TIMEOUT 1` 返回 `Query timed out`，约 2–4 毫秒内回到客户端；图仍在。配置读取证实 `TIMEOUT_MAX=3000`、`TIMEOUT_DEFAULT=1000`。 |
| 客户端断开 | 异步被测连接取消后，`CLIENT LIST` 中客户端 ID 消失；`GRAPH.INFO` 仍显示 1 个运行中查询，约 1 秒后归零。**断开连接不会立即取消服务器查询**；服务器期限提供有限的结束保证。 |
| 地址与连接 | 测试 URI 要求数字 IP；普通 `redis.Redis` 单连接模式查询前后 `CLIENT ID` 相同。该实验没有覆盖 TLS、DNS 名称或集群。`redis-py 8.1.0` 的 `Connection._connect()` 每次连接会对 `self.host` 调用 `getaddrinfo`，故 Task 4 应消费已校验的数字 IP，禁用集群/Sentinel/重定向，并单独保留原始域名作为 TLS SNI/证书主机名。其 `SSLConnection` 当前用 `self.host` 作为 `server_hostname`；直接把 host 改成 IP 会破坏域名证书校验。 |

## Task 4 准入条件

1. 模型查询只走 `GRAPH.RO_QUERY`，没有 `GRAPH.QUERY` 回退；保留写入、DDL、扩展入口负例。连接时核实目标模块版本与 `TIMEOUT_MAX`、`TIMEOUT_DEFAULT`、`RESULTSET_SIZE`、`QUERY_MEM_CAPACITY` 的**有效值**，预算未配置或超出适配器允许值就拒绝。每次请求设置不高于服务器最大值的 `TIMEOUT`，取消后关闭客户端连接，同时把服务器端终止表述为“在服务器期限内结束”。
2. 查询解析/允许范围必须排除 `range()` 及其他未验证的大量预分配函数；该限制需能区分字符串、注释、反引号，不能靠简单子串匹配。任意 Cypher 尚未获得内存安全准入。对 `collect` 等可能聚合成单个大值的查询，服务器内存上限与客户端响应上限都要生效。
3. **不能直接使用 `redis.Redis.execute_command` 获取模型查询的大响应。** 已安装的 redis-py RESP2 解析器对 `$<length>` 调用 `_buffer.read(int(response))`，对 `*<count>` 构建完整 Python 列表；`SocketBuffer` 又使用 `BytesIO` 缓冲。Task 4 需要在分配前检查 bulk 长度、数组数量、嵌套深度、累计原始字节和行数，限长 `recv` 并在超限时关闭连接；服务端行数限制之外再做客户端预算。可用受控命令打包和自有有界 RESP2 读取器，避免把任意 Redis 命令暴露给 Agent。
4. 目标地址应先按现有网络规则校验，固定实际拨号 IP；TLS 时另用原始主机名做 SNI 和证书校验。禁用自动发现、重定向与未经重新校验的重连。测试仅证明数字 IP 单连接，TLS 路径待 Task 4 验证。
5. 模型查询使用 `GRAPH.RO_QUERY ... --compact`，按 ValueType 标签显式解码节点、边、路径、布尔、浮点与大整数；通过只读 schema procedures 获取标签、边类型及属性 key 的 ID 映射。路径的节点顺序与边源/目标同时保留，schema 缓存失配须刷新或拒绝，不能默默降级为无类型字符串。

参考：[官方只读命令](https://docs.falkordb.com/commands/graph.ro-query)、[官方 compact 协议与映射](https://docs.falkordb.com/design/client-spec)、[官方查询与参数](https://docs.falkordb.com/commands/graph.query)、[服务器预算参数](https://docs.falkordb.com/getting-started/configuration)、[官方 server 镜像](https://hub.docker.com/r/falkordb/falkordb-server/tags)、[redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/)、[redis-py 取消行为](https://redis.io/docs/latest/develop/clients/redis-py/async/)。

## 历史记录：Task 4 实现后准入与产品限制（2026-09-27）

以下保留 Task 4 当时状态；当前表达式限制以本文 2026-09-28 最终审查修复及最终验收记录为准。

FalkorDB Adapter 已通过专用可写账号下的协议、安全、Cora 与边界夹具验收，并以 **experimental** 状态进入注册表。真实 Agent/浏览器验收由 Task 6 完成；这里不宣称整体产品已 verified。Neo4j 既有状态保持。

- 每次调用新建独占 socket；网络规则先解析并验证全部地址，实际连接固定数字 IP，TLS 使用原域名做 SNI/证书检查。无连接池、重试、重定向、Sentinel 或集群发现。TLS 测试使用本地 CA/真实握手，正确主机名成功、错误主机名失败。
- 异步 Adapter 使用 `asyncio.to_thread` 驱动独占阻塞 socket：自行逐帧、限长读取 RESP2，避免 redis-py 对任意响应预分配。读之前检查 bulk 长度、数组数量、嵌套深度、累计线长/帧字节与行数；所有 recv 不超过 4096 字节。协议超限失败整个查询，不把未完整读取的帧报告为完整结果。
- 连接时检查实际 `MODULE LIST` 版本为 graph 42007，并检查四个有效服务器配置均为正且不高于 Task 1 已测上限。ACL 账号必须允许这些只读配置/版本查询；无法核实预算就拒绝准入。
- 只允许专用 `GRAPH.RO_QUERY --compact`。函数采用 token 级白名单；明确禁止 `range`、`split`、未知函数/过程、动态/外部入口、变量长度路径和列表/模式推导的未验证扩展。列表推导按 `[变量 IN ...]` 的 token 绑定结构识别，即使省略投影 `|` 和 `WHERE` 也拒绝；普通列表、索引、字符串、注释和属性名不会仅因含 `IN` 而被拒绝。业务查询可自由组合允许的 Cypher 子集，任意 Cypher 尚未获准。聚合 `collect` 保留服务器内存限制和客户端帧预算两层保护。
- 实体元数据 ID 映射每次调用独立获取、无跨调用缓存；映射达到服务器行上限或出现未知 ID/类型就明确失败。compact 节点、边、路径、数组和标量保留类型；路径遍历顺序和边真实源/目标分别保留。未知 map/point 等类型尚未支持，绝不字符串化。超过 JavaScript 安全整数范围的普通值统一为 `{"type":"integer","value":"9007199254740993"}`，此契约也适用于 Neo4j；实体 ID 为精确字符串。
- schema 标签/关系名称来自元数据，属性从最多 32 个节点/关系采样，始终 `coverage.sampled=true`。查询达到服务器 128 行边界时保守 `truncated=true`；小于 128 的自定义服务器上限同理。工作区结果保存 source、原查询与参数、完整有界行；预览预算另计。
- 取消关闭当前调用 socket 并透传 `CancelledError`。实测客户端取消后服务器仍运行，约 1 秒后结束；不是服务器主动取消。每条查询 TIMEOUT 不大于剩余调用期限和有效 TIMEOUT_MAX，最大 3 秒。
- Cora 独立管理脚本复用原受限加载器和转换规则，输入 SHA-256 必须与可信 manifest 一致；2708 节点、5278 条去重边、标签分布及样本邻居已实际对照。拒绝非本批非空目标。FalkorDB 导入为独占目标下可重跑批次，不提供跨批原子事务；中断后以相同 hash 恢复。
- QA Compose 现有专用服务新增独立 `igraph-falkordb-qa_falkordb-qa-data` 持久卷和 AOF/RDB；没有删除任何已有卷。私有环境 `.env.falkordb-test` 与密码文件 `output/multi-database-phase1/private/falkordb-password` 均为 0600；复现时先 `set -a; source .env.falkordb-test; set +a`，不要打印变量。证据目录为 `output/multi-database-phase1/task4-falkordb/`，Task 1 原始证据保持。

运行：`.venv/bin/python -m pytest tests/test_falkordb_adapter.py tests/test_import_cora_falkordb.py -q`，以及加载私有测试环境后 `.venv/bin/python -m pytest tests/test_falkordb_integration.py --require-live -q`。管理导入入口为 `.venv/bin/python -m scripts.import_cora_falkordb --input cora.pt --manifest output/multi-database-phase1/task4-falkordb/cora-manifest.json --expected-sha256 <既有可信manifest的source_sha256>`。

## 2026-09-28 最终审查修复：隐式表达式与观察证据

最终审查发现 FalkorDB 4.20.7 的普通列表构造和重载 `+` 能在 RESP 读取之前复制堆值；以 16,384 次引用、128 KiB 参数组成的请求能通过旧策略，名义复制量为 2 GiB。此复现只在离线策略测试运行，未发送到数据库，也不声称实测 OOM。新表达式边界在连接前拒绝动态列表、全部 map 构造/投影、数组切片和未证明数字类型的 `+`。列表仅接受纯字面量，整条查询累计最多 128 个标量/嵌套列表项、4 层和 4096 UTF-8 字节字面量源码；括号/方括号嵌套最多 32 层。`+` 接受数字字面量、数字括号算式或显式 toFloat/toInteger 操作数。固定 MATCH 关系、单值索引、WHERE 参数过滤、普通 collect、既有元数据读取保留；collect 内外的构造同样受限。不能从属性名或常量别名推断类型；原来的动态常量别名列表也明确收窄。方言参考已同步，Agent loop、默认模型及按需加载机制不变。该边界不是通用 Cypher 解析器，也不声称所有 vendor 原生执行路径均已证明安全。

Memgraph disconnect_probe 现在保留首次观察和取消后的每次观察原文、成功状态及错误。仅成功且 rows/query 结构有效的观察能证明目标缺席；观察失败返回 status=unverified、server_stopped=false，running=null。这修复证据生成逻辑，不反推旧服务器未停止；Memgraph/Nebula 仍未准入。

本轮相关后端 **253 通过、1 个隔离 PG 环境跳过**；双库及协议 `--require-live` **26 通过、0 跳过**。慢查询只把原有四个数字属性改为显式 toFloat 后相加，数据规模、期限与取消断言保留；微型列表/索引及数字结果均实际验证。离线复核历史成功 Falkor 模型查询 **10 次调用、4 种文本，0 条新增拒绝**，未改历史证据，未重新运行模型/浏览器/前端全套。当前 igraph agent-runtime 已按现有双 Compose 文件 --no-deps 重建启动并 healthy；21108 与 21109/health 均 200，五份原始用户改动文件哈希保持。详细根因、来源、RED/GREEN、命令、部署记录、最终 SHA 及限制见 `output/multi-database-phase1/final-fix/report.md`；既有回答质量与全仓测试限制仍然有效。
