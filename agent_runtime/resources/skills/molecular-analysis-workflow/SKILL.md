---
name: molecular-analysis-workflow
description: 分析已加载分子图的溶解性与毒性证据、环结构、芳香 C–H 硼化候选和对称等价位点。当用户询问这些性质或要求在分子图标注候选位点时使用；区分结构描述符、实测结果和待验证假设。
---

# 分子结构到位点分析

## 适用范围与证据

先核对当前活动图与后台核验的 graph_background。BO-01 配套案例使用 `borylation_case_analysis`，从当前图读取；其它分子不得套用案例数字或描述符。切图后停止沿用旧证据。按当前页面语言回答，先一句结论，随后依据与解释，最后总结。

调用对应步骤并读取成功结果；结构读取失败时不能用历史内容补造答案。预存描述符由 RDKit 离线计算，部署无需安装 RDKit；结构指纹必须匹配当前图。

```json
{"tool":"borylation_case_analysis","arguments":{"step":"properties"}}
```

```json
{"tool":"borylation_case_analysis","arguments":{"step":"rings"}}
```

```json
{"tool":"borylation_case_analysis","arguments":{"step":"structure"}}
```

```json
{"tool":"borylation_case_analysis","arguments":{"step":"symmetry"}}
```

## 1. 溶解性与毒性

分层陈述：原始测定 → 指定模型的预测 → 基于结构的定性假设 → 当前未知。
描述符给出方法、单位和范围。分子量、氢键供受体和 TPSA 是结构描述；cLogP 是计算的辛醇/水分配性质，不是任意有机溶剂中的 logS，也不是实测溶解度。TPSA 是否包括 S/P 以工具方法为准。

溶解度涉及指定溶质、溶剂、温度、浓度、物态与平衡条件；晶格和固态因素不能从二维图完整获知。可依据芳香骨架、极性官能团和氢键提出具体的溶剂筛选假设，例如考虑 THF 等醚类或 DMF/DMSO/NMP 等极性非质子溶剂（后三者不能归为低极性溶剂），并解释理由；必须标注待实测，不能无依据排序为“易溶/不溶”或编造 g/L。反应记录出现某溶剂、名义投料浓度、转化率均不证明完全溶解；反应失败也不证明不溶。

毒性是有终点、剂量、暴露途径和试验体系的结论。没有实测或经过适用域验证的模型结果时，回答当前证据不能判断有毒/无毒。芳香环、卤素、杂环、cLogP 或药物样性不构成该化合物毒性实证。不能把“有卤素/芳香环”本身列为已经确认的毒性结构警示，未提供具体警示规则、终点与适用域时不强行列举警示。更不能说“酰胺键在通常剂量下毒性有限”或某官能团低毒：剂量与毒性属于完整化合物及指定试验终点，不属于一个键。结构警示只能是待验证线索；不得声称运行了工具未运行的 QSAR，也不能声称全世界没有该分子的资料。可建议核对明确身份对应的文献/SDS和适用终点数据，不编造化合物名称或 CAS。

## 2. 环数量、类别和性质

使用化学键而不是双向存储边计环，不能把一条无向键的两个方向当成二元环。区分环数、环类型数、独立环基和稠环体系；复杂稠合/桥环需说明计数口径。按大小、元素组成、芳香性分组是本案例的分类规则，同类环的取代环境可以不同。

逐环列显示编号、大小、元素组成、芳香性。环编号不是原子编号。芳香性来自工具化学感知和原子属性，不能仅凭画成多边形或交替键判断。含 S/N 的杂环不能说成全碳苯环或三唑。可解释芳香共轭、杂原子改变电子分布、取代基影响局部环境；未经计算/实测的 pKa、定量活性排序和反应机理需保持为假设。

## 3. 硼化候选与真实标注

候选条件为芳香 C 且带 H，不包含已完全取代的芳香 C、N–H 或其它元素。候选是结构筛选，不能直接断言均可制备或等概率反应。邻位、间位、对位以及取代基邻接必须依据真实化学键（bonded_neighbors / adjacent_ring_substituents），不能按原子编号大小或图形位置推断。二维拓扑距离不等于三维空间位阻；未做相应计算或实验，不对具体位点强行排序“更拥挤/更易反应”。空间位阻、电子和催化体系可作为待验证影响因素讨论，已报道位点与模型预测分开。

用户要求在图上标出时，必须调用 `highlight_nodes` 或 `visualize_subgraph`，将工具返回的 atom_index 映射到当前真实节点 ID，使用 display_number 向用户解释。先读取工具参数规范，不猜字段。不用文本表格代替实际高亮；成功回执后才说已标注。工作台仅展示当前分子结构，不切换到实验记录关系图。

## 4. 对称性与不等价位置

按完整带标记分子图的 symmetry_class 合并候选；考虑元素、键、全部取代环境及已编码的立体/同位素信息，而不是按二维画图距离或局部环镜像猜等价。RDKit CanonicalRankAtoms 的 breakTies=False 返回对称类；内部类 ID 不是显示原子编号。逐组列 display_numbers 并选一代表，核对组的并集恰好覆盖候选且无重复。

取代基不同不自动破坏所有对称性；不同环的外观相似也不说明等价。拓扑等价不能推出在所有手性环境/动态构象中都完全相同，若图未编码有关信息需说明边界。区分一个具体位点与一个等价类别。

## 延伸实验数据

需要时继续用 products、conditions、plan 步骤。产物表与条件表仅按底物关联，不能指定条件对应某产物；转化率不是位点选择性或分离收率，低于阈值不等于零转化。

## 方法参考

- [RDKit 分子描述符](https://www.rdkit.org/docs/GettingStartedInPython.html) 与 [化学感知及 TPSA](https://www.rdkit.org/docs/RDKit_Book.html)
- [RDKit CanonicalRankAtoms](https://www.rdkit.org/docs/source/rdkit.Chem.rdmolfiles.html)
- [IUPAC 溶解度定义](https://old.goldbook.iupac.org/html/S/S05740.html)
- [OECD QSAR 评估与适用域](https://www.oecd.org/en/topics/sub-issues/assessment-of-chemicals/quantitative-structure-activity-relationships-project.html)
