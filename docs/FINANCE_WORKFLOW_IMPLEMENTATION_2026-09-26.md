# iGraph 金融股权工作流实现说明

> 仓库发布说明：服务器连接信息已替换为占位符。文中 `artifacts/` 为本地保留的验收证据，不随 Git 提交；原始 CSV、PPT 和 citics 参考包也属于外部输入。仓库提供实现、测试及验收摘要；真实数据测试需设置 `IGRAPH_FINANCE_TEST_DATA`。

日期：2026-09-26。最终验收在用户指定服务器执行；本地用于编辑、打包和查看回传证据。

本轮已在代码中增加股权业务投影、五个金融工具、会话 API、金融分析 skill，以及构图工具的有界 MCP 返回。本文区分“代码已实现”“独立数据基线”和“服务器实际验收”，不以代码存在代替测试通过或真实模型完成分析。

**已确认的边界：金融工具能够计算四项核心业务，并输出可核验的结构化结果；当前模型生成的整份自然语言报告仍不能视为无需复核的合格交付。** 末轮主会话已正确报告首大、控制链、树及同根经济权益，也保留冲突诊断限制，但仍出现主体统计和覆盖率解释错误。详见第 8 节，不能将“工作流工具调用通过”写成“报告每句话均正确”。

[原需求验证报告](../docs/FINANCE_REQUIREMENTS_VALIDATION_2026-09-26.md)记录的是**改造前**代码基线与失败现场；其中“没有原生金融工具”“全图进入模型后请求超限”等结论描述当时状态。[原改进方案](../docs/FINANCE_IMPROVEMENT_PLAN_2026-09-26.md)是设计与完整验收目标。本轮已实现其中一部分，剩余范围见本文第 7 节。改造后的上线状态和最终判定须由第 8 节的服务器证据确认。

## 1. 实现范围与调用路径

现有多 CSV 上传、schema 配置、构图、项目导入及会话数据集选择流程继续作为数据入口。金融工具读取当前会话 `GraphData.graph_metadata["construction_graph"]` 中的原始三表记录，在服务端建立计算用业务投影。模型无需接收完整图，也无需临时编写 Python 才能调用这五项分析。

```text
三表上传 → PK/FK 构图 → 项目/会话载入已构图版本
                              ↓
               finance_inspect_ownership 质检与主体检索
                              ↓
       first_holder / control_chain / group_tree / economic_interest
                              ↓
             有界结构化结果、状态、原始记录证据 → 聊天解释
```

计算投影与保存的关系构图区分：原始行节点、FK 和优化产生的属性实体/捷径仍保留；本轮不会为了金融分析把已有构图覆盖为另一份主体图。当前投影在金融调用时构建，尚未实现独立持久化投影、离线预计算或结果分页仓库。

主要代码入口：

| 层次 | 已落地代码 |
|---|---|
| 业务投影与确定性算法 | [finance.py](../graph_data_service/finance.py) |
| 工具 schema 与参数上限 | [finance_catalog.py](../graph_engine/finance_catalog.py) |
| 当前会话图读取与结果字节限制 | [compute.py](../graph_data_service/compute.py)、[Graph Data Service 分发](../graph_data_service/app.py) |
| 公共工具目录、只读资源与隔离 worker | [tool_catalog.py](../graph_data_service/tool_catalog.py)、[tool_execution.py](../graph_engine/tool_execution.py)、[worker_contract.py](../graph_data_service/worker_contract.py) |
| MCP 参数适配 | [tool_forwarding.py](../mcp_service/tool_forwarding.py) |
| 已登录会话 API | [backend/app.py](../backend/app.py) |
| Agent 工作流 | [finance-ownership-workflow/SKILL.md](../agent_runtime/resources/skills/finance-ownership-workflow/SKILL.md)、[instructions.py](../agent_runtime/instructions.py) |

## 2. 五个工具与 PPT 需求映射

| 工具 | 主要输入 | 实现内容 | 对应需求及解释边界 |
|---|---|---|---|
| `finance_inspect_ownership` | 可选 `query`、`target_id` | 快照、主体/持股计数、质量问题、主体名称/ID 检索、原始持股记录样例。 | 为三项业务提供共同数据检查；构图 ready 不等于金融输入质量合格。 |
| `finance_first_holder` | `target_id` | 配置的 GP/法人规则、直接严格过半、完整根经济权益累计的优先分支；保留并列。 | 第一大股东。缺权威规则时输出限定股权口径的候选；首大不等于最终控制根。 |
| `finance_control_chain` | `target_id` | 按严格 `>0.5` 的可支配表决权执行固定点，每个受控股东的直接贡献只计一次；返回中间/最终候选与贡献证明。 | 控股链。多分支合计表决权可以形成控制，不能用单条路径比例乘积代替。 |
| `finance_group_tree` | `root_id` | 从指定根展开严格控制成员，输出控制关系、树形展示和附加边，保留贡献证据与 SCC 复核。 | 集团树。树来自严格控制关系；经济路径和关联候选不能混成已确认集团。 |
| `finance_economic_interest` | `target_id`，可选 `root_id` | 无环区域完整累计；SCC 成员级方程求解；根排名；可另查询指定上游主体对目标的累计权益。 | 为首大及穿透分析提供独立经济口径；指定上游不必是输入图边界根或最终控制根。 |

`economic_interest.result.requested_interest` 表示指定主体到目标的完整累计权益；展示路径的 `product` 只表示该条路径的乘积；`root_interests` 则按输入图边界根排名。这三个字段不能互换。改变 `evidence_top_k` 只改变展示证据，不能改变完整累计排名。

集团展示通过现有聊天中的层级文本、表格或 Mermaid 使用 `tree_edges`、`additional_edges` 和 `control_dag`。本轮增加了工具中文标签，**尚未新增独立的金融分析页面或专用交互式集团树组件**。

## 3. 投影、质量与状态契约

投影只读取 `institution_main`、`individual_info`、`institution_shareholder_relationship` 的原始 `properties.data`；构图产生的 FK、属性连接及两跳捷径不作为新增持股事实。股东类型 `0/1` 分别连接机构/个人，业务方向为股东→被投机构，机构与个人使用独立类型标识。

比例格式由 policy 显式选择，当前三表使用 `percent_string`。例如 `57.1429%` 解析为十进制 `0.571429`；输出使用十进制字符串，极端指数可使用科学记数。经济计算使用 50 位 Decimal；持股总额和控制票权累计使用 110 位，覆盖受限输入下 `0.5 + 1e-100` 和 `1 + 1e-100` 的严格阈值。超出支持的输入精度/指数范围不静默变成零或确认结果。

输入校验覆盖快照日期、主体引用、比例格式、重复/冲突事实和股份总和。重复事实不直接叠加；冲突最大值策略仅在显式选择时启用。超过 100% 的被投主体保留质量异常，不自动归一化。来源节点 ID、事实 ID、输入 hash、语义 hash、快照及算法/规则版本随结果保留；原始文件和构图仍是完整追溯依据。

结果中的几类状态需要分别读取：

| 字段/状态 | 含义 |
|---|---|
| `complete`、`completeness.projection/computation` | 所选股权口径的投影/计算是否完整；不是全部现实股权已知或全部业务规则已齐备。 |
| `rule_status`、`policy_mode`、`effective_policy` | 实际选用规则、停止/排除/小比例过滤口径及规则数据是否缺失。 |
| `quality`、`DATA_CONFLICT` | 输入质量及其对本次目标的影响；不能把候选升级为确认。 |
| `analysis_status`、`control_status`、`review_reasons` | 计算判定与复核原因；可同时保留 SCC 复核和数据冲突。 |
| `equity_controller_id` | 所选股权口径下的控制根候选；不自动填补缺失的 GP/法人或特殊主体规则。 |
| `confirmed_id`、`primary_faction_id` | 受规则覆盖、质量及复核状态约束的确认字段；默认规则缺失时不据此制造业务确认。 |
| `display_truncated`、证据 `complete`/计数 | 返回列表或证明是否完整展示；与计算排名完整性分别报告。 |
| `TRUNCATED`、`NON_CONVERGENT`、`NUMERICAL_REVIEW` | 资源或数值计算未完成/不能确认，须保留原因。 |
| `FINANCE_SCHEMA_REQUIRED`、`INVALID_ARGUMENT`、`EXCLUDED_BY_POLICY` | 输入不是所需三表、参数不支持或查询主体被所选规则排除。 |

`equity_only` 不接受同时注入权威规则关系。`configured_rules` 可消费显式提供、绑定快照且带来源证据的 GP/法人关系；发生优先级未配置时返回未决状态。**本轮没有生成这些权威关系或特殊主体名单**，也没有把法人姓名匹配当作可靠身份来源。

## 4. 会话 API 与只读边界

新增接口复用 BFF 的 `graph_tool`：校验当前登录用户、读取所属会话、确保该会话活动数据集已加载，再调用同一 Graph Data Service 工具。金融参数不接受 `session_id`、`user_id`、`dataset_id` 或完整图来覆盖当前会话的选择。

| HTTP 接口 | 对应工具 |
|---|---|
| `POST /api/sessions/{session_id}/finance/inspect` | `finance_inspect_ownership` |
| `POST /api/sessions/{session_id}/finance/first-holder` | `finance_first_holder` |
| `POST /api/sessions/{session_id}/finance/control-chain` | `finance_control_chain` |
| `POST /api/sessions/{session_id}/finance/group-tree` | `finance_group_tree` |
| `POST /api/sessions/{session_id}/finance/economic-interest` | `finance_economic_interest` |

请求体直接使用工具参数 JSON，例如：

```json
{
  "target_id": "已由检查工具解析的主体ID",
  "evidence_top_k": 5,
  "display_limit": 100,
  "max_seconds": 10
}
```

五个工具都声明为只读图资源，与写图操作遵循既有调度顺序；具备运行执行上下文时复用已有隔离 worker 和取消边界。直接 API 调用也校验 schema，不依赖模型或 MCP 提前过滤错误参数。MCP 适配支持 policy 对象以及模型提供的合法 JSON 对象字符串；不合法类型、未知字段或超限参数明确拒绝。

主体原始业务 ID 最长 256 字符；`I:`/`P:` 类型主体 ID 最长 258 字符。顶层参数与规则内主体引用采用相同边界，避免检查工具返回的 ID 无法用于后续分析。

## 5. 工作流指令与有界返回

默认 Agent 指令增加金融任务路由：先读取 `finance-ownership-workflow`，执行 `finance_inspect_ownership` 后调用对应专用工具。skill 规定主体消歧、同一 policy、缺失规则说明、控制/经济/首大分离和证据解释；禁止用普通最短路、社区结果或临时 workspace 脚本冒充金融业务结果。真实模型调用与文字语义分别验收，不能仅凭调用顺序或会话 completed 判断通过。

指定 `root_id` 时，经济工具的主路径证据只返回该主体到目标的路径，标记 `evidence.path_scope=requested_upstream`；没有指定时为 `boundary_roots`。边界根排名仍单独返回，不能混作指定主体的贡献。skill 同时禁止从有限路径推算“其余权益”，要求每个目标先报告顶层数据质量状态；改变停止/排除策略后，必须使用相同 policy 重新检查。

金融工具默认预算与硬上限：

| 参数 | 默认值 | 支持范围 |
|---|---:|---:|
| `evidence_top_k` | 5 | 0—30 |
| `display_limit` | 100 | 1—500 |
| `max_nodes` | 100,000 | 1—200,000 |
| `max_edges` | 1,000,000 | 1—2,000,000 |
| `max_seconds` | 10 | 0.05—60 |

金融分析结果在计算包装层逐段统计 UTF-8 字节，超过 **128 KiB** 时返回 `FINANCE_RESULT_TOO_LARGE`，不将被裁掉的确认结果交给模型。该限制针对分析结果 payload；会话分发还附带图版本等少量外层元数据。

[bounded_results.py](../mcp_service/bounded_results.py)对 `get_constructed_graph`、`preview_graph_optimization`、`commit_graph_optimization` 的 MCP 返回增加 **64 KiB** 检查视图限制。大图返回样例和全局计数，并明确 `inspection.truncated=true`、`complete_graph=false`；完整数据继续保留在数据服务。此变更限制模型可见内容，不代表完整图从未在服务之间传输，也不等于所有日志、历史恢复快照和授权延迟问题均已修复。

## 6. 本次真实数据基线与验收目标

以下是此前源数据统计及完整构图审计已确定的基线，用于本轮验证，**不是本轮新代码通过服务器验收的记录**。

| 项目 | 独立基线 |
|---|---:|
| 快照 | 2026-09-23 |
| 机构/个人 | 5,000 / 1,000 |
| 原始持股记录 | 13,436 |
| 原始三表行节点 / FK | 19,436 / 26,872 |
| 已保存优化后关系构图节点 / 边 | 24,823 / 74,820 |
| 业务投影目标 | 6,000 主体 / 13,436 持股事实 |
| 机构股东 / 个人股东持股 | 8,975 / 4,461 |
| 孤立主体 | 88 |
| 股份总和超过 100% 的机构 | 13 |
| 非平凡 SCC | 10 |

来源：数据需求基线（本地证据：`artifacts/finance-validation/requirements-and-data.md`）、最终构图审计（本地证据：`artifacts/finance-validation/final-graph-audit.md`）。

真实验收应继续覆盖 `000044` 的首大/控制根分离、`000013` 并列、`003784` 恰 50%、根 `000001` 的集团、SCC/股份总和异常以及注销停止口径。机构简写均使用 `SYN-ENT-` 前缀。

经济权益验收必须独立枚举或求解全部适用路径。`000001→000040→000044` 的单路径乘积是 `0.3428574`，不能把它直接用作 `000001→000044` 的全路径累计期望。[真实图集成测试](../tests/servers/test_finance_tool_integration.py)使用独立 Decimal DFS，直接读取原始持股行、记录各条路径来源与乘积，服务器已确认全路径累计为 **0.348494958768（34.8494958768%）**。原始持股合计、完整上游累计和控股链乘积是不同的量。

## 7. 明确保留的限制

- GP/法人权威身份关系、适用条件和特殊集团/政府/代持等名单仍需业务方提供。本轮实现配置接口和缺失状态，没有补造数据，也没有证明全部 PPT 特殊规则已经覆盖。
- 当前分析限定已记录股权及所选规则；缺少上游记录不证明现实中不存在上游股东，股权计算结果也不是现实控制关系的权威认证。
- 默认注销停止未启用；需要中止时显式传入停止 policy，并在检查及后续计算中保持一致。吊销和其他状态不自动套用注销规则。
- 现有集团工具输出 CONTROL 结果及其证明；未新增独立 PATH/AFFINITY 派系发现引擎。聊天可解释经济证据，不能将其加入确认 primary。
- 本轮未实现原方案中的持久化业务投影、通用结果/证据分页、增量缓存、离线批量预计算及独立金融页面。返回预算/资源状态时不代表这些后续机制已经可用。
- SCC 使用受限规模的内存求解；超出支持范围或数值不可靠时不能给确定结论。大规模性能、百万/千万节点容量和性能 SLO **尚未验收**。
- 模型可见构图结果有界，不替代对历史大快照、日志放大、授权超时、取消和终态恢复的独立验证。
- 最终报告的测试数量只采用服务器日志。前期本地探索不计入最终通过数；按用户明确要求，之后的算法、接口、前端构建及模型验收均在服务器执行。

## 8. 服务器验收记录

| 验收项 | 结果与证据 |
|---|---|
| 本轮代码版本 / 文件一致性 | 基于 `7c3ff6a7bd8edb640ccb012f4f084e606d9d1689` 的工作区修改；834 个打包源文件逐个 SHA-256 一致，另核对 5 个运行服务内的关键文件；见 `deployed-source-verification.json`。尚未创建 Git 提交。 |
| 服务器目标、Compose 项目与镜像 | `<server-user>@<server-host>:<ssh-port>`；目录 `/home/<server-user>/apps/igraph-finance-20260926`；Compose 项目 `igraph-finance-20260926`。应用使用该项目的 `:local` 镜像；回归环境为 `igraph-finance-verify:20260926`。 |
| 重建和启动结果 | 服务器重建图 API/worker、MCP、Agent runtime、backend 和 frontend；沿用现有隔离数据库及原数据卷。Node 22 容器内前端构建成功，178 项测试及模型运行使用 Python 3.12 容器。 |
| 容器健康状态及检查时间 | 2026-09-26 15:47:36 +08:00：6 个设置健康探针的服务全部 healthy，独立 worker 为 Up；见 `final-health.txt`。 |
| SSH 隧道、本地访问地址与浏览器验证 | [本机访问 iGraph](http://127.0.0.1:22173/) 通过 SSH 隧道连接服务器 loopback 22173；浏览器已看到内置 `finance-ownership-workflow`。最终会话链接见下文。 |
| 金融内核及回归测试数量、通过/失败/跳过 | 最终服务器运行 **178 passed、405 subtests passed，0 failed、0 skipped**，48.44 秒；5 条既有依赖弃用警告。见 `pytest.log` 与 `pytest.xml`。 |
| schema、MCP policy、API 和只读 worker 测试 | 上述套件覆盖五工具 schema、真实 FastMCP 的对象/JSON 字符串 policy、BFF 路由白名单及隔离 worker；Agno 实际加载 skill 成功，无 skip。 |
| 跨用户/跨会话权限隔离、原图不变验证 | 权限及构图回归通过；不同用户/会话不共享主体，不能传选择器覆盖图；真实图五工具前后 graph version 和 metadata SHA-256 一致。 |
| 真实构图五工具调用、输出字节数及独立比对 | 6,000 主体 / 13,436 持股事实与原始三表一致；真实图集成测试检查结果字节上限；在线 12 次 BFF 调用通过断言。独立原始行 DFS 枚举 9 条 `000001→000044` 路径，累计 `0.348494958768`。 |
| GP/冲突/排除/typed ID 边界测试 | GP 优先证据、规则缺失、equity_only 隔离、并列、50% 阈值、多分支控制、去重、SCC、非收敛、110 位严格累计、排除根/规则主体及 256/258 字符 ID 测试均通过。 |
| 大工具输出、恢复、授权及终态验证 | 金融 payload 128 KiB 上限、构图 MCP 64 KiB 样例化及授权回归通过；不将这些结果扩展为历史巨型快照/取消恢复已修复。新模型会话终态单独记录。 |
| 默认模型、会话 ID / run ID、参数 | 本次调用 `qwen3.6-plus`，`reasoning_mode=direct`、`planning_mode=auto`；未向 prompt 提供具体工具名或期望数值。主会话 `67b63ed0-7d36-4d45-bc80-307168b93fe0`，run `8a82269b-4337-4c9b-bbed-7f03b2728681`；异常会话 `7a459e56-edc1-4b81-abcd-50f4757cd19b`，run `1225ca97-3747-4af2-93d7-e9ff7c87a684`。两者均 completed 且 snapshot_complete=true。 |
| 模型实际读取 skill、使用五工具的调用记录 | 两轮末次会话追踪检查均通过：主会话读取 skill 1 次、检查 1 次、首大 2 次、控股链 2 次、集团树 1 次、经济权益 1 次；异常会话读取 skill、按需要加载已构图数据、检查 3 次、控股链 3 次、经济权益 2 次、集团树 1 次，注销分支检查和计算使用同一 policy。未调用全图拉取或 workspace 脚本替代。见 `live/workflow-trace-audit.json`。 |
| 第一大股东 / 控股链 / 集团树业务结论 | 结构化接口：`000044` 首大 `000040`，60%；股权控制根 `000001→000040→000044`。集团为根加 2 个受控成员，共 3 节点，2 树边、1 附加控制边、3 条控制 DAG 边。`000013` 两名个人 `000350/000685` 各 20% 并列，直接持股合计 55%，单一过半股东数为 0。`003784` 的 50% 边不构成单独严格控制；上游数据冲突使总体结论仍为 `DATA_CONFLICT`，不能确认不存在控制人。 |
| 指定上游累计权益、各侧路乘积证据 | `000001→000044` 完整累计 34.8494958768%，主控制路径 34.28574%；独立 oracle 共 9 条路径，Top-K=5 只展示部分。指定根证据全部从 `000001` 出发；12 次在线断言和真实图独立 DFS 比对均通过。 |
| 尚未通过项目与最终验收判断 | **服务器工具/算法、12 次在线接口断言、两段模型工作流执行通过。四项核心结果可取得且有独立证据。整份模型自然语言报告未完全通过语义验收**：仍有统计范围误述和复杂质量状态的解释矛盾，见下文。完整 GP/法人/特殊主体业务规则缺数据，不能宣称全部 PPT 规则的权威确认已完成。 |

已在服务器执行的测试入口：[金融内核](../tests/finance/test_finance.py)、[真实数据](../tests/finance/test_real_finance.py)、[工具契约](../tests/test_finance_tool_contract.py)、[HTTP/MCP/worker 集成](../tests/servers/test_finance_tool_integration.py)、[BFF API](../tests/test_finance_api.py)、[skill 发现与契约](../tests/test_finance_skill.py)、[构图有界返回](../tests/test_bounded_construction_results.py)，以及只读工具、标签、授权、构图 HTTP/state hook 回归。

证据已回传到 server-results（本地证据：`artifacts/finance-workflow/server-results`），服务器原件位于上述部署目录的 `workflow-results/`；部署和重放脚本保留在 deploy（本地证据：`artifacts/finance-workflow/deploy`）。原始 CSV 没有修改。

可直接打开 [主业务验收会话](http://127.0.0.1:22173/?session=67b63ed0-7d36-4d45-bc80-307168b93fe0) 或 [SCC/注销策略验收会话](http://127.0.0.1:22173/?session=7a459e56-edc1-4b81-abcd-50f4757cd19b)。前端已通过本地 SSH 隧道读取最新结果，Skills 列表可见内置金融工作流。浏览器展示的是实际模型原答，文字问题没有被隐藏或改写。

### 8.1 真实模型首轮发现与修正

首轮会话虽 completed，不能记为业务通过。主会话把其他经济根的 Top-K 路径和指定上游的总权益相减，并忽略了 `003784` 的上游 `DATA_CONFLICT`；异常场景会话切换注销策略后没有重新检查。已将指定根证据分离，收紧 skill 的状态解释及策略检查要求；保留 首轮问题记录（本地证据：`artifacts/finance-workflow/initial-workflow-review.json`） 和 `live-initial/`，随后用相同自然语言问题、新会话、修订后代码重测。

第二轮调用顺序和策略检查均通过，数值及同根证据正确，仍发现“树边和控制 DAG 边混称”“合计 55% 却称未过半”等报告文字错误，另有 SCC 收敛、异常比例修复的表述超出工具支持。该轮保留在 `live-review-2/`，不记自然语言报告完全通过。最终补充首大合计/单一过半人数，以及集团树/附加边/控制 DAG 的确定性计数；skill 要求分列这些字段、保留顶层状态，禁止以经济收敛解除控制复核或缩放股比修复数据。

### 8.2 末轮主会话的独立语义复核

末轮主会话 `67b63ed0-7d36-4d45-bc80-307168b93fe0` 已完成，run 为 `8a82269b-4337-4c9b-bbed-7f03b2728681`。实际读取 skill，再检查主体，调用首大、控股链、集团树和指定上游经济权益；集团根和经济查询根取自控制结果。核心主体、比例、并列、树计数和 `DATA_CONFLICT` 结论门槛已与工具一致。

以下四处自然语言误述仍存在，**本报告以结构化证据校正，不把原答改成通过记录**：

| 模型原答问题 | 正确解释 |
|---|---|
| “6000 家机构，1000 个自然人” | 总计 6,000 个主体 = 5,000 家机构 + 1,000 个自然人。 |
| “4987 家机构无正向股东信息” | 4,987 表示已记录持股合计不足 100% 的覆盖缺口，包含有部分股东记录的机构；没有已记录股东的是 591 家。 |
| 将 `003784` 说成 12 个异常实体之一 | `affected_entity_ids` 是本次上游范围内的 12 个股份超额主体，列表不包含目标 `003784`；目标控制判定受这些上游异常影响。 |
| “13 条持股合计超 100% 的异常记录” | 13 指被投主体的股份合计超额，不是 13 条原始持股事实。 |

这些误述不改变本次四个核心算法结果，但说明仅靠 skill 仍不能保证整份自动文字报告准确。若要交付无需人工复核的金融报告，下一步应由确定性模板直接渲染实体计数、比例、状态、树边和来源范围，模型仅补充说明；发布前校验文本中的数字及确认用语与结构化结果一致，并在更多未知样本/模型重复运行中验证稳定性。本轮没有把这项报告生成保障伪装成已实现功能。

### 8.3 末轮 SCC 与注销场景的语义边界

异常会话 `7a459e56-edc1-4b81-abcd-50f4757cd19b` 的实际调用顺序、策略检查和结构化结果通过。`000621` / `000901` 都保留 `DATA_CONFLICT` 与 `SCC_REVIEW`，`confirmed_id=null`；经济方程可返回有限精度的诊断数值。`000072` 使用注销停止策略，股权口径边界为 `000023→000072`，60%；该根的控制树为 2 节点、1 条树边，`primary_faction_id=null`。

模型正文虽然多处说明“数值可算不等于业务确认”，仍存在不能忽略的错误：

- 开头声称股比超限“不影响控制阈值独立判定”，随后又把部分候选关系直接称为控制成立。正确解释是可以计算诊断阈值，但来源冲突影响关系的可信性，不能因此解除复核或确认控制。
- 将 SCC 残差指数写为 `e-52`；原始工具返回约为 `000621: 2.8299e-50`、`000901: 3e-50`。收敛和小残差也不保证存在数据冲突时的现实经济解释正确。
- 把 `000072` 的四笔少数投资称作“均在集团树展示范围内”。这些来自检查工具的原始投资事实，控制树实际只有 `000023` 与 `000072` 两个节点；不能把弱投资边加入严格控制集团。

因此本轮**没有判定完整自动金融报告端到端合格**。已满足的范围是原生金融计算工具、内置 workflow、服务器运行及结构化结果；模型报告的自动发布保障仍需上述确定性呈现和一致性校验。独立语义复核见 semantic-review-final.md（本地证据：`artifacts/finance-workflow/semantic-review-final.md`）。
