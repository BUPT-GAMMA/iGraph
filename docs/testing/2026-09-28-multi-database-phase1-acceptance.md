# 多数据库智能问图第一批独立验收

日期：2026-09-28（Asia/Shanghai）。实施范围：`7315ffb..284f24b`，分支 `feat/intelligent-graph-qa`。本记录由控制器复核实际证据后整理，不以实施计划的复选框或实现者自评替代验收。

## 分项结果

| 项目 | 状态 | 交付边界 |
| --- | --- | --- |
| Neo4j | 已交付 / verified | 保留原生只读分类与既有连接流程；真实查询、参数、结果导出、工作区计算和本地图保留通过。 |
| FalkorDB | 已交付 / verified | 固定 4.20.7；应用使用 GRAPH.RO_QUERY、有界 RESP2 解码、每调用独占连接、已校验地址与服务器预算。限制函数/过程和语言构造；不是任意 Cypher 支持承诺。 |
| Memgraph | 技术验证完成；产品接入未准入 | 原生参数、类型、结构、期限与一次主动取消通过；当前 EXPLAIN 摘要分类不能区分允许读取与写入，原始客户端未实现读取前字节预算。未注册到产品。 |
| NebulaGraph | 技术验证完成；产品接入未准入 | 原生参数、类型、结构与并发通过；完整副作用分类未解决，服务器期限与主动取消未验证，原始客户端字节预算失败。未注册到产品。 |
| 模型回答证据质量 | 未完全通过 | 无证据统计口径、业务含义及普遍性断言，FalkorDB 采样覆盖限制遗漏仍存在；不冒充 Adapter 或数值验证已解决自由表述质量。 |

两项技术验证不准入是已完成探针的独立结论。因均未满足进入产品实现的前置条件，本轮没有编造后续 Adapter 实施计划，也没有降低安全标准开放入口。分产品版本、命令、失败/未验证区别及再次准入条件见[技术验证报告](2026-09-27-memgraph-nebula-feasibility.md)。

## 独立审查与验证

Task 1–7 均完成独立规格/质量审查；发现的问题修复后完成范围复审。Task 7 的三项探针健壮性问题在 `2d50759` 修复，复审全部 ADDRESSED、无新增问题。全分支审查 `7315ffb..2d50759` 发现 1 项 Important（FalkorDB 隐式堆构造）及 1 项 Minor（Memgraph 断连观察）。两项在 `284f24b` 集中修复，范围复审：**已通过，两项均 ADDRESSED，无新增 Critical/Important**（[复审报告](../../output/multi-database-phase1/final-fix/scoped-review.md)）。[完整审查](../../output/multi-database-phase1/final-review.md)、[修复报告](../../output/multi-database-phase1/final-fix/report.md)。审查的 15 项未作判断范围已有[逐项处置](../../output/multi-database-phase1/final-controller/review-scope-adjudication.md)，未静默当作通过。

| 验证 | 实际结果 | 原始证据 |
| --- | --- | --- |
| 最终修复后相关后端回归 | 253 通过、1 跳过；跳过项要求隔离 PostgreSQL URL | [日志](../../output/multi-database-phase1/final-fix/backend.log)；确切命令见修复报告 |
| 最终修复后真实数据库回归 | 26 通过、零跳过，显式 --require-live；Neo4j 7、FalkorDB Adapter 10、协议 9 | [日志](../../output/multi-database-phase1/final-fix/live.log)，同目录 live-neo4j/、live-falkordb/ |
| 最终两项修复的聚焦回归 | 126 通过；包含危险形状只在策略层测试、允许子集及失败/畸形观测；无危险 live 查询 | [日志](../../output/multi-database-phase1/final-fix/green-final.log) |
| Task 7 探针判定回归 | 37 通过；修复前 30 失败 / 7 通过；未重复压力负载 | [RED](../../output/multi-database-phase1/task7-review-fix/red.log)、[GREEN](../../output/multi-database-phase1/task7-review-fix/green-final.log) |
| NebulaGraph 认证连接修复 | 正确认证成功、错误密码拒绝、连接关闭无清理警告；没有查询/写入 | [认证证据](../../output/multi-database-phase1/task7-review-fix/auth-only.json)、[日志](../../output/multi-database-phase1/task7-review-fix/auth-only.log) |
| 探针证据回放 | 四份报告、40 个控制状态及准入矩阵不变；历史正控仅保存 changed 标志、没有原始前后快照，不能重新证明未保留观测 | [回放](../../output/multi-database-phase1/task7-review-fix/replay.json) |
| 全前端回归与构建（Task 6） | 61 文件 / 576 测试通过；TypeScript / Vite 通过；之后无前端源码变更 | [测试](../../output/multi-database-phase1/task6-regression/frontend.log)、[构建](../../output/multi-database-phase1/task6-regression/build.log) |
| 真实 PostgreSQL 迁移（Task 2） | 隔离 schema 两线程并发和幂等性通过；另比对 31 条现有连接 ID、revision、密文指纹不变；独立探针不记作跳过 pytest 的通过 | [探针](../../output/multi-database-phase1/postgres-concurrent-migration-probe.py)、[日志](../../output/multi-database-phase1/postgres-concurrent-migration-probe.log) |
| 真实 Agent 与浏览器（Task 6） | 旧 Agent 2、旧/新浏览器各 1 通过。新增 Agent 分阶段：结构/计数 2 项早期通过，参数/工作区/拒绝 2 项最终通过；当前检查器另复核 4 案例 / 10 轮。不是一次最终 4 项全绿运行 | [完整交接及逐轮证据](2026-09-27-multi-database-phase1-handoff.md#task-6方言与真实端到端) |
| 参数行为与原始结果 | 两库 label=0 得到 298，工作区实际计算 596；保持原查询换 label=1 得到 418，证明参数参与过滤。原始 artifact 与发布副本完整相等 | [参数复核](../../output/multi-database-phase1/task6-review-fix/parameter-replay.json)、[JSON 比对](../../output/multi-database-phase1/task6-agent-final/artifact-comparison.json) |

控制器修复前另独立运行了 175 项相关后端测试（1 个环境跳过）和 26 项真实数据库测试；最新修复后日志逐项核对，并核实容器实际文件与工作区相同。此前 10 次成功 FalkorDB 工具调用的 4 条唯一查询在新策略下全部通过离线准入复核，没有改写历史证据或新增模型调用：[查询兼容检查](../../output/multi-database-phase1/final-fix/historical-policy.json)。

控制器还查看了真实工具记录与三张浏览器截图；没有用模型声称替代工具结果。当前配置模型仍为 qwen3.6-plus，既有 Agent loop、拒绝非法参数行为与默认模型未替换。

本轮没有全仓 Python 绿色结论：早期完整回归在既有 graph-data `hiv.pt` 契约断言（预期 200、实际 404）处停止，此前 365 通过、6 跳过；未查明根因、未修改无关模块。当前相关后端及真实数据库回归见上表。

## 本地服务与工作区

最新产品代码在 Task 6 已按当前 `igraph` 项目的 `compose.yaml + compose.dev.yaml` 重建并启动 agent-runtime/backend/frontend；最终审查后的 FalkorDB 策略修复又单独重建启动受影响的 agent-runtime，容器内策略代码和方言说明 SHA256 与工作区一致。新探针 Compose 项目已独立启动并完成重复观测，未删除既有数据卷。

- 前端：[http://127.0.0.1:21108](http://127.0.0.1:21108)
- 后端健康：[http://127.0.0.1:21109/health](http://127.0.0.1:21109/health)
- 专用数据库端口：Neo4j Bolt 21187 / HTTP 21174；FalkorDB 21188；Memgraph 21189；NebulaGraph 21190，均本机测试环境。
- 控制器观察到 15 个相关容器运行、已配置的健康检查均健康，两个访问地址 HTTP 200；原有 5 个已修改文件 SHA256 不变。[健康与保存检查](../../output/multi-database-phase1/final-controller/health-after-final-fix.json)
- 原有未跟踪文件保留，只提交本任务明确文件。不合并、不推送，保留当前分支和工作区。凭据仅存被忽略的本机私密文件，报告没有凭据值。

## 执行中裁决及代价

以下按执行顺序记录 ledger 中全部七条 Ruling；[原始记录](../../output/multi-database-phase1/final-controller/rulings-verbatim.md)保留原文。

1. 在现有特性分支和工作区执行，保留开发覆盖配置并更新当前 Compose；误判代价是分支隔离较弱，以原始 diff/哈希和精确暂存缓解。
2. 看到 FalkorDB 大 cell 与 OOM 证据后，仅在读取前协议预算、固定 IP/TLS 主机名、服务器期限与保守函数集合成立时准入；误判代价是源数据库资源耗尽，已用限制和实测收窄范围，未重演危险负载。
3. 将两个请求真实重叠时只取消一个的测试视为原始隔离要求，先补覆盖再关闭 Task 3；误判代价是少量额外测试工作。
4. 局部修正远程工具的 Agno 参数 schema，不改运行时对象契约、模型或全局提示；复用已配置专用测试用户配独立会话，并在 Docker 运行浏览器。误判代价是增加局部 SDK 兼容维护，且同用户独立会话不等于新身份隔离证明。
5. 允许既有 Agent loop 在被拒绝的错误只读调用后改正成功，保留失败证据；参数、来源、数值和拒写门槛不降低。误判代价是增加失败调用和延迟，不能当作首次成功。
6. 仅对两个远程工具的可选对象/数组广告省略 nullable anyOf，保留运行时 None 与非法字符串拒绝，做一次受控兼容实验；当前配置模型已实测改善。误判代价是其他模型兼容性未证明、需要维护局部注册逻辑。

7. 最终审查发现隐式构造也可绕过及时的服务器内存拦截，因此将 FalkorDB 列表构造收窄到有界纯字面量，并只允许可证明的数字加法；动态列表/map 构造、切片和未定型拼接提前拒绝。字面量全查询限 128 项、4 层、4096 UTF-8 源码字节，括号总深度 32；数字加法需字面量、数字括号或显式转换证明。误判代价是部分原生查询需要改写或转到工作区计算；原始失败/成功证据保留，新的兼容范围单列。

## 剩余限制

- Memgraph/NebulaGraph 产品接入仍未实现；准入缺口和重新验证条件以分产品报告为准，不能写成“数据库本身不支持”。
- FalkorDB 只保证客户端关闭和有界服务器期限，不保证断开立即终止服务器查询；固定版本、服务器配置和支持的原生查询范围是交付条件。
- 云环境、其他版本、集群、Sentinel/重定向、跨库联邦查询不在本次已验证覆盖中；后两项探针 TLS/DNS 重绑定/发现机制未验证。
- Neo4j 保留既有 driver 路径，未获得与 FalkorDB RESP 实现等同的新单值读取前内存保证；没有开展全站 HTTP 输入内存与身份机制加固。
- 回答证据质量与全仓既有失败仍未解决。没有增加答案核验器、无限提示词迭代或更换模型来掩盖问题。
