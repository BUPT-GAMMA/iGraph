---
name: finance-ownership-workflow
description: 基于当前会话股权数据查询第一大股东、控股链、最终控制根、集团树和经济权益穿透，先检查快照与主体再执行专用金融工具。适用于企业持股和控制关系分析，不用于证券价格预测、普通社交网络社区或一般最短路。
metadata:
  version: 1.0.0
  domain: finance_ownership
  risk_level: low
  igraph:
    requires_tools: [finance_inspect_ownership, finance_first_holder, finance_control_chain, finance_group_tree, finance_economic_interest]
    requires_graph_state: loaded
    activates_on_intents: [first_holder, ownership_control, group_tree, economic_interest]
---

# 股权分析工作流

从当前会话已加载的属性图生成股权业务投影，在服务端计算；这里只解释工具返回的事实、候选、状态和证据，不自编算法或补造规则。

## 先检查数据和主体

1. 读取本技能后先调用 `finance_inspect_ownership`。已知目标时可同时传 `query`，否则先用空参数检查快照。该工具直接读取当前会话图，不传入自己猜测的 graph_id、session_id 或文件路径。
2. 若工具明确说明当前没有可用图，按数据集状态流程加载用户指定且已 ready 的版本，再重新检查。不为金融查询重新构图，不调用 `get_constructed_graph` 拉回完整图，也不以 workspace Python、普通可达性或社区工具替代金融计算。
3. 从返回的质量报告核对输入是否可投影、快照、主体/持股数量、比例格式、重复及股份总和异常。构图 ready 不代表金融数据已通过质检；算法 complete 也不等于业务规则齐备。质量问题由专用工具决定对当前目标的影响，不忽略冲突，也不因无关目标的异常擅自推翻所有结果。
4. 用 `query` 搜索用户给出的业务 ID 或名称，读取 `result.entities` 的 `entity_id`、`business_id`、`name` 和来源标识。后续优先使用返回的 `entity_id`。名称仅用于检索，不能据同名合并个人或认定法人身份。匹配多个主体或返回 `AMBIGUOUS_ID` 时先缩小查询；仍不明确则请用户选定主体，不自行挑第一条。

以下调用中的中文字符串是参数位置说明，执行时替换为用户原查询或工具已经返回的主体 ID；不是测试样例或预设答案。

```json
{"tool":"finance_inspect_ownership","arguments":{"query":"用户提供的主体ID或名称","display_limit":20}}
```

## 按问题选择专用计算

使用同一快照和同一 `policy`。检索得到 ID 后，只执行用户问题需要的分支；独立分支可以一批调用，依赖前一结果的分支要等待真实返回。

每个独立 `policy` 分支都先调用 `finance_inspect_ownership` 并传入与该分支计算相同的 `policy`；切换或新增停止、排除策略时必须重新检查。默认策略的检查或普通身份检索不能替代该分支的策略检查。

| 问题 | 工具与解释 |
|---|---|
| 第一大股东、首大候选 | `finance_first_holder`。保留所有并列候选，区分直接股东锚点和累计经济根；不能把首大候选改称最终控制人。 |
| 谁控制这家公司、控股链、最终控制根 | `finance_control_chain`。根据工具的严格控制结果解释中间主体、最终根和贡献链；经济权益路径乘积不替代表决权闭包。 |
| 指定根的控股集团、集团树 | `finance_group_tree`。`root_id` 为用户指定并已解析的根。若用户只给成员公司而要其所属控制集团，先运行 `finance_control_chain`，再对其返回的股权口径根调用集团工具，并保留质量与规则限制；没有唯一根就呈现未决/多候选，不能把首大或社区中心当作根。 |
| 股权穿透比例、间接经济权益、经济根排名 | `finance_economic_interest`。未指定出资主体时解释最终经济根排名；用户指定某个上游主体时传可选 `root_id`，读取 `requested_interest`。该主体可以是中间股东或控制根，不要求是输入图边界根。遇 SCC 采用工具方程求解状态，不能手工删环或将展示的简单路径相加代替完整累计。 |

```json
{"tool":"finance_first_holder","arguments":{"target_id":"已解析的目标entity_id","evidence_top_k":5}}
```

```json
{"tool":"finance_control_chain","arguments":{"target_id":"已解析的目标entity_id","evidence_top_k":5}}
```

```json
{"tool":"finance_group_tree","arguments":{"root_id":"用户指定或工具确认的根entity_id","display_limit":100,"evidence_top_k":5}}
```

```json
{"tool":"finance_economic_interest","arguments":{"target_id":"已解析的目标entity_id","display_limit":100,"evidence_top_k":5}}
```

用户询问“控制根对目标的经济穿透”时，先取得 `finance_control_chain` 返回的对应控制根，再将该根原样作为经济工具的 `root_id`；`equity_controller_id` 只表示当前股权口径的根，仍保留 `rule_status` 的业务规则缺失说明。多个根分别查询或明确列为候选，不自行挑选或相加。用户直接指定上游主体时，用检查工具解析该主体后传入，不必先把它认定为控制人。

```json
{"tool":"finance_economic_interest","arguments":{"target_id":"已解析的目标entity_id","root_id":"用户指定或控制工具返回的上游主体ID","display_limit":100,"evidence_top_k":5}}
```

控制链衔接集团树或经济权益时，优先传 `equity_controller_entity_id` 或 `ultimate_controller_entity_ids` 中带 `I:`/`P:` 的主体标识。集团树边按 `source_entity_id` 和 `target_entity_id` 对齐成员，控制证明按 `controller_entity_id` 和 `controlled_entity_id` 对齐；不将机构与个人中相同的原始业务 ID 合并，也不据此误画自环。原始业务 ID 仍用于展示。

分别解释三种数值：`requested_interest` 是指定主体对目标的完整累计经济权益；展示路径的 `product` 是该条路径的乘积；最终经济根 ranking 是按输入图边界根累计后的排名。控制链的单链乘积或有限 Top-K 路径之和都不能代替 `requested_interest`，控制根也不必出现在最终经济根排名中。

解释指定主体的路径贡献前，核对 `evidence.path_scope="requested_upstream"`、`requested_root_entity_id` 及路径起终点均对应本次 `root_id → target_id`；未指定主体的 `boundary_roots` 是另一范围。范围缺失或不一致时只报告工具返回的总值，明确该路径不能支持其分解。`root_interests` 及其他经济根的路径不能混入该主体贡献、相加或与 `requested_interest` 求差。展示路径只是有界证据，不自行用总值减 Top-K 合计推算“其余路径贡献”；有 SCC 时，简单路径展示尤其不能与包含循环游走的累计总值求剩余。工具若直接提供同一范围、同一计算口径的贡献分解，才可引用该分解。

集团的主视图只使用工具返回的 CONTROL 成员和关系，确认范围仍受质量与规则状态约束。优先依据 `finance_group_tree` 的 `tree_edges` 和 `additional_edges`，用层级文本、表或 Mermaid 展示；节点业务名称和 ID 必须取自返回结果。树边是展示结构，控制判定仍依据 `control_dag`，不能将每条展示边改称独立确认控制。

默认 `equity_only` 下称“股权口径成员/集团层级”；`primary_faction_id` 为空时，不能宣称已经确认完整业务集团。CONTROL 计算得到成员也不补足缺失的 GP、法人或特殊主体规则。

PATH 是经济证据，AFFINITY 是关联候选，不能加入已确认 primary。工具提供的跨分支贡献、多父关系或 SCC 不能为画成单树而删除。这个金融集团展示流程优先于通用路径展示规则，无需额外调用普通 `visualize_subgraph` 替代专用结果；不要把原始记录关系图的可视化或通用生成树称为金融集团。普通非股权路径分析继续使用其原有可视化流程。

## 规则、完整性和有界返回

- 默认 `policy` 是 `equity_only`，只使用输入股权事实；返回 `RULE_DATA_MISSING` 时明确 GP/法人及特殊主体规则未齐备。即使计算完整，也只能按这个范围报告候选。不得靠姓名补出 GP/法人关系，或自行填入 rule_relations / rule_coverage 制造确认。
- 注销停止默认没有启用。用户明确要求在注销主体中止时，为检查及相关计算一致传 `policy: {"stop_statuses":["注销"]}`；其它停止/排除名单只用用户提供或数据中已验证的 ID。没有指令时保留默认并在结果说明采用的政策，不默默切换过滤策略。`min_edge_ratio` 不为降低工作量而自行提高。
- 控制采用严格 `>0.5`；恰 `0.5` 只说明该边本身不足以确认控制，不能据此断言整个目标不存在控制人。控制贡献按受控股东计，不能把多条到同一股东的路径重复累计为表决权。
- 每个目标先读取顶层 `status`、`quality` 对该目标的影响及 `review_reasons`，以此决定能否给确定结论；`analysis_status`、`control_status` 保留的算法诊断不能越过该门槛。`DATA_CONFLICT` 下的 `NO_SINGLE_CONTROLLER` 只能报告“当前算式未识别单一控制人，但数据冲突使结论无法确认”，不能改写成“确认没有严格控制人”。上游异常会影响目标，目标直接持股合计正常或某条边恰为一半，均不能推翻工具已报告的上游冲突。
- `DATA_CONFLICT` 不得转成已确认首大/控制/primary；首大返回的候选 ID 仍可能只是诊断候选。保留同时返回的全部复核原因，`SCC_REVIEW` 不代表一个环已经合并成控制人；`NON_CONVERGENT` 不得编造精确权益值。
- `evidence_top_k` 只影响展示证据，`display_limit` 只影响返回列表；它们不应改变完整计算中的候选排名。默认先用少量证据，缺少展示细节时才调整展示参数，不从有限展示反推出“只有这些股东/成员”。
- `max_nodes`、`max_edges`、`max_seconds` 是计算预算。预算截断、未算全、数据覆盖不足、规则未齐和纯展示分页分别解释；不可只因工具 HTTP 成功或有候选值就称完整。不要通过删除小股比边、增加任意阈值或用 Top-K 路径代替完整累计来消除截断。
- 不支持的输入、规则或资源规模，按工具返回说明缺失条件和下一步。确定性失败不重复提交同一请求，不切换到临时 Python 猜算“成功结果”。

## 回答和证据

先回答用户实际问的首大、控制链、集团成员或经济权益，每个目标的结论同时说明其状态与受影响范围，不能只在末尾附上一条通用风险提示。再给快照日期、使用的规则范围、关键比例/贡献和来源边证据。把确认结果、并列/候选、质量冲突和不完整状态分开；经济权益不自动等于最终受益人。比例展示可转百分数，但不得修改工具用于阈值和排名的精度。

最终报告按所执行的分支保留以下字段，并在发送前逐项核对工具返回：

- 每个主体先列“顶层状态：`status`”，另列“算法诊断：`analysis_status` / `control_status`”；不要将算法诊断标成总体状态。诊断或字段未返回时明确缺失，不自行补全。
- 首大说明分别列“直接持股合计：`direct_ownership_total`”和“单一主体严格过半数量：`direct_strict_majority_holder_count`”。`threshold_semantics="per_single_holder"` 的过半判断针对单一主体；所有股东合计超过一半不表示存在过半股东，也不能把“没有单一主体过半”写成“直接持股合计未过半”。
- 集团规模分别列 `node_count` 及 `node_count_includes_root`、不含根的 `controlled_member_count`，再分别列 `tree_edge_count`、`additional_edge_count`、`control_dag_edge_count`。按 `count_scope` 说明计数范围，不用有界展示列表长度代替完整计算计数，不把不同边集合的计数混称为同一项，不自行将树边与附加边合并改称控制 DAG。`primary_faction_id` 为空时始终使用“股权口径成员/集团层级”的限定。
- 环路结论分别说明经济数值与控制判定：经济方程收敛只支持该口径的数值计算，不能解除 `SCC_REVIEW` 或控制候选的确认门槛，不能据此声称环路不影响控制人判定。
- 数据修正建议须核实权威原始记录后更正。持股合计超过百分之百时，不得按该合计归一化或同比缩放，以此消除异常或制造可确认结果。
- 统计说明区分快照全图统计与当前目标的组件。策略前后 SCC 等数量变化不能证明某个目标或关系导致该变化；没有目标组件及来源边证据时只报告各自统计范围，不猜测因果或关联。

优先引用返回的业务 ID、名称、直接比例、可支配表决权或经济权益及有界来源链。不要打印完整输入图、内部会话元数据或服务器文件路径。证据没有返回的部分应明确缺失，不能用常识或公司名称补全。
