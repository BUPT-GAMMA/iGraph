# 远程问图连接体验与回答证据交付

起点：`3739a590`，分支 `feat/intelligent-graph-qa`。保留原有未提交改动；不合并、不推送。交付提交 SHA 见最终回复与 `git log -1`。

## 实现范围

- 列表/空状态与独立新建、编辑表单；只展示 Neo4j。
- 保存并使用；保存成功但绑定失败时保留 ID 和最新版本、清空表单密码，重试只绑定。独立测试不保存、不选择，配置改变立即清除旧测试状态。
- 当前会话选择、解除与删除分开；地址示例、服务端 localhost、数据库名、编辑留空密码提示。沿用主题变量，支持中英文、窄屏、焦点移动、Tab 循环和 Escape。
- 账户切换/卸载后的异步结果不会触发新账户的绑定或刷新；运行中操作禁用。错误仅展示受控错误码，不显示原始驱动错误或凭据。
- 查询与结构结果默认展示来源和简洁预览（最多 5 行，长单元格缩短）；查询、参数、版本、连接/查询 ID、完整工具预览在技术详情中。嵌套节点/关系对象也概括展示，原始标识仍可追踪。
- 结构采样、结构截断、底层结果截断、模型预览截断、界面预览缩短和错误均位于折叠区外；无 `source` 的真实工具错误同样可见。
- 证据规则仅修改 remote-graph-qa skill。Agent 自主选择调用顺序和次数；不新增数据库类型，不恢复账号只读接入门槛，不修改全局提示词。

## 回归与真实操作证据

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
| 独立审查 | 两项 P2 均已红灯复现并修复：无来源错误被隐藏、嵌套图对象内部 ID 默认暴露。最终复审无新的重要问题 |

仅运行相关 Python 测试，未声称全量 Python 测试通过。

## 原会话与模型逐事实审查

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
- `existing-agent-before-permission-wording/`：八轮工具行为通过，但拒绝写入的措辞曾将应用限制说成账号只读权限；保留记录，补充权限证据边界。

- `partial-facts-6/`：权限边界解释已正确；长度聚合加上 count(n) 仍没有属性非空数，继续明确“非空数组”范围。
- `refusal-wording-failure/`：模型未提交写入，明确表示“仅允许查询、不支持删除”，旧拒绝措辞正则误报。新增真实措辞回归并扩展识别，每条查询的 EXPLAIN 审计保持不变；`refusal-wording-red.log`、`refusal-wording-green.log` 记录红绿结果。

- `failed-facts-7/`：独立审查指出第 3 轮结论又以公开数据集作类比，并把数组称为特征；另指出全库计数不应直接归于唯一已知标签。保留审查失败，补充中性结论和统计口径要求后重跑。

- `failed-facts-8/`、`confirmation-8/`：重复验证暴露偶发总结扩写和公开数据集类比。将累积规则整理为更短的回答合约后继续验收。

- `failed-facts-9/`：两组原问题均通过，主会话第三轮将导入器说明误列为当前库已确认来源；明确区分导入器文档和连接来源证明后重跑。`passed-confirmation-9/` 的四轮核心数据库事实独立审查通过，但仍有显式未确认的公开数据集类比，记录为表达偏差。

## 转义修复前的真实模型验收：未通过

实际模型：qwen3.6-plus。两次四轮链路测试均完成，实际加载的 skill 正文与当前文件一致；这只说明采集与执行链路成功。

- 主会话 `90588ade-c2fd-43b0-b94f-14f26e04ff21`，连接 `Cora acceptance 23bd9aeb`：结构、全库及按类型计数都有实际查询；但首轮结尾仍把 `x` 解释为“特征向量”，没有独立业务定义支持。后续实际查询维度、非空数、空值数和 label 分布，数值及覆盖有证据；第三轮结论把未获定义扩写为数据库内缺少定义，超出已查范围。
- 确认会话 `512ef935-04e0-4b47-96f2-bda723564f6e`，连接 `Cora acceptance f529a01e`：维度与 label 统计有实际证据；但第三轮仍做公开 Cora 类比并声称数据库缺少元数据。第一轮使用按 Paper 计数称作全库节点总数，查询口径也需要更谨慎。
- 两组对数据库账号权限都正确区分应用限制与账号权限，未用写入探针验证权限。

完整证据：[主会话四轮](../../output/remote-graph-test/ux-evidence/fact-evidence.json)、[确认会话四轮](../../output/remote-graph-test/ux-evidence/confirmation/fact-evidence.json)。直接基准包含节点/关系数、x 长度、Paper/x/label 非空覆盖及 label 分布。

**未完成项：当前模型仍不能稳定遵守全部事实证据边界，回答质量验收未通过。** 最终独立审查确认上述事实问题；已审查代码没有剩余已知阻塞项。 本轮已经落地针对性 skill 改善、真实采集与逐事实审查，但不把绿色测试脚本当作回答质量通过，也不宣称问题已解决。保留此项等待独立验收和后续处理；本轮不擅自更换默认模型、增加第二个 Agent 循环或把规则移入全局提示。

### 后续只读诊断

读取上述两个会话共八轮的 Agno 持久化消息，保存脱敏摘要于 [context-diagnosis.json](../../output/remote-graph-test/ux-evidence/context-diagnosis.json)。没有修改运行配置或追加提示词。

- 两个首轮的 `get_skill_instructions` 正文均与当前文件逐字一致；首轮系统消息中均无 `session_context`。因此，最新首轮的事实偏差不能解释为旧技能副本或已有会话摘要污染。
- 后续系统消息中的自动会话摘要沿用了部分不严谨结论，包括把按标签计数写成节点总数，以及加入公开 Cora 的词袋/主题描述。它可能影响后续回答，但这不是首轮错误的原因，也尚未通过对照实验建立因果关系。
- 持久化消息不是上游 HTTP 请求抓包；历史消息可能在保存时被省略。因此不能据此断言后续请求遗漏技能正文，也不能断言模型服务实际收到的完整上下文与持久化记录完全相同。

诊断结论仍为：规则已在首轮执行链路加载，回答却出现超出证据的表述；当前证据不足以定位更底层原因。单纯继续增加规则没有得到稳定改善，本次保留失败证据交由独立验收，不扩大为模型替换或 Agent 架构改造。

## 技能传输修复与当前版本验收

随后检查 Agno 实现，发现 `get_skill_instructions` 使用默认 `json.dumps`，把中文编码为字面量 `\uXXXX`；OpenAI-compatible 适配器将该 JSON 字符串原样作为模型消息内容。应用界面后来解码出的正常中文不能证明模型收到的也是可读中文。

`ReadableSkills` 保留 Agno 生成的完整 JSON 数据，只使用 `ensure_ascii=False` 重新序列化技能正文工具结果。它不改变技能内容、权限规则、全局提示词、工具选择或 Agent 循环。真实运行的持久化工具消息从 13,663 字符、2,078 个 Unicode 转义，减少为 3,273 字符、零 Unicode 转义，解码后的同一版正文均为 2,992 字符，见 [actual-transport.json](../../output/remote-graph-test/ux-evidence/serialization-probe/actual-transport.json)。新回归经过真实适配器格式化，先失败后通过；它验证传输行为，不代表回答质量通过。

使用保存的真实首轮查询结果做了四组、每组五次模型回放：原转义中文、可读中文、英文候选和未添加证据规则的旧技能。回放不执行 Agent 工具循环，不替代真实验收。原转义中文五次均增加“特征向量”等释义；可读中文减少此类扩写，但仍存在其他偏差；英文候选没有改善，未采用。完整回答保留在 [serialization-probe/results.json](../../output/remote-graph-test/ux-evidence/serialization-probe/results.json)。这支持传输修复和进一步验证，不足以把转义认定为全部事实错误的根因。

仅修复传输后的两个真实会话保留在 `unicode-live/`、`unicode-confirmation/`，仍未通过逐事实审查。随后在远程技能中增加三个通用例子，说明全库与标签计数、非空范围与总体覆盖、未查询定义与数据库内不存在定义的区别。工具顺序、次数仍由 Agent 决定。

**最终两组真实四轮验收仍未通过**，证据为 [scoped-live/fact-evidence.json](../../output/remote-graph-test/ux-evidence/scoped-live/fact-evidence.json) 与 [scoped-confirmation/fact-evidence.json](../../output/remote-graph-test/ux-evidence/scoped-confirmation/fact-evidence.json)。独立审查确认：

- 主会话第一轮仅查全库数量，却断言所有节点都有 Paper 标签；第三轮在没有读取定义内容的情况下断言库内无映射/元数据，还从属性名解释 `_igraph_import` 的含义。
- 确认会话第四轮建议把写入被拒绝作为只读权限证明。单次写入失败不能证明全部权限，该建议也违反技能对写入验证的限制；实际没有执行写入探针。
- 两组第二轮均实际查询了非空覆盖，数组长度及覆盖结论已有证据；这是具体改善，不抵消其他轮次的失败。

追加验证：相关远程模块、技能 API 和真实 Neo4j 共 **119 项通过**（`serialization-probe/backend-regression.log`）；最终技能/接口 **28 项通过**（`serialization-probe/final-skill-tests.log`）；最终版本的原有八轮 Agent 回归 **通过，135.19 秒**（`unicode-existing-agent/live.log`），含工作区报告、写入拒绝与注入验证。两份独立代码审查均无新的重要问题。Runtime 已重新构建并启动；服务状态、HTTP 200 和凭据扫描记录在 `serialization-probe/verification.json`。

当前交付仍含上述回答质量未完成项。已向用户提出后续范围选择：评估其他配置模型、设计额外证据核验机制，或按已知限制验收本轮；在用户决定前保持当前默认模型与 Agent 流程。

后续又对同一份真实首轮证据进行了当前技能与精简中文候选的回放，各五次。精简候选将输出约束集中在末尾、使用明确表格并减少分支说明，但五次仍全部补入未经验证的“特征向量”等释义，其中一次再次断言引用关系，因此未采用。十份完整回答保留于 [compact-probe/results.json](../../output/remote-graph-test/ux-evidence/compact-probe/results.json)。这仍是候选筛选实验，不是新的完整 Agent 验收，也不证明所有可能的技能写法均无效。当前没有新的已验证修复；后续范围选择尚待用户回复。


## 截图

全部显式截图仅在空密码表单或表单以外拍摄；自动 trace、视频、失败截图关闭。

- [连接列表](../../output/remote-graph-test/ux-evidence/connections-list.png)、[新建表单](../../output/remote-graph-test/ux-evidence/connection-form.png)、[当前会话选择](../../output/remote-graph-test/ux-evidence/connection-selected.png)
- [保存成功但绑定失败](../../output/remote-graph-test/ux-evidence/saved-not-used.png)：真实保存，密码已清空
- [查询默认视图](../../output/remote-graph-test/ux-evidence/query-collapsed.png)、[键盘展开技术详情](../../output/remote-graph-test/ux-evidence/query-expanded.png)
- [英文窄屏](../../output/remote-graph-test/ux-evidence/connection-mobile-en.png)、[中文窄屏](../../output/remote-graph-test/ux-evidence/connection-mobile-zh.png)：390px

## 本地环境与范围外事项

当前 Compose 项目为 `igraph`（`compose.yaml` + `compose.dev.yaml`），专用 Neo4j 为 `igraph-neo4j-qa`。受影响 Runtime 和 Frontend 已重建启动；7 个主项目服务 running，其中 5 个有健康检查的服务全部 healthy；专用 Neo4j healthy；前端与 Backend 健康接口均 HTTP 200，见 `deployment.json`。前端 http://127.0.0.1:21108 ，Backend http://127.0.0.1:21109/health 。

沿用既有交接记录中的身份防伪限制：Backend 信任有效用户的 X-User-ID，本轮没有扩大为整站认证改造。其他数据库接入留待后续设计。原有开发 Compose 卷标签提示保持原状，没有删除或覆盖卷。
