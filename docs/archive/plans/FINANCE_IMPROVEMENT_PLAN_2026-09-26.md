# iGraph 金融股权分析改进方案

> 仓库发布说明：服务器连接信息已替换为占位符。文中 `artifacts/` 为本地保留的验收证据，不随 Git 提交；原始 CSV、PPT 和 citics 参考包也属于外部输入。仓库提供实现、测试及验收摘要；真实数据测试需设置 `IGRAPH_FINANCE_TEST_DATA`。

日期：2026-09-26。范围为设计和验收方案，不包含本次新增应用实现。依据为 `tasks.pptx`、实际三份 CSV、当前 iGraph 代码审计及 citics 0.5.0 实测。**下文的业务投影、专用工具、API 和界面均为拟新增能力；citics 原型可复用不表示 iGraph 已经具备这些能力。**

依据文件：需求与数据基线（本地证据：`artifacts/finance-validation/requirements-and-data.md`）、当前能力审计（本地证据：`artifacts/finance-validation/capability-audit.md`）、参考算法审计（本地证据：`artifacts/finance-validation/citics-audit.md`）、独立算术结果（本地证据：`artifacts/finance-validation/independent-oracles.json`）。服务器部署、浏览器上传和聊天调用的实测结论见[完整验证报告](../reports/FINANCE_REQUIREMENTS_VALIDATION_2026-09-26.md)。

## 0. 先修复本次实测阻塞：全图工具输出和失败恢复

本次原生会话在两个工具已完成后，以模型HTTP400结束：单字符串28,049,408字符超过供应商28,000,000限制。恢复接口实际返回106,245,421字节，同一整图同时保存在tools和processItems。期间两次数据集授权请求触发4秒超时。因此需将以下P0放在金融功能上线之前；仅新增三个金融算法仍不足以保证端到端可用。

1. **有界工具响应与文件计算**：`get_constructed_graph`默认只返回版本、schema、统计和有字节上限的样例；增加按业务ID/类型/属性的查询、分页以及会话授权的artifact导出。完整图写入工作区或对象存储，模型只拿引用，计算在服务或沙箱内完成。对所有工具设响应字节上限，不能只按节点数限量。
2. **避免多份全文和日志放大**：日志保存统计、哈希与artifact引用；工具持久化、processItems、恢复消息共享同一结果引用，避免重复嵌入。运行上下文压缩和供应商请求大小检查必须覆盖当前轮巨型工具结果，超限时保留证据并返回可操作错误。
3. **授权和运行状态保持轻量**：数据集scope校验只取用户/项目/活动数据集/版本所需状态，不加载完整run内容；run终态查询不依赖巨型历史。保留当前严格权限检查，不用扩大超时或绕过授权掩盖问题。
4. **SSE与失败恢复**：工具完成但模型失败时，前端明确显示失败及可重试范围；超大事件改为artifact引用。重连恢复只拉小型状态和必要内容，禁止为显示一次错误下载两份整图。

5. **修复工作区脚本校验并验证取消/重启恢复**：本轮补测有5次写脚本命令因Python除号`/`被当作绝对路径而遭到执行前拒绝，另1次`/dev/stdin`使用确实不在允许列表中。按shell命令结构区分参数、源码、读路径和写目标，保留真实沙箱边界及Uploaded只读约束，不通过编码隐藏源码或关闭校验解决。为Python文件写入提供受约束的专用接口；临时诊断可经正常上传入口提交审阅后的脚本，再按文档运行并发布产物，但此路线本轮未验证。当前实例取消接口返回`RUN_CANCEL_UNAVAILABLE`；应完成取消协议验收后统一启用相关开关，并验证进程重启后将遗留running归并为有解释的中断终态。此次仅重启隔离Agent服务结束重复调用，持久化状态仍为running。

建议验收：用本次53MB图重放原始提问，禁止将整图加入模型请求；工具常规响应、单个SSE事件、run终态快照均按明确字节预算验收；在模型错误、观察端断线、并发授权时正确恢复终态。新增除法、字符串、heredoc、多层引号、只读Uploaded和真实越界写入的校验回归，并实际测试运行取消与进程重启。详细代码链路、已证实事实与待验证原因见运行故障诊断（本地证据：`artifacts/finance-validation/runtime-findings.md`）和工作区补测诊断（本地证据：`artifacts/finance-validation/custom-workspace-findings.md`）。这些是待实施修改，本轮未修改应用代码。

## 1. 改进目标与规则边界

目标是在现有“上传→构图→项目/会话→分析”的流程中，增加可复用的金融主体投影和三个业务操作，输出可核验的第一大股东、控股链和集团树。继续保留原始关系构图及原始文件，以便从结果回到输入记录。

三个操作不能互相替代：第一大股东是规则选出的起始锚点；控股链按可支配表决权严格 `>50%` 推导；集团树是控制关系的展示投影。经济权益、最短路径、普通祖先后代和 PPR/社区结果不能直接生成确认控制关系。

PPT 已给出规则优先级：适用的 GP/法人规则→直接过半→完整累计根权益排名。但以下内容没有足够的数据或可执行定义，不能由系统臆断：

- “GP/法人”的适用企业类型、GP 与法人同时存在时的优先顺序、有效期与权威身份映射。现有 `LGL_RPRT` 是姓名；29 个重名导致290家企业对应多个候选个人ID，不能直接按姓名合并。GP关系未提供。
- 注销终止点能否作为候选、吊销如何处理，中登/港结算名单与排除范围，政府主体层级与停止条件，海航/中信国安等特殊集团的具体断开规则。
- 多来源同一持股事实的冲突裁决规则，包括是否采用“来源记录中的最大比例”。最大比例策略可以配置，不能默认为重复行相加或最大值必然正确。

这些规则应进入经版本化的规则包。未配置时可以执行明确声明的 `equity_only` 分析，返回股权候选；对可能影响最终选择的规则缺失，返回 `RULE_DATA_MISSING` / `RULE_CONFIG_MISSING`，不能将其包装为已完成全部业务规则的确定结论。基础工程与股权算法可以先完成，不必等待所有特殊规则。

## 2. 数据契约：原始记录与业务图并存

### 2.1 统一主体与多态外键

新增 `ownership_snapshot.v1` 投影，来源仍是当前三表上传的 `csv_bundle.v1`。初始关系图中的19,436个行节点与26,872条外键边可作溯源；算法读取的业务图应有6,000个主体、13,436条有向持股边，包括88个孤立主体。

| 对象 | 建议字段 | 约束 |
| --- | --- | --- |
| `Entity` | `entity_id, entity_type, business_id, name, operating_status, source_refs` | `entity_id` 由类型+业务ID稳定生成；机构/个人不因ID文本相同而合并，姓名仅供检索 |
| `OwnershipFact` | `fact_id, snapshot_id, holder_entity_id, investee_entity_id, ratio_raw, ratio_decimal, holder_type, source_refs, valid_from, valid_to` | 股东→被投机构；保留原始比例、源文件SHA-256、表名、原始行号/记录ID；快照来源日期不能混用 |
| `RuleRelation` | `relation_id, relation_type, subject_id, object_id, effective_range, authority, evidence_refs` | 用于权威GP/法人及其他协议关系；缺失不推造，不能把普通法人任职自动当作控制边 |
| `SnapshotManifest` | `snapshot_id, input_hashes, schema_version, projection_version, quality_report_id, counts` | 同一版本输入内容不可原地覆盖；可追溯到三份源文件 |
| `IdentityMap` | `source_table, source_id, entity_id, match_method, confidence, status` | 保存原始row ID→业务主体ID映射，避免聊天把哈希row ID当企业业务ID |

股东多态外键按 `REL_SHRHLR_TP_CODE` 选择目标表：`0→institution_main.UUID`，`1→individual_info.IDV_ID`。不应向两张父表各建一个必需外键，再把合法的另一类型计为 dangling。被投企业 `UUID` 始终引用机构表。

个人优先使用权威 `IDV_ID`，MD5仅作为有唯一约束的辅助映射。若同一MD5映射多个ID、姓名重名或类型矛盾，应输出身份冲突；不要选第一条。类型、业务ID、快照参与连接，不能按显示名称合并。

### 2.2 比例与重复事实

- 为本次来源显式配置 `ratio_format="percent_string"`：`57.1429%→"0.571429"`。其他来源的 `fraction` / `percent_number` 单独配置；不要靠“大于1才除100”猜测 `1` 究竟是1%还是100%。
- 原始值和规范比例都保存为字符串；解析和控制阈值比较使用 `Decimal`，经济累计使用有明确精度的十进制运算，API返回十进制字符串。建议初始有效精度50位，输出显示单独舍入；不能在每跳乘加时提前保留两位小数。
- 稳定 `fact_id` 使用规范化业务事实键、快照和权益类别；本测试表可用快照+股东ID+被投ID。各来源记录另有独立 `source_record_id`，通过 `business_fact_key` 关联到同一事实，才能区分“不同来源抄录”与“不同权益类别”。导入前验证唯一性。文件行号用于定位，不能作为跨重排唯一标识。
- 完全重复事实按稳定身份去重并计数。合法的不同权益类别/份额需有独立ID及可累加依据；不能把相同快照的多来源抄录当成多份权益相加。
- 冲突策略建议支持 `reject_conflict`、`prefer_authoritative_source`、`latest_valid_record`、`max_ratio_per_business_fact`。最后一种仅在业务确认其来源含义时启用，同时保留被淘汰记录、策略版本和选值原因；不得对不同真实持股类别盲目取最大值。

### 2.3 质量闸门与覆盖率

质量报告同时产生行级问题、主体级问题和快照汇总，不静默跳过坏行，不自动把超过100%的比例归一化。

| 检查 | 处理 | 本次数据验收 |
| --- | --- | --- |
| 表间统计日期/有效期混杂 | 阻止生成可分析快照，要求先显式筛选 | 三表全部为2026-09-23 |
| 缺必需字段、无法解析比例、比例<0或>1、未知类型、无法解析主体 | 保留原记录，标记拒收/冲突；严格投影不能声称完整 | 本次无此类记录 |
| 重复事实/冲突来源 | 按明确策略处理并审计；无策略不确认受影响结论 | 本次无重复股东—企业对 |
| 某企业已记录股比和>1 | 标记 `OWNERSHIP_TOTAL_EXCEEDED`，阻止依赖该企业的最终确认；仍可输出诊断数值 | 恰13家，最高1.23 |
| 已记录股比和<1、没有记录股东 | 标记覆盖率/未知上游；不把缺资料节点称为现实最终根 | 4,396家<1，591家机构无已记录股东 |
| 注销/吊销/特殊主体 | 应用规则包的停止/排除策略并保留原因 | 注销275、吊销42；停不停止取决于显式规则 |
| 缺GP/法人权威映射 | `RULE_DATA_MISSING`；允许另列equity_only候选 | 不按姓名强配ID |

已知60%持股且总比例合法时，剩余40%的股东身份未知并不自动否定这条严格控制关系；但“全图最终根”“现实实控人”“所有集团成员已完整”是更强结论，必须分别描述。算法完整性、源数据覆盖率和业务规则完整性使用不同字段。

## 3. citics 复用边界及必修项

| 可复用构件 | 移植前修改 | 证据 |
| --- | --- | --- |
| `io.load_enterprise_tables_snapshot` | 接入统一主体、多态FK、显式比例格式、流式导入、质量报告、权威身份映射 | `io.py:39`；当前 `:36` 全表list，`:105` 无效行静默跳过 |
| `_aggregate_root_interests` / `first_holder_result` | 去除未声明的默认15%过滤；首大完整排名与Top-K独立；补GP/法人和特殊规则；并列状态统一 | `engine.py:1286、1420`；默认15%并非PPT的明确要求 |
| `compute_control_closure` | Decimal阈值、事实去重、质量/停止规则、每个受控持有人的投票贡献只计一次 | `engine.py:525`；重复30%两行会变成60%确认控制 |
| SCC与目标经济方程 | 无环/SCC共享有效边策略；组件边索引替代逐组件全边扫描；记录残差、条件数/误差界和资源状态 | `graph.py:30`、`engine.py:329、366` |
| `control_graph_result` | 修复成员预算漏报，分开控制判断与展示范围完整性 | `engine.py:622、695、817`；A→B→C只返回A/B却complete=true已复现 |
| 派系和证据分表 | 只有满足完整规则且质量通过的唯一严格控制结果才能填primary；增加集团树投影 | `engine.py:949`、`result_store.py:58` |

另须将默认 `FINAL_LOCAL_RESULT` 改为保守 `REVIEW_CANDIDATE`。若需要保留旧版局部决策，应以显式兼容规则返回 `local_candidate`，保留截断状态，不能写成全图确定第一大股东。实际 `000072` 的穿注销推断、`000621/000901` 的超比例确认必须由规则和质量闸门纠正。

44项既有测试通过可作为迁移起点，不是完整验收。新增测试必须覆盖已复现的默认截断确认、成员截断漏报、重复事实、总比例超限和规则缺失。

## 4. 算法流程与集团树语义

### 4.1 完整累计与SCC

1. 根据不可变快照和规则版本建立有效持股正向/反向索引。0比例边不参与持股可达性和SCC，但保留为原始事实；过滤、终止节点及原因可回查。
2. 非递归求SCC及condensation DAG，保留组件成员、内部原始边和跨组件成员端点。结构缩点不把多个法律主体变成一个控制人。
3. 无环目标祖先图使用逆拓扑动态规划累计。令目标边界贡献为1，每个上游节点汇总其所有出边 `ratio × downstream_interest`；同一节点的共享后缀只计算一次，多个分支贡献均累加。对有效图根节点比较累计值，保留全部并列；不需枚举所有路径再排名。
4. SCC按组件DAG逆拓扑构造 `(I-A)s=d`，求成员级权益。小组件可复用原型并改造数值审计；大组件进入稀疏离线求解，在线返回 `TOO_LARGE` / `PENDING`。不收敛、奇异、依赖失败均不产生确定首大。目标本身位于SCC时的终端/回流计数约定也须绑定规则版本，不能与无环简单路径值混称。
5. 只有完整累计后才生成排名；Top-K另外枚举不重复节点的展示路径，包含原始边、逐边比例、乘积、稳定排序、是否还有未展示路径。`evidence_complete=false` 可以和 `ranking_complete=true` 并存。

预算到期时可以返回已计算下界、当前候选及未探索边界，但不能因为队列停止就认定当前第一名。当前方案不引入复杂的上下界排名认证：未完整计算一律保持 `TRUNCATED`；需要精确结果时转异步继续计算。旧5/15层和30条路径属于显式兼容/展示配置，不是完整累计的默认事实删除规则。

### 4.2 严格控制闭包

初始化每个股东的直接可支配比例；仅当严格大于0.5时确认一对控制关系。每发现 `u控制v`，把v对其他企业的直接持股表决权贡献加入u，继续到固定点；每个 `(u,受控持有人v)` 只传播一次，按来源事实保留贡献，避免菱形结构重复计票。

例如 A控制X/Y，X/Y各持T30%，A可以通过两家受控主体合计60%控制T。返回的是分支证明DAG，不能伪造一条“每跳均>50%”的原始持股链。经济权益仍单独计算，不能把控制后的全部表决权当作比例乘积。

SCC涉及的硬控制按PPT保留 `SCC_REVIEW`；不通过求出经济权益就升级为确定控制。多个互不控制的过半主体应报数据/控制冲突，保留全部候选。

将目标上游控制判断与集团向下展开分为两次预算：前者完整时可以确认当前目标的控制根，后者截断只影响集团成员完整性，避免000044仅为查看下游就扩到4,096节点并污染控制判断状态。

### 4.3 集团DAG与树形展示

业务存储保留控制DAG、根→成员的关系和贡献证据。展示树可以选一个稳定主父节点，但必须返回其余父边/贡献分支 `additional_edges`，并能切回DAG；不得为了成树删除事实、并列根或SCC。

`CONTROL`集团只纳入严格控制成员；`PATH`与`AFFINITY`可在独立视图发现重叠候选，不混入确认成员。第一大股东锚点树若需兼容旧系统，单独标为 `FIRST_HOLDER_ANCHOR`，不复用 `CONTROL` 的集团ID和确认语义。

本次集团验收根000001的确认树为根→000040→000044，恰3个节点。000001=>000044派生控制边另外保存，其证据回放到两条原始持股边。不能把该根的所有弱持股可达节点都纳入集团。

## 5. 三个专用工具及API契约（拟新增）

在当前Graph Data Service中实现金融计算层，在Graph Engine注册工具并添加策略说明；BFF提供用户授权范围内的公开接口，前端/聊天共用同一实现。不要让模型临时编写一份算法、UI再使用另一份算法。

建议工具签名：

```python
finance_first_holder(
    graph_id: str, snapshot_id: str, target_id: str, rule_version: str,
    budget: CalculationBudget, evidence_top_k: int = 30
) -> FirstHolderResult | AnalysisJob

finance_control_chain(
    graph_id: str, snapshot_id: str, target_id: str, rule_version: str,
    budget: CalculationBudget, include_contribution_evidence: bool = True
) -> ControlResult | AnalysisJob

finance_group_tree(
    graph_id: str, snapshot_id: str, root_id: str, rule_version: str,
    budget: CalculationBudget, view: str = "CONTROL", display_limit: int = 500,
    cursor: str | None = None
) -> GroupTreeResult | AnalysisJob
```

`CalculationBudget` 含节点/边/时间/内存上限和可选深度限制；`evidence_top_k`与`display_limit`不改变业务排名或成员归属。已确认成员分页读取物化结果；未计算完的成员范围必须标记不完整。`target_id/root_id` 是稳定业务主体ID，服务端解析内部图节点，不要求用户输入row哈希。

建议公开接口均按已授权项目/图校验快照归属：

```text
POST /api/graphs/{graph_id}/finance/first-holder
POST /api/graphs/{graph_id}/finance/control-chain
POST /api/graphs/{graph_id}/finance/group-tree
GET  /api/finance/jobs/{job_id}
POST /api/finance/jobs/{job_id}/cancel
GET  /api/finance/results/{result_id}/members?cursor=...
GET  /api/finance/results/{result_id}/evidence?cursor=...
```

POST请求携带 `snapshot_id, rule_version, target_id/root_id, budget, options, request_id`。同一request_id重复请求返回原作业；异步接受为202，并可取消。精确结果缓存键至少包含输入快照哈希、投影版本、算法版本、规则版本和计算口径；被截断结果不得命中完整结果缓存。

建议统一返回骨架：

```json
{
  "result_id": "...",
  "graph_id": "...",
  "snapshot_id": "2026-09-23",
  "algorithm_version": "finance-1.0",
  "rule_version": "equity-only-v1",
  "input_manifest_hash": "...",
  "execution_status": "SUCCEEDED",
  "business_status": "TIED_CANDIDATES",
  "data_quality_status": "VALID_WITH_COVERAGE_GAPS",
  "rule_status": "COMPLETE_FOR_SELECTED_POLICY",
  "completeness": {
    "ranking": true,
    "target_control": true,
    "group_members": null,
    "display_evidence": false
  },
  "candidates": [],
  "confirmed_id": null,
  "primary_faction_id": null,
  "warnings": [],
  "budget_used": {},
  "evidence_ref": "..."
}
```

| 操作 | 主要输出 | 业务状态 |
| --- | --- | --- |
| 首大 | `rule_applied, direct_candidates, root_interests, first_holder_ids, first_holder_id, path_count, stop_reasons` | `COMPLETE / TIED_CANDIDATES / NO_EVIDENCE / DATA_CONFLICT / RULE_DATA_MISSING / RULE_CONFIG_MISSING / TRUNCATED / NON_CONVERGENT / TOO_LARGE / DEPENDENCY_FAILED` |
| 控股链 | `ultimate_controller_ids, intermediate_controller_ids, controllable_voting_stake, control_edges, chains, contribution_evidence` | `CONFIRMED_CONTROL / NO_SINGLE_CONTROLLER / TIED_CANDIDATES / SCC_REVIEW / DATA_CONFLICT / TRUNCATED`，并携带独立rule/quality状态 |
| 集团树 | `faction_type, root_ids, members, tree_edges, additional_edges, control_dag_ref, memberships, next_cursor` | `IDENTIFIED_CONTROL_FACTION / MULTIPLE_CANDIDATES / SCC_REVIEW / TRUNCATED / CANDIDATE_ONLY / DATA_CONFLICT` |

`SUCCEEDED`只表示计算执行结束，不能替代业务确认。`primary_faction_id`仅在选定规则完整、依赖数据质量通过、目标控制完整且唯一严格控制根时填写；成员视图分页不会改变集团ID，但成员计算未完时必须显示不完整。股权候选可有稳定候选ID，不能占用确认primary字段。所有金额/比例输出使用十进制字符串。

## 6. 前端与聊天证据界面

1. **构图向导**：提供“机构+个人+股东关系”模板，预览股东→企业方向、百分数格式、多态引用、孤立主体和实际数量；显示13家比例冲突及GP/法人数据缺失，再选择规则版本。保存用户选择供重放。
2. **三个业务入口**：公司名/业务ID检索后分别查看第一大股东、控股链、集团树。页面始终显示快照、算法/规则版本、计算完整性、源资料覆盖率及冲突，不把“运行成功”显示成“业务已确认”。
3. **首大证据**：先显示采用哪条优先规则，再列直接股东、全部根累计排名、并列、停止原因和Top-K路径。改变Top-K时确认排名不变；未知规则清楚标“仅股权口径候选”。
4. **链与树证据**：层级树/DAG切换；每条派生边可展开贡献股东、原始边ID、直接比例与可支配表决权；SCC可展开成员及方程/残差。注销、特殊主体的边界可点击解释。PATH/AFFINITY分别显示，不能用相同颜色暗示已确认控制。
5. **导出与聊天**：JSON/CSV携带原始ID、状态、版本与证据索引；聊天仅解释专用工具返回的结构化结果，遇到规则缺失或截断必须说明。下载表和页面使用同一result_id，防止版本错配。

## 7. 分阶段里程碑与量化验收

以下为建议交付顺序和验收门槛，不是已经实现或测得的性能。每阶段保留独立报告；应用代码实施后按项目约定重建并启动受影响Compose服务，检查健康状态和SSH隧道本地访问。

| 阶段 | 交付物 | 必须通过的门槛 |
| --- | --- | --- |
| M0 规则与契约冻结 | 版本化schema、equity_only规则包、特殊规则配置结构、固定样例 | PPT17页要求逐项映射；GP/法人及特殊主体未定项全部列出；不得新增姓名自动认定规则 |
| M1 数据投影和质量 | 三表模板、主体注册表、多态FK、Decimal比例、quality report | 6,000主体/13,436边、8,975机构边/4,461个人边、88孤点；无伪dangling；13家超限准确；三表日期一致；改变CSV行顺序后规范化投影与业务结果语义哈希不变，源文件及溯源manifest哈希允许变化 |
| M2 金融计算内核 | SCC、完整累计DP、控制固定点、证据存储、3工具及异步接口 | 5,980组件/10个非平凡SCC；真实A–F样例与合成矩阵全部通过；44项参考测试迁移后语义兼容或有版本化调整说明；不保留已知完整性缺陷 |
| M3 产品流程验收 | 首大/控制链/树界面、规则选择、证据回放、导出 | 从隧道浏览器上传到分析全过程；3个业务入口均能复核000044；root000001集团恰3节点；工具、页面、导出结果与同一oracle一致；全部确认边可回放来源 |
| M4 工程化与规模 | 组件边索引、缓存、流式/分片、离线作业、增量失效 | 先通过本次6k/13k基线，再用10万/100万边合成图压测；未通过更大规模前不宣称千万节点可用 |

本次测试集建议性能目标：在固定并记录的服务器CPU、内存、镜像版本下，三表投影≤60秒；缓存热查询P95≤3秒（≥30个预先固定目标、至少3轮，另报冷查询）；同步请求10秒到预算则明确转异步/截断。这些数值是初始验收目标，若需调整应记录原因，不以删节点/删小比例边换取“达标”。同时报告峰值RSS、CPU时间、缓存命中率、截断率和返回证据数量。

增量更新需绑定新快照，只失效受影响SCC/组件祖先及控制传播范围的缓存；旧result_id继续指向旧快照，不原地改写历史结论。

## 8. 可执行测试矩阵

### 8.1 当前已可运行的只读基线

以下命令只生成验证产物，不表示专用API已实现。在项目根目录执行：

```powershell
python -B artifacts/finance-validation/profile_source_data.py
python -B artifacts/finance-validation/citics_reference_audit.py
python -B -c "import runpy; runpy.run_path('artifacts/finance-validation/micro_graph_semantics.py')"
```

参考代码回归在 `F:\mypaper\data\finance\citics\citics` 下执行：

```powershell
python -B -m unittest discover -s tests -v
```

第三条当前构图命令需要已安装本项目依赖的Python环境。`independent-oracles.json` 是独立Decimal计算所得的输入图算术基线；新实现应直接逐字段比较，不能用自身输出更新“期望值”。

### 8.2 新实现须自动化的断言

下表可直接转为参数化fixture。真实ID省略公共前缀时以机构 `SYN-ENT-`、个人 `SYN-IDV-` 补全；GP/法人未配置样例均显式使用equity_only规则。

| ID | 输入与策略 | 必须断言 |
| --- | --- | --- |
| DATA-01 | 本次三表，2026-09-23 | 6,000主体、13,436边；方向股东→企业；含88孤点；所有原始行可追溯 |
| DATA-02 | 个人/机构股东同一来源列 | 按type单一解析；0个伪dangling；8,975/4,461分型边 |
| DATA-03 | 一表改统计日；比例=`bad/-1%/101%` | 混合快照拒绝；非法行有明确计数/定位；不悄悄变成完整图 |
| DATA-04 | 同一事实A→T30%重复两次 | 去重后30%，不确认控制；或明确冲突拒绝，绝不直接加成60% |
| DATA-05 | 两来源A→T30%与40%，同一业务事实 | 无策略时冲突；显式max策略取40%，保留两个来源和裁决原因 |
| DATA-06 | 13个真实总比例超限企业 | 超限集合与profile完全一致；相关确认结果受阻并保留诊断证据 |
| FIRST-01 | 000044 | 直接首大000040，60%；不被最高经济根000957替代 |
| FIRST-02 | 000013 | 个人000350/000685各0.20；排序稳定、并列保留、confirmed_id=null |
| FIRST-03 | 000041，禁用注销停止/小比例过滤且完整计算 | 692累计`0.0119256519764553408`，5路径；723为`0.0087544152`；首大692；不得把此算术期望用于启用注销规则的结果 |
| FIRST-04 | FIRST-03分别取Top-K=1/5/30 | 根累计和排名完全不变；只有display_evidence及其完整性变化 |
| FIRST-05 | R→A80%→B80%→T40%，深度1 | TRUNCATED，first_holder_id=null；B只能是局部候选 |
| FIRST-06 | A→T60%、B→T60% | DATA_CONFLICT；保留两主体；不择一确认 |
| RULE-01 | 重名法人或GP关系缺失 | RULE_DATA_MISSING；不按第一条姓名匹配；equity_only候选与全规则结果分开 |
| RULE-02 | 构造权威GP关系及明确适用规则 | GP按配置优先，来源/有效期可回放；无适用条件不触发 |
| RULE-03 | 000003→已注销000023→000072 | 注销停止开/关分别有版本；开时不确认穿越注销节点的最终根；停止点与原因可见 |
| RULE-04 | 中登/港结算/政府/特殊集团mock清单 | 只按显式ID规则过滤/断开；未知配置不猜测；不覆盖旧快照 |
| CTRL-01 | 000044 | 控制链000001→000040→000044；经济权益0.3428574与可支配0.60分开 |
| CTRL-02 | A→T50%、B→T50%；真实003710→00378450% | 各50%边本身不产生确认控制；不得使用>=0.5 |
| CTRL-03 | A→X60%、A→Y60%、X→T30%、Y→T30% | A控制T，表决权0.60；保留X/Y两条贡献支路 |
| CTRL-04 | A→X80%、A→Y80%、X→Z60%、Y→Z30%、Z→T60% | A对T的Z贡献只计一次0.60，不能因两条到Z路径重复成1.20 |
| SCC-01 | 真实621–625、901/902 | 成员集合正确；控制SCC_REVIEW；同时保留总比例冲突，非单一干净状态 |
| SCC-02 | A↔B各100%，B→T20% | NON_CONVERGENT/奇异诊断，不确认首大；硬控制SCC_REVIEW |
| SCC-03 | A↔B各20%，B→T40% | Decimal/高精度基线核验：sA=1/12，sB=5/12；成员值残差≤1e-10；不把经济收敛当硬控制确认 |
| SCC-04 | 多SCC串联、大于配置上限SCC | 逆拓扑依赖正确；大SCC给TOO_LARGE/异步状态，无假精确值 |
| GROUP-01 | 控制根000001 | 成员集合恰{000001,000040,000044}；原始链与派生边均可回放 |
| GROUP-02 | A→B80%→C80%，成员展示/计算预算2 | 未算全时group_members=false；完整物化但分页时有next_cursor；不能漏C仍宣称展示完整 |
| GROUP-03 | CTRL-03的聚合控制 | 树主边选择不删除X/Y贡献；DAG、额外边与导出保持全部证据 |
| STATUS-01 | 完整控制但Top-K证据/树页面截断 | target_control可为true，display_evidence=false；primary不受纯展示分页影响 |
| VERSION-01 | 相同输入重排、多次运行、规则切换 | 同规则结果/并列排序稳定；不同规则产生不同缓存键和result版本，历史不覆写 |
| API-01 | 重复request_id、异步、跨项目graph_id | 重复提交复用作业；可查终态；越权图拒绝；HTTP成功不掩盖业务冲突 |
| UI-01 | 浏览器三表上传→构图→000044三入口→导出 | counts、快照、业务ID、结果状态及证据与API/独立oracle一致 |

`SCC-03` 的分数为求解定义下A/B对环外目标T的经济权益；使用同一高精度基线比较，不要求浮点近似输出与十进制文本逐字相同。非SCC比例与控制阈值不得借用数值容差把50%升级成过半。

实施后建议建立 `tests/finance/test_projection.py`、`test_first_holder.py`、`test_control_closure.py`、`test_group_projection.py`、`test_contract.py` 及前端E2E；这些文件在本方案中尚未创建。CI逐行覆盖上表，再在已部署Compose实例通过SSH隧道执行UI-01。任何一项状态/证据断言失败，都不能由模型文字解释或“图已ready”替代通过。
