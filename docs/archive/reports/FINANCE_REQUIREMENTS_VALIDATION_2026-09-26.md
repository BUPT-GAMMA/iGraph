# iGraph 金融股权需求验证报告

> 仓库发布说明：服务器连接信息已替换为占位符。文中 `artifacts/` 为本地保留的验收证据，不随 Git 提交；原始 CSV、PPT 和 citics 参考包也属于外部输入。仓库提供实现、测试及验收摘要；真实数据测试需设置 `IGRAPH_FINANCE_TEST_DATA`。

**验证日期：2026-09-26**

**目标：第一大股东、控股链、集团树**
**iGraph 代码基线：`7c3ff6a7bd8edb640ccb012f4f084e606d9d1689`**

## 1. 当前结论与适用范围

从代码审计、完整源数据统计、现有构图库实测和 citics 参考算法交叉验证看，**iGraph 已具备多表上传、主外键配置、关系构图、属性保留及通用图分析基础，但尚不能把当前内置能力视为已满足 PPT 中的三项金融业务要求。** 差距集中在持股业务投影、规则优先级、严格控制闭包、完整经济权益累计、SCC 状态、集团语义及审计交付。

| 需求 | 当前内置能力判断 | 主要缺口 |
|---|---|---|
| 第一大股东 | 未具备符合 PPT 的现成业务计算契约 | GP/法人优先；直接过半与根累计分层；完整累计及并列；特殊主体规则；数据缺失/冲突与截断状态。 |
| 控股链 | 通用路径与可达性不能完成该判定 | 按受控主体可支配表决权执行严格 `>50%` 固定点；中间/最终控制人分离；贡献证据；SCC 复核。 |
| 集团树 | 有通用图展示与树/DAG 算法，缺金融集团结果模型 | 按确认控制成员生成层级视图；保留多父/交叉边及环；分离 CONTROL/PATH/AFFINITY；限制 primary 的来源。 |

这不是“iGraph 无法通过任何方式计算这些问题”的结论。系统注册了可执行 Python 的 `workspace_bash`，Agent 可以尝试编写定制分析脚本；该路径是否成功，要以本次真实会话的数据可见性、执行记录、代码及独立结果比对为依据。**定制脚本得到正确答案**和**产品已经原生支持可重复的金融业务分析**分别记载。

服务器隔离部署、浏览器上传、构图优化、项目导入和有界可视化已完成；已保存 revision 1，最终为 **24,823 节点、74,820 边**。对服务器最终图逐行审计确认，19,436 条原始记录的所有字段均保留，但仍缺完整的数值持股业务投影。原生会话实际完成 `get_graph_info`、`get_constructed_graph` 两次工具调用后，因全图工具结果超过模型供应商的字符串长度上限而报错，**没有产出三项业务结论**。原生流程本次未通过端到端业务验收。

独立 workspace 定制脚本路径也已实际补测，但完整脚本多次被工作区命令规则拒绝，仅完成附件预览和一个 14 字节测试文件写入，**没有有效执行金融计算，也没有生成 Derived 脚本或结果 JSON**。公开取消接口返回 409，随后本次验证操作只重启隔离实例的 agent-runtime 终止重复尝试；重启后快照仍保留陈旧 running，不作为自然完成或应用已正确置 error 的证据。两条在线路线均未完成三项业务验收，离线独立算术和 citics 结果单独保留，不能冒充 iGraph 会话输出。

本报告依据用户提供的 PPT 规则对输入快照进行技术验收。“第一大股东”“控制根”“经济权益候选”均指明确算法和规则口径下的计算结果，不把这些结果自行扩展为现实中的股东身份、最终受益人或控制关系的权威认定。本次 CSV 标注为合成测试数据，未提供业务部门签署的逐目标真值标签。

## 2. 验证资料、版本与方法

### 2.1 输入资料

| 资料 | 实际位置 | 本次用途 |
|---|---|---|
| 需求 PPT | `F:\mypaper\data\finance\tasks.pptx` | 提取全部 17 页文本，以其规则和输出约束作为验收依据。 |
| 三份 CSV | `F:\mypaper\data\finance\finance_csv` | 完整读取，不以预览样本代替全量统计。用户原消息中的 `finance\_csv` 与磁盘实际目录不同。 |
| citics 参考实现 | `F:\mypaper\data\finance\citics\citics` | 只读审计及运行既有测试、真实目标和风险复现。参考版本 `0.5.0`。 |
| iGraph | `F:\mypaper\iGraph` | 代码审计、实际调用现有构图库、服务器部署和端到端验证。 |

源文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `individual_info.csv` | `42488b995e05eb3e0ba99e7ea1961518246c4de4e81abafc1dd04ad35c04fa0a` |
| `institution_main.csv` | `32f45bc436a8dd4d9adb162b44eef3919b6754de1a36e1e865b48720d68367f9` |
| `institution_shareholder_relationship.csv` | `30fcd35b17e4e31010721d4438875cfe421bf0e87c66ce1aeec942ac48c39855` |

### 2.2 证据分级

1. **需求证据**：PPT 页码及逐页文字提取。没有把未读取的图形连接关系当作额外规则。
2. **源数据证据**：标准库 CSV 解析，核对全部 ID、类型、比例、快照、重复记录、SCC 及股份总和。
3. **独立算术基线**：使用 Python `Decimal` 对小型无环目标枚举完整上游路径，保留源 CSV 行号；另行实现每个受控股东只贡献一次的严格控制闭包。不调用 citics 或 iGraph 业务算法作为自身真值。
4. **实现证据**：iGraph 代码定位、实际构图库输出和 citics 独立执行结果。
5. **部署与运行证据**：服务器构建、健康检查、SSH 隧道、浏览器上传、构图状态和实际 Agent 工具调用，由主验证流程保存。

代码存在某个算法、构图状态为 ready、SSE 流结束或大模型给出文字答案，均不足以单独证明业务正确。三项需求必须分别核对图语义、算法语义、数值/成员结果、完整性和来源证据。

## 3. PPT 需求解释与验收约束

### 3.1 第一大股东

PPT 第 14 页规定顺序：

1. 私募合伙企业匹配到 GP/执行事务合伙人或法人时，应用优先规则。
2. 其次检查直接持股严格大于 50% 的主体；全部满足者均保留，多个主体满足时标记 `DATA_CONFLICT`。
3. 再比较全部有效路径按根累计的经济权益；并列最高者全部保留，并采用稳定排序。

PPT 第 3、12、17 页要求第一大股东锚点和最终控制人分开保存。直接第一大股东可能是中间公司，经济权益累计第一的根也可能与最终控制根不同，结果不可只用一个 `owner_id` 混合表达。

路径贡献为 `Π ratio(e)`，根贡献为全部有效路径贡献之和（第 13 页）。**Top-K 只限制证据展示，不得参与根排名的完整累计计算**（第 4、13 页）。达到预算后必须报告截断和结论范围，不能把局部最高候选当作全图最终根。

### 3.2 控股链

PPT 第 11 页采用严格 `>50%` 的控制闭包。候选控制主体控制某公司后，该受控公司对外持有的直接股份可作为候选主体可支配表决权的贡献。每个贡献主体只计一次，并保留其边证据。

例如 A 持有 B 60%、B 持有 C 60%：A 经此路径的经济权益为 36%，而控制闭包仍确认 A 对 C 的传递控制。经济权益乘积不能替代表决权闭包，普通图可达也不能替代控制判定。恰 50% 不自动确认单一控制人。

PPT 第 7—9、12 页进一步要求：持股图和派生控制图分离；传递边可以回放中间链与原始 `edge_id`；SCC 是结构组件，不是自动合并的控制人，内部硬控制保留 `SCC_REVIEW`。

### 3.3 集团树及派系

PPT 第 6、15—17 页把结果分为：

| 证据类别 | 回答的问题 | 对集团结果的约束 |
|---|---|---|
| CONTROL | 谁能支配表决权；哪些成员属于严格控制关系。 | 只有满足严格控制规则的派系可填确认集团的 primary。 |
| PATH | 哪些路径贡献经济权益。 | 可解释和形成候选，不直接认定控制成员。 |
| AFFINITY | 哪些主体在网络上较接近。 | PPR/社区只能生成关联候选，不直接认定控制成员。 |

“集团树”应建立在明确类型的结果上。严格控制图可能存在交叉边、多父、成员重叠和 SCC，因此前端可以提供层级树主视图，但不能为得到单树而丢弃这些关系。第一大股东锚点树和严格控制集团树应明确区分。

PPT 第 15 页已经指出无向 PPR 丢失控制方向；第 16 页把 Leiden/Louvain 定位为后续全图候选层。引入社区算法本身不会补足首大和控制的业务语义。

### 3.4 共同输入、环路和状态要求

| 要求 | PPT 依据 | 验收解释 |
|---|---|---|
| 统一快照、有效持股、标准比例 | 第 3—5 页 | 比例归一化 `[0,1]`，保留原始值、主体类型和边标识。 |
| 索引及投影 | 第 4—5 页 | 正向持股 `股东→企业`、反向股东索引、独立无向关联投影。 |
| 经济权益成环 | 第 7—10 页 | SCC 内解 `(I-A)s=d`，提供成员值、残差和收敛状态；不能用简单删环代替。 |
| 预算及完整性 | 第 2—4、7、17 页 | 分开记录查询范围、累计计算、成员列表、展示路径的截断；截断经济累计仅是下界。 |
| 特殊主体规则 | 第 3、14 页 | 注销、中登、港结算、政府层级和特殊集团应可配置、有版本和停止原因。 |
| 审计输出 | 第 12、17 页 | 输出控制、首大、经济权益、路径和派系结果，绑定来源边、规则和状态。 |

PPT 第 3 页提到旧口径 5/15 层、最多 30 条路径；没有明确要求把每条低于 15% 的持股边排除。citics 默认的 15% 逐边过滤需要作为有名称的兼容策略说明，不能默认为 PPT 的完整事实口径，也不能在无环与 SCC 分支使用不同语义。

## 4. 源数据审计

### 4.1 表结构与业务图规模

| CSV | 记录数 | 字段数 | 身份字段 |
|---|---:|---:|---|
| `institution_main.csv` | 5,000 | 35 | `UUID`，唯一机构 ID。 |
| `individual_info.csv` | 1,000 | 7 | `IDV_ID`，唯一个人 ID。 |
| `institution_shareholder_relationship.csv` | 13,436 | 16 | `UUID` 是被投企业 ID，只有 4,409 个不同值，不能作为关系主键。 |

三表快照日期均为 `2026-09-23`，UTF-8 编码。主体 ID 自带 `SYN-ENT-`/`SYN-IDV-` 前缀，本样本没有跨类型 ID 冲突。

正确业务图的基本映射是：

```text
from_id     = REL_SHRHLR_CUST_ID                 # 股东
to_id       = UUID                              # 被投企业
ratio       = parse_percent(REL_SHRHLR_HOLD_SHR_RTO)
holder_type = REL_SHRHLR_TP_CODE                 # 0 机构，1 个人
snapshot_id = STATC_DT
edge_id     = 确定性编码(源表, 快照, 股东ID, 被投企业ID)
```

例如 `57.1429%` 应转换为 `0.571429`。原始关系表没有天然边 ID；当前 `(REL_SHRHLR_CUST_ID, UUID, STATC_DT)` 复合键唯一，但通用导入设计仍需明确重复事实、不同证券类别和不同有效期的处理方式。

完整业务图应保留 **6,000 个主体、13,436 条持股边**，其中 88 个主体完全孤立。仅从边创建节点会得到 5,912 个主体，遗漏实体表中的合法孤立记录。

### 4.2 完整性、重复及质量异常

| 检查项 | 实测 |
|---|---|
| 重复主体 ID、完全重复记录 | 均为 0。 |
| 重复股东—目标对、自环 | 均为 0。 |
| 股东/目标 ID 引用失配、股东类型失配 | 均为 0。 |
| 机构股东边 / 个人股东边 | 8,975 / 4,461。 |
| 股比范围 | 0.3967%—75%；无单边超出 `[0,100%]`。 |
| 直接严格过半边 / 恰 50% 边 | 1,285 / 2。 |
| 已记录股比之和 >100% | 13 家，最高 123%。 |
| 已记录股比之和 <100% / =100% | 4,396 家 / 0 家。 |
| 没有已记录股东的机构 | 591 家。 |
| 机构状态 | 存续 3,894；在业 789；注销 275；吊销 42。 |
| 全部主体 SCC 数 | 5,980，包含孤立主体。 |
| 非平凡 SCC | 10 个：二节点 4 个、三节点 3 个、四节点 2 个、五节点 1 个。 |

股比超限目标均为 `SYN-ENT-` 前缀：`000122`、`000181`、`000183`、`000244`、`000321`、`000323`、`000421`、`000542`、`000621`、`000622`、`000761`、`000901`、`000902`。它们都在交叉持股组件中，但交叉持股不免除同一被投企业股份总和的输入校验。

数据覆盖不足 100% 不自动意味着数据错误，但意味着不能假定未知股权不存在。“没有更上游记录”只能证明输入内到达边界，不能证明现实中的最终根。对超 100% 数据也不能通过静默归一化改变原始事实后输出确定结论，应保留异常与采用的处理策略。

字段缺失主要是：机构 `REFR_UUID` 和个人 `IS_IDV_GRP_TOP_PNT` 全空；关系表中机构股东字段/个人股东字段的空值随类型互斥，不能把这些条件性空值误判为缺失股东。

### 4.3 无法从三表可靠补出的业务规则

机构 `LGL_RPRT` 都有姓名且可在个人表中按姓名匹配，但有 **29 个个人姓名重名，影响 290 家企业的法人姓名匹配**。姓名匹配不是可靠身份关系；数据未提供足够的 GP/执行事务合伙人关系及有效期，不能通过字符串猜测满足 PPT 的最高优先级规则。

同样，政府主体层级、特殊集团名单及断开策略没有提供可直接执行的权威配置；“吊销”应如何处理也不等于已经规定为“注销”。这些是规则/数据待补项，结果应显式给出 `RULE_DATA_MISSING` 或相应未决状态，而不是默认该规则不适用。

## 5. iGraph 当前能力及直接代码证据

### 5.1 数据能够保存，但关系图不等于股权图

多 CSV 上传、schema 和 PK/FK 已有完整入口：前端 [api.ts:305](../../../frontend/src/api.ts#L305)、[CsvBundleDialog.tsx:153](../../../frontend/src/components/CsvBundleDialog.tsx#L153)，后端 [app.py:3335](../../../backend/app.py#L3335) 和 [project_datasets.py:165](../../../backend/project_datasets.py#L165)。

初始构图 [sources.py:1020](../../../graph_engine/construction/sources.py#L1020) 的契约是保留每一输入行；第 1031 行把原始字段放入 `properties.data`，第 1051 行开始按“子表行→父表行”产生外键边。节点通过 [sources.py:153](../../../graph_engine/construction/sources.py#L153) 的表名/主键哈希生成 `row_...` 标识，业务 ID 仍是属性。

据此，三表原样输入产生的是“持股记录节点同时连接股东主体和被投主体”的关系图。它保留了原值，却没有自动形成 `股东→被投企业` 的类型化持股边。不能把保留属性误写为丢失数据，也不能把属性存在误写为金融算法已能正确消费。

边权兼容逻辑 [property_graph.py:25](../../../graph_data_service/property_graph.py#L25) 只读取边顶层 `properties.weight`，缺省为 1.0。源 CSV 的百分号字符串不会自动成为标准持股比例或金融表决权属性。

### 5.2 通用算法与业务算法的差别

| 当前代码位置 | 实际能力 | 为何不足以验收 |
|---|---|---|
| [tool_catalog.py:376](../../../graph_data_service/tool_catalog.py#L376) | Dijkstra/跳数最短路及 K 最短路。 | 不计算 GP/法人优先、全部根经济累计或严格控制。 |
| [tool_catalog.py:866](../../../graph_data_service/tool_catalog.py#L866) | 源到目标的简单路径，默认深度限制。 | 路径展示截断不能充当根排名完整累计。 |
| [tool_catalog.py:943](../../../graph_data_service/tool_catalog.py#L943) | 祖先、后代和拓扑传递闭包。 | 可达关系不等于表决权控制固定点。 |
| [tool_catalog.py:1045](../../../graph_data_service/tool_catalog.py#L1045) | 连通性与 SCC 缩点。 | 缺环内权益方程、成员级经济值和金融复核状态。 |
| [tool_catalog.py:1070](../../../graph_data_service/tool_catalog.py#L1070) | 通用树/DAG、生成树、支配点。 | 拓扑支配点不是金融控制人，生成树也不定义集团成员。 |
| [GraphWorkspace.tsx:20](../../../frontend/src/components/GraphWorkspace.tsx#L20) | force/radial/circle 布局。 | 缺集团层级树及控制贡献、交叉边、SCC 复核交互。 |
| [domain.py:69](../../../graph_engine/construction/domain.py#L69) | finance 领域的交易/账户优化先验。 | 选择 finance 不会自动启用股权三项规则。 |

构图优化确实支持桥接行折叠等操作（[tools.py:168](../../../graph_engine/construction/tools.py#L168)），但候选选择和具体方向不能代替明确的批量持股投影契约；应核对实际最终图，而不是根据存在 `collapse_node` 推断全表持股已规范化。

### 5.3 沙箱定制路径的能力边界

[agno_factory.py:77](../../../agent_runtime/agno_factory.py#L77) 注册 workspace 工具，[workspace_tools.py:583](../../../agent_runtime/workspace_tools.py#L583) 提供 `workspace_bash`。因此，本次端到端会话应分别记录：

- 是否用现成图工具完成；若失败，失败发生在数据寻址、图语义、算法还是资源预算。
- 是否转为读取 Uploaded 中的 CSV 并编写 Python；若完成，保存实际脚本、输入校验值、完整结果和独立比对。
- 结果是否回到可复用项目图、业务 API 和前端视图，还是仅为一次性文件/聊天结论。

原生会话未进入 workspace 计算：在取得全图后即因模型请求超限结束为 error。独立补测会话 `b61ab217-0fda-471b-bae3-e48ae7a183fc` 已验证 Uploaded 三个附件可访问，但没有成功执行完整分析脚本；具体工具计数、规则拒绝和操作终止证据见第 9.4 节。由此可确认的是通用 workspace 入口存在且部分命令可执行，不能确认本次金融定制计算已经可用。

## 6. 已执行的离线构图验证

### 6.1 最小语义复现

用当前 `initial_graph → to_graph_data` 输入三家公司 A/B/C 和两条记录（A 持有 B 60%、C 持有 B 40%），实际得到：

```text
节点数：5
外键边：持股记录h1→A，h1→B，持股记录h2→C，h2→B
边权：全部1.0
原始比例：仍保存在持股记录节点属性中
```

这证明当前关系构图的保真性与股权语义之间存在明确转换步骤。对这四条单位权外键边调用最短路，不能得到有业务意义的第一大股东或控制链。复现代码：micro_graph_semantics.py（本地证据：`artifacts/finance-validation/micro_graph_semantics.py`）。

### 6.2 全量三表初始构图

已调用当前 `Table`、`ForeignKey`、`RelationalSource`、`initial_graph` 完整处理三份 CSV，没有仅抽样，也没有改写原文件。配置如下：

```text
institution_main: PK [UUID]
individual_info: PK [IDV_ID]
institution_shareholder_relationship:
  PK [REL_SHRHLR_CUST_ID, UUID, STATC_DT]
  FK REL_SHRHLR_CUST_ID → institution_main.UUID
  FK REL_SHRHLR_CUST_ID → individual_info.IDV_ID
  FK UUID → institution_main.UUID
```

实际结果为 **19,436 个记录节点、26,872 条外键边**。数量可独立解释为：5,000+1,000+13,436 行节点；8,975 机构股东引用+4,461 个人股东引用+13,436 被投机构引用。

本地初始构图用时约 0.671 秒，该数值不含优化、网络上传、持久化和业务分析，不代表整体系统响应时延。

两个股东外键共享来源列，对每一条记录只有一种主体类型适配，因此另一种外键分别产生 4,461/8,975 个 dangling 提示。本数据所有股东 ID 都存在，这些提示反映的是普通 FK 缺少多态引用语义，不应据此误报源数据股东缺失。

证据：raw-csv-schema.json（本地证据：`artifacts/finance-validation/raw-csv-schema.json`）、initial-graph-oracle.json（本地证据：`artifacts/finance-validation/initial-graph-oracle.json`）。服务器最终图已经下载并完成独立全量审计：原始记录与外键完整，部分个人股东已生成方向正确的捷径，但机构股东及数值比例仍未形成统一业务投影，详见第 9.2 节。

## 7. 独立验收样本及预期

以下数值基于输入快照；没有静默补全未知股份、GP/法人或特殊主体规则。完整路径、源 CSV 行号及精确数值见 independent-oracles.json（本地证据：`artifacts/finance-validation/independent-oracles.json`）。为避免前缀歧义，正文中机构简写均对应 `SYN-ENT-`，个人明确注明 `SYN-IDV-`。

### 7.1 第一大股东、控制根和经济根分离：000044

目标 `SYN-ENT-000044` 的直接股东为机构 000040（60%）、000012（8%）、000025（5%），以及个人 000013/000814（各 3%）。没有适用更高优先级规则时，直接首大候选是 **`SYN-ENT-000040`**。

机构 000001 持有 000040 的 57.1429%，000040 持有 000044 的 60%，因此严格控制链为：

```text
SYN-ENT-000001 => SYN-ENT-000040 => SYN-ENT-000044
```

该路径的经济权益为 `0.571429 × 0.60 = 0.3428574`，即 **34.28574%**；000001 通过受控 000040 可支配对 000044 的 60% 表决权，仍确认传递控制。

完整上游共有 33 条路径、13 个根，经济权益累计最高根为个人 `SYN-IDV-000957` 的 **13.71426%**。此人不能替代直接首大或控制根。该目标的所有上游机构均为存续/在业，适合基础验收。

### 7.2 集团树：控制根 000001

独立严格控制闭包得到成员恰为 000040、000044：

```text
SYN-ENT-000001
└── SYN-ENT-000040  直接持股57.1429%，确认控制
    └── SYN-ENT-000044  直接持股60%，确认控制
```

可另外展示派生边 `000001 => 000044`，但必须回放这两条原始持股边。不能把弱持股可达节点、经济路径节点或 PPR 邻近节点全部归为确认集团成员。本结果只核验控制成员，不凭合成公司名称认定集团名称。

### 7.3 并列首大：000013

个人 `SYN-IDV-000350` 与 `SYN-IDV-000685` 各持有 20%；另外两个机构股东分别为 000010（10%）、000001（5%）。完整上游有 16 条路径、11 个根，两个个人仍以累计 20% 并列第一，必须同时保留并稳定排序。

两个 20% 首大候选不能因此变成单一确认控制人。该目标全部上游机构均为正常存续/在业。

### 7.4 恰 50% 边界：003784

机构 `SYN-ENT-003710` 对 `SYN-ENT-003784` 持股恰 50%，其他直接比例为 34%、6.6667%、5.3333%。独立闭包显示 003710 没有被确认控制的企业，不能仅从该 50% 边输出控制关系。

补充边界样本是 `SYN-ENT-004697 → SYN-ENT-004816`，同样为 50%。这类检查限定的是“不能从恰 50% 单边认定控制”，不凭此断言其他上游主体绝无受控公司合计贡献。

### 7.5 多路径累计胜过最大单路径：000041

**本样本关闭注销中止，仅验证原始事实图中的累计算法。** 目标 000041 有 81 条完整上游路径、18 个根：

| 根 | 路径数 | 根累计经济权益 | 最大单路径 |
|---|---:|---:|---:|
| `SYN-IDV-000692` | 5 | **1.19256519764553408%** | 0.54% |
| `SYN-IDV-000723` | 1 | 0.87544152% | **0.87544152%** |

完整根累计应选 000692，直接取最大单路径会误选 000723。000692 的路径经过已注销机构 000017，部分还经过已注销 000008；启用注销中止后必须重新计算，不能沿用上述业务结论。此样本同时要求结果声明使用的规则版本和过滤口径。

### 7.6 SCC、数据冲突和注销

| 目标 | 事实 | 应验证的行为 |
|---|---|---|
| 000621—000625 | 一个五节点 SCC；621/622 入股和分别 104%/104.0001%。 | 保留成员级环证据与 `SCC_REVIEW`；报告数据异常。PPT 第 10 页是另一套演示比例，不可套用其数值。 |
| 000901/000902 | 相互持股 26%/22%；901 入股和 123%。 | 同时呈现交叉持股和数据冲突；不能输出一个没有异常说明的确定集团树。 |
| 000072 | 000003→000023 为 62.6087%，000023→000072 为 60%；000023 已注销。 | 启用注销中止后不能继续把经注销节点穿透的上游根作为确定结论；终止节点如何保留需规则定义。 |

## 8. citics 参考实现核验与复用边界

### 8.1 已执行结果

参考实现的 44 项既有单元测试全部通过。三表导入得到快照 `enterprise-tables:2026-09-23`、6,000 节点、13,436 条边且无重复边 ID。另执行 7 个真实目标与 4 类最小风险复现，原始结果保存在 citics-reference-results.json（本地证据：`artifacts/finance-validation/citics-reference-results.json`）。

可复用部分包括：三表适配器 [io.py:39](<本地数据目录>/data/finance/citics/citics/faction_engine/io.py:39)、非递归 SCC [graph.py:30](<本地数据目录>/data/finance/citics/citics/faction_engine/graph.py:30)、经济求解 [engine.py:329](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:329)、严格控制闭包 [engine.py:525](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:525)、首大计算 [engine.py:1420](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:1420)。

| 真实样本 | 参考输出 | 与独立基线的关系 |
|---|---|---|
| 000044 | 首大 000040；理论最终根 000001、中间 000040。 | 关键数值关系相符，但局部范围扩至 4,096 节点并截断，完整性需分项记录。 |
| 000013 | 两个个人 000350/000685 各 20% 并列；无单一理论控制人。 | 候选相符；并列应有明确 tie 字段/状态。 |
| 003784 | 无理论控制人；默认首大计算截断。 | 不能从 50% 认控制；首大仍须保留预算边界。 |
| 000041 | 默认 15% 逐边过滤/5 层得到截断候选；全正股比/30 层得到个人 000692 及 5 条累计路径。 | 后一算法口径与独立算术相符，不能省略注销过滤关闭的条件。 |
| 000621/000901 | 硬控制 `SCC_REVIEW`，但仍产生首大结果。 | 104%/123% 输入异常没有得到足够阻断。 |
| 000072 | 首大 000023，理论根 000003。 | 穿过已注销 000023，说明注销中止未实现。 |

### 8.2 不应原样迁入的风险

| 问题 | 实现证据/复现 | 集成前约束 |
|---|---|---|
| GP/法人优先缺失 | 首大入口从股权直接开始；节点模型没有该身份关系。 | 增加权威实体关系及规则层，缺数据时返回未决。 |
| 默认把截断局部候选当确定结果 | [engine.py:20](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:20) 默认 `FINAL_LOCAL_RESULT`；[engine.py:1537](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:1537) 可在 `TRUNCATED` 时填确定首大。 | 默认保守候选策略；局部结果不能复用为全图最终根。 |
| 成员限量漏报 | 预算 2 的 A→B80%→C80% 只返回 A/B，但仍可 `complete=true`。 | 查询范围、成员、累计、证据各自报告完整性并向上传播。 |
| 重复与股份总和缺校验 | 重复 A→T30% 可累加为 60%；A60%+B45% 可仍返回首大 COMPLETE。 | 先完成重复事实与总比例质量校验，不静默修正。 |
| 注销/吊销状态未执行 | 适配器读取状态，但引擎没有消费；000072 已复现。 | 明确停止策略、保留事实节点和原因。 |
| 无环与 SCC 过滤/深度口径不一致 | [engine.py:1420](<本地数据目录>/data/finance/citics/citics/faction_engine/engine.py:1420) 默认 5 层/15%；经济 SCC 分支另行读取完整祖先图。 | 同一策略在所有结构上使用一致的有效事实与完整性语义。 |
| 全量 Python 对象图及重复扫描 | 原型一次读取 CSV；经济分析反复扫描组件相关边。 | 生产化需索引、缓存、异步离线预计算和独立查询预算。 |

参考代码测试通过证明其已有测试与实现一致，不证明已覆盖 PPT 全部规则。其自身定位为算法原型，适合复用经过修正的计算核心，不宜整体无条件搬入 iGraph。

## 9. 服务器部署、SSH 隧道与端到端记录

### 9.1 已确认的部署信息

以下来自本次主验证流程已记录的实际操作，不表示后续业务验收已通过：

| 项 | 值 |
|---|---|
| 服务器 SSH | `<server-user>@<server-host>:<ssh-port>` |
| 隔离 Compose 项目 | `igraph-finance-20260926` |
| 服务器代码目录 | `/home/<server-user>/apps/igraph-finance-20260926/repo` |
| 代码一致性 | 本地基线为 `7c3ff6a7bd8edb640ccb012f4f084e606d9d1689`；822 个文件的哈希与服务器一致。 |
| 服务状态 | 已记录 6 个服务 healthy、worker running；最新检查详见 `ops/health-final.txt`。本次仅重启隔离实例 agent-runtime 终止 custom 重复尝试，原有实例未替换。 |
| 远程前端 | `127.0.0.1:22173` |
| SSH 转发后本地访问 | `http://127.0.0.1:22173` |
| 浏览器账户 | `admin` |
| 原始个人数据集 ID | `88f6ceec-c9a6-400c-962d-f5c1988278d7` |
| 项目 ID | `df6df4e2-1fbd-4443-abac-dae3088a6c61` |
| 构图配置 | `rules`、`finance`、query 15、经验关闭。 |
| 初始关系图 | 19,436 节点、26,872 边。 |
| 保存后的最终构图 | `ready`、revision 1；24,823 节点、74,820 边。 |
| 项目内独立副本 | `0b9e876d-396f-4a4c-abfd-ef455bb35963`，`ready`，设为项目默认数据集。 |

浏览器已上传原始三份 CSV 并配置 PK/FK。实际保存的关系表复合 PK 顺序为 `[UUID, REL_SHRHLR_CUST_ID, STATC_DT]`；其键集合与离线基线一致，但哈希节点 ID 受顺序影响，因此会话工具使用服务器最终图的业务 ID 映射，不能复用按另一顺序生成的本地 row ID。配置和副本状态见 project-datasets.json（本地证据：`artifacts/finance-validation/project-datasets.json`）。

SSH 隧道转发模板如下；请替换密钥路径、SSH 端口和服务器账户：

```powershell
ssh -N -T -i "path\to\private_key" -p <ssh-port> -L 127.0.0.1:22173:127.0.0.1:22173 <server-user>@<server-host>
```

实际后台隧道还配置了 `ExitOnForwardFailure` 与存活探测参数，完整重连和 Compose 操作见 OPERATIONS.md（本地证据：`artifacts/finance-validation/ops/OPERATIONS.md`）。本地 Docker daemon 未运行，本次按用户指定在服务器隔离 Compose 项目中重建并启动服务。服务镜像采用服务器现有且 requirements 哈希一致的依赖基底，重新复制当前受版本控制源码；前端经本地 TypeScript/Vite 生产构建后打包。源码 822 文件比对无差异，证据为 source-verification.json（本地证据：`artifacts/finance-validation/ops/source-verification.json`）。重启后的最新服务检查见 health-final.txt（本地证据：`artifacts/finance-validation/ops/health-final.txt`），隧道访问返回 HTTP 200，证据见 tunnel-final.json（本地证据：`artifacts/finance-validation/ops/tunnel-final.json`）。没有修改应用源码或创建 Git 提交。

### 9.2 最终构图与项目载入

| 检查项 | 实测状态/证据 |
|---|---|
| 构图优化终态及耗时 | `ready`，revision 1；progress 记录 195.613 秒（约 196 秒）；完成事件时间 `2026-09-26T14:06:41.753695+08:00`，持久化状态更新时间 `14:06:48.614706+08:00`。 |
| 最终节点/边数量、类型及变更 | 24,823 节点、74,820 边；7 个候选中采用 1 个“组合结构优化”。新增 5,387 个属性实体、19,436 条属性连接、28,512 条两跳捷径，见下述全量审计。 |
| 持股方向、比例位置及标准化 | 4,461 条个人→机构捷径方向正确；8,975 条机构持股缺对应主体间直接投影。全部 74,820 条边无顶层 `properties.weight`，加载有效权重为 1.0；比例仍在持股行节点中作为百分号字符串保存。 |
| 原值、业务 ID、快照、来源边保留 | 19,436/19,436 条源记录全字段相同；缺失、重复、字段变化均为 0；26,872/26,872 原始 FK 完全匹配；6,000 个业务主体 ID 映射无遗漏。 |
| 构图验收报告与异常 | `failure_code=null`，构图工作负载最终校验通过；逐行审计确认保真。该验收范围不包含首大/控制/集团业务算法；原生分析另因工具输出超限失败。 |
| 导入项目的数据集副本 ID | `0b9e876d-396f-4a4c-abfd-ef455bb35963`，`ready`，来源为个人数据集 revision 1；详见 project-datasets.json。 |
| 实际分析会话 ID、模型与运行参数 | `f683fb6a-dc72-42eb-8f0f-4caa02b54c55`；run `86240201-22b1-411d-93f1-5950c2a67cd4`；`qwen3.6-plus`、reasoning=`direct`、planning=`auto`。 |
| 图加载、节点寻址与可视化 | 图工具已取得实际项目图；独立保存 6,000 主体业务 ID→row ID 映射。有界可视化 HTTP 200、success=true，实际 80 节点/83 边，详见下文。 |

此状态的原始证据为 construction-current.json（本地证据：`artifacts/finance-validation/construction-current.json`），build ID 为 `43d395dc-6cdc-4008-8980-a9c559f04d81`。浏览器显示 Saved Version 1，随后从项目 Load constructed graph 完成导入；项目副本已就绪，后续图工具和可视化实际读取了该图。

最终图原始响应为 **53,113,422 字节**，SHA-256 为 `60770555060660f2b61090c45bf5cc3bd1c1393690b591f2952d6b062bda8ea5`。审计直接比较完整文件和原始 CSV，不依赖前端抽样。节点/边 ID 无重复、边端点全部存在；三个原表的 19,436 行仍全为记录节点。新增节点为 10 个一级行业实体、4,409 个企业名称实体、968 个个人姓名实体；这些是属性索引，不能当作新增公司/个人，尤其 968 姓名实体不等于只有 968 个自然人。

两跳捷径中，24,051 条为持股记录→持股记录，4,461 条为个人→机构。全部 28,512 条均可回放到真实原始 FK 路径；后一类与个人持股对一一对应，但没有数值 ratio 或独立快照属性。因此应明确记录“部分正确方向主体捷径已经存在”，同时保留“尚无完整统一持股投影”的结论。详见 final-graph-audit.md（本地证据：`artifacts/finance-validation/final-graph-audit.md`）、final-graph-audit.json（本地证据：`artifacts/finance-validation/final-graph-audit.json`） 和 final-graph-business-id-map.json（本地证据：`artifacts/finance-validation/final-graph-business-id-map.json`）。

有界可视化采用 `smart` 模式和 `topology-types-communities-v1`，`deterministic=true`，节点限制 80、边限制 2,000，返回 80 节点/83 边；`nodes_truncated=true`、`edges_truncated=false`、`degree_scope=full_graph`。此处边未截断仅表示已选节点样本内没有再次触及边数量上限，不代表展示了全部 74,820 条边。响应的社区计数为 34、社区代表节点数 26、类型代表数 6、连接节点数 51；均为可视化选择信息，不能视为确认集团。证据：bounded-visualization.json（本地证据：`artifacts/finance-validation/bounded-visualization.json`）。

另外，实际观察到 **两次** `/api/sessions/.../info` 返回 HTTP 503 / `DATASET_AUTHORIZATION_UNAVAILABLE`，内部授权耗时为 4,003/4,005 ms，超过约 4,000 ms 的期限。其一响应保存在 005-session-info.json（本地证据：`artifacts/finance-validation/api-evidence/20260926-140854-39f11e/005-session-info.json`），代码链与运行计时见 runtime-findings.md（本地证据：`artifacts/finance-validation/runtime-findings.md`）。这是授权依赖未及时返回的可用性故障，不能解释为用户无权限；目前也没有证据将其限定为只有一次或已自行消失。项目图加载成功、可视化成功和容器健康分别有独立证据，它们不消除授权 503，也不能以 503 反推项目图从未加载。

### 9.3 三项需求的现场分析验收

| 用例 | 预期 | 内置工具结果 | 沙箱定制结果 | 最终判定及证据 |
|---|---|---|---|---|
| 000044 首大 | 000040，60%，与控制根区分。 | 已提出请求；进入全图工具后报错，未计算。 | 请求中的 A 项；完整脚本未执行，无结果。 | 两条在线路线均未验证；独立 oracle/citics 数值相符。 |
| 000044 控股链 | 000001=>000040=>000044，来源边可回放。 | 已提出请求；未输出业务链。 | 请求中的 A 项；完整脚本未执行，无结果。 | 两条在线路线均未验证；独立 oracle/citics 关键关系相符。 |
| 000001 集团 | 确认成员仅 000040/000044，保留传递证据。 | 已提出请求；未输出确认集团。 | 请求中的 D 项；完整脚本未执行，无结果。 | 两条在线路线均未验证；独立闭包已得到基线。 |
| 000013 并列 | 两位个人各 20%，均保留。 | 已提出请求；未执行该业务计算。 | 请求中的 B 项；完整脚本未执行，无结果。 | 两条在线路线均未验证；独立 oracle/citics 候选相符。 |
| 003784 的 50% | 不单凭 003710 的恰 50% 边认控制。 | 已提出请求；未执行该业务计算。 | 请求中的 C 项；完整脚本未执行，无结果。 | 两条在线路线均未验证；独立闭包/参考结果符合该边界。 |
| 000041 累计 | 关闭注销过滤时，累计选 692，不能被 Top-1 误导。 | 原生后续运算未开展，不能记作在线通过或数值失败。 | 未纳入本次 A—D 有效计算覆盖，也无后续计算。 | 已完成独立 oracle/citics 验证；在线结果未验证。 |
| SCC/冲突/注销 | 621/901 有复核和数据异常；72按策略中止。 | 原生后续运算未开展。 | 提示含 SCC/冲突检查要求，但没有有效执行；特殊样本未计算。 | 已完成数据/参考审计并定位缺口；在线结果未验证。 |

原生会话的实际序列为：用户提交验收请求→`get_graph_info` 完成→`get_constructed_graph` 完成→下一次模型供应商请求返回 HTTP 400。错误原文为 `String value length (28049408) exceeds the maximum allowed (28000000)`，属于返回全图之后的请求字符串超限，不是模型经过金融计算后给出错误答案。运行记录 `status=error`、`active_run=null`、`snapshot_complete=true`，助手仅留下开始验收的短提示，没有三项业务结论。

运行快照达到 **106,245,421 字节**，`tools` 与 `processItems` 各存一份全图结果；这与模型错误中的字符串长度是不同计量对象，不能把字节数和字符长度混写。浏览器当时仍显示旧的 running，而后端权威快照已为 error，属于本次观察到的**错误终态未及时正确恢复**；不能由旧卡片推断工具还在执行，也不能仅据此确定首次断流和恢复延迟的唯一原因。直接证据为 native-chat-result-summary.json（本地证据：`artifacts/finance-validation/native-chat-result-summary.json`）、model-error.log（本地证据：`artifacts/finance-validation/model-error.log`） 和 chat-run-snapshot.json（本地证据：`artifacts/finance-validation/chat-run-snapshot.json`）。

该失败发生在业务运算之前，说明需要优先限制模型可见工具输出、以文件/分页保留全量图证据、避免快照重复保存巨型对象，并保证 error/cancel/completed 终态与 UI 同步。独立 workspace 补测采用了避免全图工具的另一条路线，但也未成功，详见下节；不能以离线审计脚本的正确答案替代线上没有产生的结果。

### 9.4 独立 workspace 定制脚本补测

该会话为 `b61ab217-0fda-471b-bae3-e48ae7a183fc`，run 为 `61fdeb75-f449-4e5a-92b9-eaf0e493ffcb`。本轮附上构图使用的同三份 CSV，明确禁止再次调用超大 `get_constructed_graph`，要求读取 Uploaded，在 Derived 生成可复现脚本及有界 JSON，只计算 A—D：000044 的首大/经济根/控制链、000013 并列、003710→003784 的恰 50% 边界、000001 的控制集团。请求使用 `equity_only`，不适用 GP/法人/注销/特殊主体规则，仍要求报告规则缺失和数据覆盖边界。请求原文见 chat-request.json（本地证据：`artifacts/finance-validation/custom-analysis/chat-request.json`）。

实际保存 **14 个工具结果**，不是 14 次业务计算：

| 结果类别 | 数量 | 实际行为 |
|---|---:|---|
| `workspace_list` | 1 | 成功列出三个 Uploaded 附件。 |
| `workspace_bash` completed、exit 0 | 5 | 附件预览、文件名定位、行数查看及写入一个 14 字节 Ephemeral 测试文件。 |
| `workspace_bash` completed、exit 1 | 2 | 两次使用错误的个人 CSV 文件名，读取失败；completed 仅指命令进程结束。 |
| `workspace_bash` rejected | 6 | 完整脚本相关命令被工作区检查拒绝，没有执行。 |

三份 CSV 的预览和带表头行数已读取，说明 Uploaded 可访问；不能据此宣称全量业务投影、控制闭包、根累计或 SCC 已完成。唯一写入产物是隐藏的 `Ephemeral/equity_projection.py`，大小 **14 字节**，只是测试文件。Derived 中没有交付脚本或结果 JSON，A—D 均无可独立比较的在线结果。原始工具统计及文件列表见 result-summary.json（本地证据：`artifacts/finance-validation/custom-analysis/result-summary.json`）。

对 13 次 bash 命令逐调用重放工作区静态门禁发现：6 次拒绝中，**5 次是 Python 除号 token `/` 被当作工作区外绝对路径而触发门禁，另 1 次确实使用了不允许的 `/dev/stdin`**。命中定位来自当前源码的离线重放，服务器事件本身仅返回统一拒绝信息，二者不能混写。具体代码是 [workspace_tools.py:303](../../../agent_runtime/workspace_tools.py#L303) 的绝对路径检查，以及 [workspace_tools.py:307](../../../agent_runtime/workspace_tools.py#L307) 对含空白参数递归 shell 解析。因此不能把六次都称为误拒绝，也不能把正常的工作区边界限制当作应绕过的权限要求。

修复应区分脚本正文与 shell 路径，并提供与规则兼容的结构化写文件/执行方式。当前 Derived 是保护区，正确产物路线是写入 Ephemeral 后通过 `workspace_publish` 发布，不能让 Bash 直接写 Derived；另一合法诊断候选是正常上传已审阅 `.py` 后执行 Uploaded 的真实相对路径，但本轮并未实测该候选，不能将其记为成功补救。详见 custom-workspace-findings.md（本地证据：`artifacts/finance-validation/custom-workspace-findings.md`）。

为结束持续重复尝试，先调用公开取消接口，实际返回 **HTTP 409 / `RUN_CANCEL_UNAVAILABLE`**，证据为 cancel-response.json（本地证据：`artifacts/finance-validation/custom-analysis/cancel-response.json`）。随后本次验证操作**仅重启本隔离 Compose 实例的 agent-runtime 服务**；Compose 显示 Started，原 SSE 接收结束。操作说明见 operator-stop.json（本地证据：`artifacts/finance-validation/custom-analysis/operator-stop.json`）。没有停止原有其他实例或改写源数据。

receipt.json（本地证据：`artifacts/finance-validation/custom-analysis/receipt.json`） 的起止相差约 **590.194 秒**，这是本轮 SSE 接收窗口，不是成功金融运算耗时。重启后读取的快照仍为 `status=running`、`snapshot_complete=false`，这是与执行进程已被操作终止不一致的陈旧状态；**本次不能记作应用自然完成、成功取消或自动转为 error**。该结果同时暴露工作区门禁兼容性、重复拒绝后的有界停止、公开取消可用性和运行中断后的终态回收问题。

## 10. 改进方向与交付判定

正式改进方案已完成：[FINANCE_IMPROVEMENT_PLAN_2026-09-26.md](../plans/FINANCE_IMPROVEMENT_PLAN_2026-09-26.md)。方案第 0 节列出 P0 运行阻塞修复，其后包含三项专用工具/API 的请求与结果契约、质量及完整性状态、集团树/DAG 交互、M0—M4 交付阶段，以及 31 项金融业务自动化验收用例。方案中的修复、专用 API、界面和测试属于待实施设计，本次没有将其伪装成已上线能力。

现场已复现的工具输出超限、快照双份全图、授权超时和错误终态未及时正确恢复，应作为 **P0 运行可靠性修复**，在金融业务计算集成前或同时解决。否则，即便算法正确，真实数据工具返回仍会在进入计算前阻断会话。详细代码链、已确认事实与尚未确定的耗时归因见 runtime-findings.md（本地证据：`artifacts/finance-validation/runtime-findings.md`）；其修复验收覆盖大结果分页/外置、有限模型摘要、快照去重、轻量权威授权与状态读取、确定性 HTTP 400 不重复发送、终态恢复及用户可见失败原因。

定制补测进一步要求把工作区脚本门禁修复、结构化写文件/执行、有界拒绝恢复、公开取消的有效送达，以及 worker/runtime 中断后的运行租约与终态回收纳入 P0。须验证普通 Python 除法不会被误识别为绝对路径，同时真实 `/dev/stdin` 或工作区外访问仍按规则拒绝；命令 exit 非零不能计为分析成功；重复拒绝不能无限触发大脚本重试；运行被操作终止后应有明确中断状态而非长期陈旧 running。不得通过绕过权限或盲目放宽所有超时替代根本修复。

基于本报告已确认的证据，改造至少包含以下层次：

1. **持股投影与质量闸门**：三表合为统一主体注册表和有向持股边；归一化比例，保留来源、快照和状态；校验多态引用、重复、总股比和未知主体。
2. **规则与算法服务**：独立首大规则链、严格控制闭包、全量根权益累计与 SCC 方程；统一有效边策略和预算状态；修复参考原型的截断、成员完整性和数据质量问题。
3. **可审计结果契约**：首大、控制、权益、路径、集团分别返回；绑定图版本、快照、算法/规则版本、完整性、质量异常和来源边。
4. **前端集团视图**：明确树类型；显示中间/最终根、控制贡献、多父/交叉关系和 SCC 复核；将关联候选置于独立视图。
5. **自动验收与运维**：保留本次真实样本及最小边界例；构图和算法任务可恢复、可定位；缓存/增量与规模测试围绕真实查询负载设计。

要宣称三项需求已经完成，至少需要同时满足：业务图映射正确；所有适用优先规则及缺数据状态可解释；上述独立样本通过；环、冲突、并列和预算结果正确；来源链可回放；用户能从部署实例完成上传、构图、分析与结果查看。原型能输出一个候选 ID 或一张图，不满足这一交付标准。

## 11. 证据索引与复现入口

| 产物 | 内容 |
|---|---|
| requirements-and-data.md（本地证据：`artifacts/finance-validation/requirements-and-data.md`） | PPT 按页摘要、字段映射、数据质量、验收样本。 |
| ppt-requirements-extracted.md（本地证据：`artifacts/finance-validation/ppt-requirements-extracted.md`） | 17 页 PPT 原始文字提取。 |
| source-data-profile.json（本地证据：`artifacts/finance-validation/source-data-profile.json`） | 全字段缺失统计、哈希、ID/类型一致性、全部环和超限目标。 |
| independent-oracles.json（本地证据：`artifacts/finance-validation/independent-oracles.json`） | 独立十进制完整路径、源行号、累计根、控制闭包成员。 |
| profile_source_data.py（本地证据：`artifacts/finance-validation/profile_source_data.py`） | 只读源数据分析与独立基线，可重复运行。 |
| capability-audit.md（本地证据：`artifacts/finance-validation/capability-audit.md`） | iGraph 代码定位、公开 API、能力边界。 |
| raw-csv-schema.json（本地证据：`artifacts/finance-validation/raw-csv-schema.json`） | 三表精确 schema 及 PK/FK。 |
| initial-graph-oracle.json（本地证据：`artifacts/finance-validation/initial-graph-oracle.json`） | 当前构图库全量初始结果。 |
| micro_graph_semantics.py（本地证据：`artifacts/finance-validation/micro_graph_semantics.py`） | 初始构图方向/权重的最小复现。 |
| citics-audit.md（本地证据：`artifacts/finance-validation/citics-audit.md`） | 参考实现复用点与已复现风险。 |
| citics-reference-results.json（本地证据：`artifacts/finance-validation/citics-reference-results.json`） | 参考算法真实目标与风险复现原始输出。 |
| citics_reference_audit.py（本地证据：`artifacts/finance-validation/citics_reference_audit.py`） | 参考引擎只读审计脚本。 |
| verify_instance.py（本地证据：`artifacts/finance-validation/verify_instance.py`） | 本次实例 API/会话检查辅助入口；是否执行某步骤以实际日志为准。 |
| final-graph-audit.md（本地证据：`artifacts/finance-validation/final-graph-audit.md`） | 服务器最终图全记录、全字段、全边和派生结构审计。 |
| bounded-visualization.json（本地证据：`artifacts/finance-validation/bounded-visualization.json`） | 成功返回的 80 节点/83 边有界可视化。 |
| native-chat-result-summary.json（本地证据：`artifacts/finance-validation/native-chat-result-summary.json`） | 原生会话 error 终态、调用次数和超大快照摘要。 |
| model-error.log（本地证据：`artifacts/finance-validation/model-error.log`） | 模型供应商请求字符串超限的实际 HTTP 400。 |
| runtime-findings.md（本地证据：`artifacts/finance-validation/runtime-findings.md`） | 超大工具、恢复快照、授权超时与终态恢复的代码链及 P0 方案。 |
| health-final.txt（本地证据：`artifacts/finance-validation/ops/health-final.txt`） | 最新服务器 Compose 服务检查，独立于业务运行是否成功。 |
| tunnel-final.json（本地证据：`artifacts/finance-validation/ops/tunnel-final.json`） | 重启后本地 SSH 隧道入口 HTTP 200 检查。 |
| [FINANCE_IMPROVEMENT_PLAN_2026-09-26.md](../plans/FINANCE_IMPROVEMENT_PLAN_2026-09-26.md) | 正式 API、算法、UI、里程碑和 31 项验收设计。 |
| custom-analysis/result-summary.json（本地证据：`artifacts/finance-validation/custom-analysis/result-summary.json`） | workspace 14 个工具结果及文件统计；没有业务结果产物。 |
| custom-workspace-findings.md（本地证据：`artifacts/finance-validation/custom-workspace-findings.md`） | 工作区静态门禁逐命令复核、拒绝原因和 P0 修复建议。 |
| custom-analysis/cancel-response.json（本地证据：`artifacts/finance-validation/custom-analysis/cancel-response.json`） | 公开取消返回 `RUN_CANCEL_UNAVAILABLE`。 |
| custom-analysis/operator-stop.json（本地证据：`artifacts/finance-validation/custom-analysis/operator-stop.json`） | 为结束重复尝试而重启隔离实例 agent-runtime 的操作说明。 |
| custom-analysis/receipt.json（本地证据：`artifacts/finance-validation/custom-analysis/receipt.json`） | 定制补测 SSE 接收起止时间，约 590.194 秒。 |

标准库数据基线可在项目目录运行：

```powershell
python artifacts/finance-validation/profile_source_data.py
```

参考算法核验：

```powershell
python -B artifacts/finance-validation/citics_reference_audit.py
```

本次没有修改源 CSV、参考算法或 iGraph 应用源码，也没有创建 Git 提交。服务器已按本次代码基线重建隔离 Compose 实例，6 个服务 healthy、worker running；用户可通过 SSH 隧道访问 `http://127.0.0.1:22173`，使用 `admin` 查看项目 `df6df4e2-1fbd-4443-abac-dae3088a6c61` 及其已构图数据集。本地 Docker daemon 未运行，因此没有本地 Compose 重建结果；本次实际验证入口按用户要求部署在服务器。

本次原生与独立 custom 在线验收均已停止取证，均未得到三项业务结果；失败位置和边界已分别记录。独立数据基线、citics 对照、服务器部署、浏览器上传构图和最终图保真审计已完成，足以给出当前能力缺口及可执行改进方案。金融专用能力和 P0 运行问题的代码实现不在本次已完成修改之列，应按正式方案另行实施、重建受影响服务并复验；不得将尚未实现的方案或离线正确结果标为线上已通过。
