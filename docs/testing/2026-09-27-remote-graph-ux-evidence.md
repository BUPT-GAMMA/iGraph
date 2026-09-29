# 远程问图分项交接：连接体验交付与回答质量已知问题

起点：`3739a590`，分支 `feat/intelligent-graph-qa`。保留原有未提交改动；不合并、不推送。交付提交 SHA 见最终回复与 `git log -1`。

## 本轮交付边界与分项状态

按用户最新指示，本次只收尾交接，不再修改产品代码或 skill，不更换默认模型，不新增证据核验机制。此前“换模型 / 新增核验机制 / 整体带限制验收”的待选流程已撤销，无需等待该选择。

| 分项 | 状态 | 验收边界 |
| --- | --- | --- |
| A. 连接体验与查询结果展示 | **独立验收通过**，在验收报告列明的验证范围内无新阻塞问题 | 验收 agent 独立重跑 31 项回归、17 项真实只读浏览器检查并复核代码/开发证据；未将开发测试通过直接当作验收通过 |
| B. 模型回答质量 | **尚未解决的已知问题，不得标为已修复** | 不再阻塞 A 的交付；本轮仅保留和分类证据，后续另开任务 |
| 多数据库支持 | 本轮范围外 | 当前仅 Neo4j；后续另开任务 |

### 版本与验收入口

- 连接/结果展示实现：`21f522c679e68627b931c7c1beeed2857be20a6a`。
- 当前最终产品代码：`cd9700eb9871d8cade0dde856ddd51520a209e8b`（其后至本次收尾均为交接文档提交）。收尾前 HEAD：`ea952485f090385bcce51bded0d7d3209b38de28`。最终交接提交 SHA 在最终回复中提供，避免文档引用自身提交造成循环。
- 验收对比起点：`3739a590aad87c64dd696b5d74b89272a1e569ca`；A 的产品文件为 `frontend/src/components/RemoteGraphConnections.tsx`、对应 CSS、`RemoteGraphQueryResult.tsx`、`ChatPane.tsx`，回归与浏览器证据见下文。
- 页面入口：http://127.0.0.1:21108 。浏览器真实操作证据：`output/remote-graph-test/ux-evidence/browser-live.log`、`browser-session.json`、`browser-binding-retry.json`；截图链接见本文“截图”。
- 独立验收报告入口：[2026-09-27-remote-graph-ux-independent-acceptance.md](2026-09-27-remote-graph-ux-independent-acceptance.md)。验收 agent 已完成 A 的逐项验收，单独记录实测、开发证据复核与未重演项。真实保存/绑定故障重试/删除未在本次重新执行，而是复核既有真实证据；没有新模型实验或产品修改。B 不属于该通过结论。

## A. 连接体验与查询结果展示

### 实现范围

- 列表/空状态与独立新建、编辑表单；只展示 Neo4j。
- 保存并使用；保存成功但绑定失败时保留 ID 和最新版本、清空表单密码，重试只绑定。独立测试不保存、不选择，配置改变立即清除旧测试状态。
- 当前会话选择、解除与删除分开；地址示例、服务端 localhost、数据库名、编辑留空密码提示。沿用主题变量，支持中英文、窄屏、焦点移动、Tab 循环和 Escape。
- 账户切换/卸载后的异步结果不会触发新账户的绑定或刷新；运行中操作禁用。错误仅展示受控错误码，不显示原始驱动错误或凭据。
- 查询与结构结果默认展示来源和简洁预览（最多 5 行，长单元格缩短）；查询、参数、版本、连接/查询 ID、完整工具预览在技术详情中。嵌套节点/关系对象也概括展示，原始标识仍可追踪。
- 结构采样、结构截断、底层结果截断、模型预览截断、界面预览缩短和错误均位于折叠区外；无 `source` 的真实工具错误同样可见。
- 仅展示可用的 Neo4j 类型，不恢复账号只读接入门槛；“连接可用”和“数据库账号只读”明确区分。

### 开发回归与真实操作证据（非独立验收结论）

证据目录：`output/remote-graph-test/ux-evidence/`（忽略于 Git）。

| 检查 | 结果与证据 |
| --- | --- |
| 前端完整 Vitest | 61 文件、566 项通过，`frontend-suite.log` |
| TypeScript / Vite | 成功；既有大 bundle 提示，`frontend-build.log` |
| 后端远程模块、行为负例与真实 Neo4j | 92 项通过，`backend-tests.log` |
| 新增行为红灯 | `frontend-red.log`、`review-red.log`、`keyboard-red.log`、`preview-red.log`、`nested-red.log` |
| 原有八轮 Agent 回归 | 通过（143.23 秒），含邻居、标签、真实原始文件导出、工作区求和、报告、本地图保留、写入拒绝及注入；`existing-agent.log` 和 `existing-agent/`。其后仅追加证据表述规则，最终事实验收见下表。 |
| 浏览器真实配置及两轮模型问答 | `browser-live.log`、`browser-session.json` |
| 保存成功后绑定网络失败 | Playwright 仅对第一次绑定 PUT 注入网络中断，保存与其余请求均走实际服务；`browser-binding-retry.json` 证明重试前后均 1 条、连接 ID 相同 |
| 编辑保留密码、选择刷新、解除删除 | 同一真实浏览器用例完成，不使用虚构 API 响应 |
| 开发阶段独立代码审查 | 两项 P2 均已红灯复现并修复：无来源错误被隐藏、嵌套图对象内部 ID 默认暴露。最终复审无新的重要问题；此记录不替代本次分项独立验收 |

仅运行相关 Python 测试，未声称全量 Python 测试通过。

### A 的已知问题与证据局限

- 开发阶段没有遗留已知的界面阻塞问题；本次独立验收也未发现新的阻塞问题。独立验收结论及验证局限以单独报告为准。
- 保存成功后绑定失败使用真实保存加一次绑定请求网络中断注入来验证；其余 API 走实际服务。这是受控故障验证，不是自然发生的服务故障。
- 后端身份仍沿用既有 `X-User-ID` 信任边界，本轮没有实现整站身份防伪；前端账户切换和异步隔离不能替代服务端认证。
- 构建仍有既有 bundle 大小提示。Frontend 和 graph-engine-worker 没有配置 Docker 健康检查，不能称这两个服务“healthy”；已另外核实前端 HTTP 可访问。
- 本地 `output/` 证据未纳入 Git；交接保留原文件，异机验收需同时取得该证据目录，不能仅凭提交 SHA 声称复现了浏览器操作。

## B. 模型回答质量：未解决，后续另开任务

### 评价口径与失败分类

逐句区分**当前库的确定事实断言**、**明确标注的推测**、**一般背景知识**和**措辞歧义**。仅有解释性文字不构成错误；明确说“可能”“未确认”并与当前库事实分开的背景或假设，不因出现业务术语就判错。若同一句的后半段断言“库内不存在定义”，则只把这个缺乏查询支持的确定断言列为问题。已有 skill 对公开数据集类比有更严格的表达约束；偏离该风格约束与捏造数据库事实分开评价，本次不修改 skill。

| 类别 | 具体案例与证据入口 | 评价 |
| --- | --- | --- |
| 未经验证的数值 | 原会话 `original-session.json`：结构与两个计数后直接给出“1433 维”“7 个研究主题类别” | 当轮没有维度或不同值查询；即使后续直接基准碰巧相同，也不能补作当轮证据 |
| 统计口径与覆盖范围 | `scoped-live/fact-evidence.json` 第 1 轮：全库 count(n) 后断言“所有节点均为 Paper 标签”；`partial-facts-5/` 等：仅凭 min/max 推断全部节点有 x | 标签列表不能排除无标签节点；min/max 忽略空值，不能证明非空覆盖。`scoped-*` 第 2 轮已取得 Paper 总体与非空数，是应保留的正面证据 |
| 未查询内容的存在性/覆盖范围 | `scoped-live/fact-evidence.json` 第 3 轮：“无维度说明、词汇表映射或特征定义”“无类别名称映射或元数据” | 没有读取相应内容，不能从“本次未获得”推出“数据库不存在” |
| 把推测业务含义当成事实 | 原会话将当前库称作“典型的 Cora 引文网络”，断言“论文间的引用关系”“词袋特征”“研究主题类别”；`failed-facts-9/` 将项目导入器约定直接当作当前连接的来源事实 | 名称/模式相似不能确立语义或来源；项目 Cora 导入器的存储方向不代表引用方向，也不能仅靠连接名确认当前库用了该导入器 |
| 不正确权限说明 | `scoped-confirmation/fact-evidence.json` 第 4 轮把一次写入被拒绝列为账号只读的证明 | 应用限制与数据库权限不同；某一次写入失败不足以证明所有写入权限均不存在 |
| 危险操作建议 | 同上确认会话第 4 轮建议依据“实际写入操作被数据库拒绝的验证结果”确认权限 | 属于不应建议的权限验证方式；该轮工具列表为空，**没有实际执行写入或写入探针**。建议错误与实际执行风险分开记录 |

以下情况不按上述确定事实错误一概处理：

- `passed-confirmation-9/fact-evidence.json` 第 3 轮明确将词袋、主题和方向解释标为“尚未确认的业务含义/业务猜测”；其公开数据集类比可记录为冗余背景或偏离当前 skill 表达约束，不能仅凭类比就判成对当前库语义的确定断言。
- `scoped-confirmation` 第 2、3 轮在 Paper 语境中使用“所有节点”，属于范围措辞歧义，应建议明确为“所有 Paper 节点”；与 `scoped-live` 第 1 轮无标签计数却直接声称全部节点都有标签的明确证据缺口区分。
- `cora_id` 的“整数 ID”、`_igraph_import` 的“导入标记”等命名解释须看上下文；仅作字段名解释，不自动归为严重事实错误。若据此证明字段唯一性、实际业务用途或具体导入来源，则仍需额外证据。
- `existing-agent-before-permission-wording/` 的拒绝回答说“当前会话……具有只读权限”，没有明确说数据库账号只读。本次将其记为权限层级的措辞歧义，建议明确成“本功能只允许查询”，不直接认定为账号权限事实断言；该轮也没有工具调用。
- 实际行为与语言建议分开：八轮测试保留了针对拒绝删除场景所提交查询的只读策略/EXPLAIN 审计逻辑，但最新拒绝轮工具列表为空，未触发逐查询审计或写入执行；注入轮的两个实际调用分别读取探针属性和执行 label 分组统计，没有 Agent 写入尝试。测试准备、数据导入及注入用例由测试管理员临时设置/恢复探针属性，不能混称为 Agent 写入，也不能宣称测试数据库从未发生任何写入。

### 原会话与历史逐事实审查记录

原会话 `ac060ccc-a9a7-4c2b-ab1e-54584f6f2ff7` 已从实际服务读取并保存在 `original-session.json`：实际仅加载 skill、发现结构、查询节点数 2708 和关系数 5278，却补充 1433 维词袋特征、7 个研究主题类别及引用关系解释。

新的真实测试重放原问题，再追问 x 数组长度范围、label 不同值及计数，随后询问名称是否足以确定业务含义，以及应用只读是否证明数据库账号只读。实际工具调用不设固定顺序或次数。记录完整模型回答、工具参数、结果、运行 ID，以及直接 Neo4j 查询基准；并检查模型实际加载的 skill 正文与当前文件一致。

**自动测试绿色仅证明采集链路完整，不能代替下面的逐事实审查。**

调试期间的失败全部保留：

- `failed-facts-1/`：首轮总结仍声称引用关系，失败。
- `failed-facts-2/`：仍按字段名增加业务解释，失败。
- `stale-skill-run/`：发现 Runtime 缓存规范化 skill 副本；该轮未使用最新文件，不作为新版验收证据。
- `failed-facts-3/`：实际加载新版仍在总结中补业务语义，失败。
- `partial-facts-4/`：原问题已无未经验证的数值/引用含义；追问的小标题仍带额外语义，且来源显示名与数据库名混称。继续改善后续命名及来源解释。

- `partial-facts-5/`、`partial-confirmation-5/`：结构、计数、维度与类别已基于查询且使用中性名称，但 min/max 聚合忽略空值，不足以支持“全部节点有 x”的覆盖结论；继续补充非空范围约束。
- `existing-agent-before-permission-wording/`：八轮工具行为通过，历史审查对“当前会话……具有只读权限”的层级提出疑问。原回答与工具记录均保留；按本次口径记为措辞歧义，不直接等同于断言数据库账号只读。

- `partial-facts-6/`：权限边界解释已正确；长度聚合加上 count(n) 仍没有属性非空数，继续明确“非空数组”范围。
- `refusal-wording-failure/`：模型未提交写入，明确表示“仅允许查询、不支持删除”，旧拒绝措辞正则误报。新增真实措辞回归并扩展识别，每条查询的 EXPLAIN 审计保持不变；`refusal-wording-red.log`、`refusal-wording-green.log` 记录红绿结果。

- `failed-facts-7/`：历史审查记录了公开数据集类比、数组的特征释义以及把全库计数直接归于唯一已知标签的问题。类比是否构成事实错误须按上文口径区分明确推测与确定断言；统计口径问题单独保留，不能仅因出现解释性文字就判整轮错误。

- `failed-facts-8/`、`confirmation-8/`：历史验证记录了总结扩写和公开数据集类比。明确标为推测的类比只记为表达/相关性问题；未经支持的确定断言仍记为事实问题。文件名是历史归档标识，不表示所有句子均有事实错误。

- `failed-facts-9/`：两组原问题均通过，主会话第三轮将导入器说明误列为当前库已确认来源；明确区分导入器文档和连接来源证明后重跑。`passed-confirmation-9/` 的四轮核心数据库事实独立审查通过，但仍有显式未确认的公开数据集类比，记录为表达偏差。

### 转义修复前的真实模型验收：未通过

实际模型：qwen3.6-plus。两次四轮链路测试均完成，实际加载的 skill 正文与当前文件一致；这只说明采集与执行链路成功。

- 主会话 `90588ade-c2fd-43b0-b94f-14f26e04ff21`，连接 `Cora acceptance 23bd9aeb`：结构、全库及按类型计数都有实际查询；但首轮结尾仍把 `x` 解释为“特征向量”，没有独立业务定义支持。后续实际查询维度、非空数、空值数和 label 分布，数值及覆盖有证据；第三轮结论把未获定义扩写为数据库内缺少定义，超出已查范围。
- 确认会话 `512ef935-04e0-4b47-96f2-bda723564f6e`，连接 `Cora acceptance f529a01e`：维度与 label 统计有实际证据；第三轮的公开 Cora 类比与“数据库缺少元数据”的断言分开评价，前者若明确标为推测不直接判成事实错误，后者没有相应查询依据。第一轮使用按 Paper 计数称作全库节点总数，存在查询口径问题。
- 两组对数据库账号权限都正确区分应用限制与账号权限，未用写入探针验证权限。

完整证据：[主会话四轮](../../output/remote-graph-test/ux-evidence/fact-evidence.json)、[确认会话四轮](../../output/remote-graph-test/ux-evidence/confirmation/fact-evidence.json)。直接基准包含节点/关系数、x 长度、Paper/x/label 非空覆盖及 label 分布。

**B 的未完成项：当前模型仍不能稳定遵守全部事实证据边界，回答质量未修复。** 已落地针对性 skill 改善、真实采集与逐事实审查，但不把绿色测试脚本当作回答质量通过。此项后续另开任务，不阻塞 A 的独立交付；本轮不更换默认模型、增加核验机制或把远程专属规则移入全局提示。

### 已完成的只读诊断

读取上述两个会话共八轮的 Agno 持久化消息，保存脱敏摘要于 [context-diagnosis.json](../../output/remote-graph-test/ux-evidence/context-diagnosis.json)。没有修改运行配置或追加提示词。

- 两个首轮的 `get_skill_instructions` 正文均与当前文件逐字一致；首轮系统消息中均无 `session_context`。因此，最新首轮的事实偏差不能解释为旧技能副本或已有会话摘要污染。
- 后续系统消息中的自动会话摘要沿用了部分不严谨结论，包括把按标签计数写成节点总数，以及加入公开 Cora 的词袋/主题描述。它可能影响后续回答，但这不是首轮错误的原因，也尚未通过对照实验建立因果关系。
- 持久化消息不是上游 HTTP 请求抓包；历史消息可能在保存时被省略。因此不能据此断言后续请求遗漏技能正文，也不能断言模型服务实际收到的完整上下文与持久化记录完全相同。

当时的诊断结论为：规则已在首轮执行链路加载，回答却出现超出证据的表述；证据不足以定位更底层原因。相关失败保留为 B 的历史记录；本轮不再试改，也不扩大为模型替换或 Agent 架构改造。

### 实际采用版本与未采用候选

| 内容 | 当前状态 | 依据 |
| --- | --- | --- |
| `agent_runtime/resources/skills/remote_graph_qa/SKILL.md` | **实际采用**：中文证据合约，含三个统计/覆盖/定义示例，最后修改于 `cd9700e` | Runtime 规范化副本名为 `remote-graph-qa`；正文 SHA-256 为 `8b0ffe0aa73a068fd41ce73788111f02378170d1ad7fc5743b7ce1c1669ff31f`，与工作区正文一致 |
| `agent_runtime/skills_runtime.py` 的 `ReadableSkills` | **实际采用**：`ensure_ascii=False` 的技能工具结果传输修复，提交 `cd9700e` | 保留解码后的 JSON 数据；不改默认模型、全局规则、权限或 Agent 循环 |
| `serialization-probe/english.md` | **未采用** | 仅作英文候选回放，未写入运行技能 |
| `compact-probe/candidate.md` | **未采用** | 仅作精简中文候选回放，未写入运行技能 |
| `serialization-probe` 中原转义中文、旧技能对照 | **未作为当前版本采用** | 仅是历史/对照输入；可读 Unicode 的传输行为已由正式修复实现 |
| `failed-*`、`partial-*`、`stale-skill-run` 等中的历史正文 | **历史版本，不是当前加载文件** | 部分有效原则已汇入现版，以当前文件和运行副本为准；缓存旧技能的实验不作为新版验收证据 |

本次收尾不再修改以上实际采用版本，也不因调整失败评价口径而重写 skill。

### 技能传输修复与当前版本的历史验证

随后检查 Agno 实现，发现 `get_skill_instructions` 使用默认 `json.dumps`，把中文编码为字面量 `\uXXXX`；OpenAI-compatible 适配器将该 JSON 字符串原样作为模型消息内容。应用界面后来解码出的正常中文不能证明模型收到的也是可读中文。

`ReadableSkills` 保留 Agno 生成的完整 JSON 数据，只使用 `ensure_ascii=False` 重新序列化技能正文工具结果。它不改变技能内容、权限规则、全局提示词、工具选择或 Agent 循环。真实运行的持久化工具消息从 13,663 字符、2,078 个 Unicode 转义，减少为 3,273 字符、零 Unicode 转义，解码后的同一版正文均为 2,992 字符，见 [actual-transport.json](../../output/remote-graph-test/ux-evidence/serialization-probe/actual-transport.json)。新回归经过真实适配器格式化，先失败后通过；它验证传输行为，不代表回答质量通过。

使用保存的真实首轮查询结果做了四组、每组五次模型回放：原转义中文、可读中文、英文候选和未添加证据规则的旧技能。回放不执行 Agent 工具循环，不替代真实验收。原转义中文五次均增加“特征向量”等释义；可读中文减少此类扩写，但仍存在其他偏差；英文候选没有改善，未采用。完整回答保留在 [serialization-probe/results.json](../../output/remote-graph-test/ux-evidence/serialization-probe/results.json)。这支持传输修复和进一步验证，不足以把转义认定为全部事实错误的根因。

仅修复传输后的两个真实会话保留在 `unicode-live/`、`unicode-confirmation/`，仍未通过逐事实审查。随后在远程技能中增加三个通用例子，说明全库与标签计数、非空范围与总体覆盖、未查询定义与数据库内不存在定义的区别。工具顺序、次数仍由 Agent 决定。

**最终两组真实四轮验收仍未通过**，证据为 [scoped-live/fact-evidence.json](../../output/remote-graph-test/ux-evidence/scoped-live/fact-evidence.json) 与 [scoped-confirmation/fact-evidence.json](../../output/remote-graph-test/ux-evidence/scoped-confirmation/fact-evidence.json)。独立审查确认：

- 主会话第一轮仅查全库数量，却断言所有节点都有 Paper 标签；第三轮在没有读取定义内容的情况下断言库内无映射/元数据。这两处是明确证据缺口。对 `_igraph_import` 的解释则按上文口径区分命名说明与来源证明，不仅凭解释性文字判错。
- 确认会话第四轮建议把写入被拒绝作为只读权限证明。单次写入失败不能证明全部权限，该建议也违反技能对写入验证的限制；实际没有执行写入探针。
- 两组第二轮均实际查询了非空覆盖，数组长度及覆盖结论已有证据；这是具体改善，不抵消其他轮次的失败。

追加验证：相关远程模块、技能 API 和真实 Neo4j 共 **119 项通过**（`serialization-probe/backend-regression.log`）；最终技能/接口 **28 项通过**（`serialization-probe/final-skill-tests.log`）；最终版本的原有八轮 Agent 回归 **通过，135.19 秒**（`unicode-existing-agent/live.log`），含工作区报告、写入拒绝与注入验证。两份独立代码审查均无新的重要问题。Runtime 已重新构建并启动；服务状态、HTTP 200 和凭据扫描记录在 `serialization-probe/verification.json`。

上述回答质量问题归入 B 的未完成项，保留当前默认模型与 Agent 流程。用户已明确撤销此前范围选择的等待，本轮按分项边界交接结束。

后续又对同一份真实首轮证据进行了当前技能与精简中文候选的回放，各五次。精简候选将输出约束集中在末尾、使用明确表格并减少分支说明，但五次仍全部补入未经验证的“特征向量”等确定释义，其中一次再次断言引用关系，因此未采用。十份完整回答保留于 [compact-probe/results.json](../../output/remote-graph-test/ux-evidence/compact-probe/results.json)。这仍是候选筛选实验，不是新的完整 Agent 验收，也不证明所有可能的技能写法均无效。本轮不再开展候选实验。


## 截图

全部显式截图仅在空密码表单或表单以外拍摄；自动 trace、视频、失败截图关闭。

- [连接列表](../../output/remote-graph-test/ux-evidence/connections-list.png)、[新建表单](../../output/remote-graph-test/ux-evidence/connection-form.png)、[当前会话选择](../../output/remote-graph-test/ux-evidence/connection-selected.png)
- [保存成功但绑定失败](../../output/remote-graph-test/ux-evidence/saved-not-used.png)：真实保存，密码已清空
- [查询默认视图](../../output/remote-graph-test/ux-evidence/query-collapsed.png)、[键盘展开技术详情](../../output/remote-graph-test/ux-evidence/query-expanded.png)
- [英文窄屏](../../output/remote-graph-test/ux-evidence/connection-mobile-en.png)、[中文窄屏](../../output/remote-graph-test/ux-evidence/connection-mobile-zh.png)：390px

## 本地环境与范围外事项

本次只读复核时间：2026-09-27 20:26:07（Asia/Shanghai）。完整版本、容器镜像 ID、启动时间、源码 SHA-256、运行技能副本及访问检查见 [handoff/runtime.json](../../output/remote-graph-test/ux-evidence/handoff/runtime.json)。

- Compose 项目 `igraph` 使用 `compose.yaml` + 既有未提交的 `compose.dev.yaml`；专用数据库项目为 `igraph-neo4j-qa`。7 个主项目服务均 running，5 个带健康检查的服务均 healthy；专用 Neo4j 为 running/healthy。Frontend 与 graph-engine-worker 无健康检查字段。
- 当前前端使用 `/app` 源码挂载，容器于 09-24 启动不表示仍在运行旧界面；已逐文件核对 4 个本次变更的前端产品文件，容器文件、工作区和 HEAD 内容相同。Runtime 的技能文件、传输实现也与 HEAD 相同，规范化技能正文与工作区一致。
- 默认模型读取结果仍为 `qwen3.6-plus`。Runtime 容器于 2026-09-27 20:11:08 本地时间启动。上述检查能证明本次相关源码版本，不能把含既有未提交配置的整套开发环境宣称为纯净提交镜像。
- 前端 http://127.0.0.1:21108 、Backend http://127.0.0.1:21109/health 、Neo4j Browser http://127.0.0.1:21174 均返回 HTTP 200。
- 本次只有交接文档更新，没有产品代码修改，故不重新构建或重启健康服务。以前的构建与部署记录仍完整保留。

沿用既有交接记录中的身份防伪限制：Backend 信任有效用户的 X-User-ID，本轮没有扩大为整站认证改造。其他数据库接入留待后续设计。原有开发 Compose 卷标签提示保持原状，没有删除或覆盖卷。
