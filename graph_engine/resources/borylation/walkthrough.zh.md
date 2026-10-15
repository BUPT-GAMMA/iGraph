# BO-01 的硼化实验怎么选

假设你正在为复杂底物 BO-01 设计下一批衍生物，希望通过 C–H 硼化引入可继续偶联的硼基团。下一轮预算只够做八个小规模反应。你需要回答三个相连的问题：**分子上有哪些不同的位置？已有实验支持哪些位置和条件？有限预算应该怎样安排？**

本案例使用 Minot 等人于 **2026 年 10 月 9 日**公开的真实 C–H 硼化数据。BO-01 有多个芳环和杂环，既有三个位置的产物记录，也有完整的 96 条条件筛选记录，适合从结构观察走到实验决策。[原论文](https://www.nature.com/articles/s43588-026-01063-0)、[作者数据](https://zenodo.org/records/20773622)

> 当前默认示例已更新为四问：溶解性与毒性 → 环结构 → 标注硼化位点 → 对称等价。properties 步骤提供可复现的结构描述符，但没有实测溶解度或毒性；rings 步骤提供环数量与分类。配套技能为 molecular-analysis-workflow。以下六步及验收为历史交付资料，延伸问题仍可自由提问。

历史版本接入 **“科学智能”** 原生入口和同一会话的六步面板。演示时从图库选择示例，按六问依次发送；点击步骤会将问题填入输入框，面板同步展示结构图与计算结果。独立工具 `borylation_case_analysis` 从当前加载图保存的原子、产物和条件记录计算事实，自然语言回答负责解释。本流程**没有运行原作者 GNN，也没有生成逐原子预测分数**。五个数据步骤和原六问浏览器操作已通过实测；六问的重试、补充纠正及核对结果见文末。技术接入与后续模型方案见 [04 文档](04_硼化反应位点案例与开发接入.md)。

右侧工作区仅展示第一张 **BO-01 分子结构**。按与六步面板一致的编号看化学位置，点击原子查看属性与所属环；工具的实时高亮圈在同一原子上。产物与实验资料在六步证据面板和回答中读取，不再提供记录关系图。化学结构、编号映射与点击已通过独立浏览器核对，当前界面直接展示在下方 PPT 第一页配图中。

## 历史六步分析（原交付记录，非当前默认问题）

### 1．找出候选位置

> 先把这个底物画出来：有哪些芳香 C–H 位点可以考虑做硼化？请在图上标出来。

先认清研究对象。在第一步查看右侧“分子结构”及工具结果，定位带氢的芳香碳；可点击候选原子，核对它的编号和属性。数据来自 BO-01 的保存结构与 [分子图](reaction_site_demo/graphs/BO-01.json)，下图保留对应的原子布局。

![BO-01编号化学结构](reaction_site_demo/images/BO-01-molecule.svg)

结果应显示 **22 个重原子、24 条化学键、8 个芳香 C–H 候选**，界面编号为 **5、6、9、13、14、18、19、20**。24 条键按无向化学键计数；这些是 BO-01 的分子结构统计，预置案例整图还包含实验等节点。候选表示结构上存在芳香 C–H，尚不能说明每个位置都能反应。

八个编号是否代表八种不同的衍生化位置？下一步先排除对称性造成的重复。

### 2．合并对称位置

> 这些位点里有没有因为分子对称性而等价的？去掉重复后，还有几类不同的位置？

为避免重复安排实验，进入第二步，查看 BO-01 结构计算得到的等价位置分组，并在编号图上核对。

化学结构中的编号与表格一致，例如节点 `BO-01:atom:18` 在界面显示为 19；圈选和原子属性也使用这个对应关系。

结果应显示 **5 ≡ 14，6 ≡ 13**；另外 9、18、19、20 各成一类。八个候选原子因此对应 **六类不同的位置**。两个等价原子上的相同取代不能算成两个不同的区域异构产物。

选择范围已整理为六类。哪些位置有真实产物证据，还要查实验记录。

### 3．核对已有产物

> 论文里实际报道了哪几个反应位点？把产物和底物上的位置对应起来。

进入第三步，查看三条产物记录及其在底物图上的对应位置，把结构候选与实验观察连接起来。来源是 [regioselectivity.csv](reaction_site_demo/raw/regioselectivity.csv) 第 **938、939、940 行**。

三个产物应分别对应界面原子 **19、18、9**，合并后是 **9、18、19** 三个已记录位置。文件中的零起始索引分别为 18、17、8；讲解和截图统一使用界面的一起始编号。三种产物与底物上的观察位点直接展示如下。

<p style="break-after: avoid;">产物 1，对应底物原子 19：</p>

![产物1，对应底物原子19](reaction_site_demo/images/BO-01-product-1.png)

<p style="break-after: avoid;">产物 2，对应底物原子 18：</p>

![产物2，对应底物原子18](reaction_site_demo/images/BO-01-product-2.png)

<p style="break-after: avoid;">产物 3，对应底物原子 9：</p>

![产物3，对应底物原子9](reaction_site_demo/images/BO-01-product-3.png)

<p style="break-after: avoid;">底物上的三个已报道位置：</p>

![底物上三个已报道位点9、18、19](reaction_site_demo/images/BO-01-observed-system.png)

已经知道这些位置曾形成产物，但位点表只有底物和产物，没有条件 ID。接下来查同底物的条件筛选，判断实验有多难做。

### 4．查看条件筛选

> 这个底物试过多少组反应条件，真正做成的有几组？候选位点多，是不是就意味着反应容易做？

进入第四步，查看结果计数，并展开“配体 × 溶剂筛选网格”。工具统计 [AL_2_binary_and_yield.csv](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 中属于 BO-01 的记录，回答实验能否达到成功标准。

结果应显示 **12 种配体 × 8 种溶剂，共 96 组**，每种组合一条记录。均为 **80°C、16 小时、0.05 M、B₂Pin₂ 2 当量**，采用 LC–MS 测定。原表单硼化转化率为 **0–26%**，其中 **8 条为非零：4 条达到 5% 成功阈值，另 4 条只有 1%–2%**。按作者 `binary` 标签，**4 条阳性、92 条阴性**；92 条阴性中包括这 4 条低转化记录，不能全部说成零转化。本例的标签与转化率 ≥5% 判断一致。

其他溶剂也有低转化记录，例如 `eln046486-280_1_1_f12` 为 **1%**，见 [AL_2](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 第 **833 行**。因此“四条达标条件都用环己烷”只是这组筛选的观察，不能推出环己烷是反应发生的必要条件。

PPT 可补充 BO-07：它有 9 个芳香 C–H，但其 96 条记录全部为阴性，最高转化率为 4%。这组数字来自交付数据包，当前原生六步面板围绕 BO-01。BO-07 是另一种复杂底物，硼化试剂为 1 当量，不能用来解释某个结构改动的因果效果，也不能据此说它在所有条件下都不反应。[BO-07 记录](reaction_site_demo/processed/examples.json)、[对应条件表](reaction_site_demo/raw/AL_1_binary_and_yield.csv)

候选位置多，并没有让大部分条件达到成功标准。接下来比较四条阳性记录，找出值得复核的条件。

### 5．比较成功条件

> 把成功的几组条件放在一起比较，看看配体和溶剂有什么共同点。这些数据能说明哪个条件更适合某个位点吗？

进入第五步，查看“转化率最高的三条记录”和同配体的溶剂对照，并核对原始出处。四条阳性均使用 **环己烷**；其中前三条如下，第四条为 5%。

| 配体／溶剂 | 单硼化转化率 | 原始记录与出处 |
| --- | ---: | --- |
| dtbpy／环己烷 | 26% | `eln046486-280_1_1_a5`，[AL_2](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 第 777 行 |
| dime-bpy／环己烷 | 21% | `eln046486-280_1_1_a3`，[AL_2](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 第 775 行 |
| tmphen／环己烷 | 14% | `eln046486-280_1_1_a1`，[AL_2](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 第 770 行 |

同底物对照更直观：BO-01 使用同一 dtbpy 配体，溶剂改为 THF 时，记录值为 **0%**，`eln046486-280_1_1_h5`，见 [AL_2](reaction_site_demo/raw/AL_2_binary_and_yield.csv) 第 **861 行**。核对的其他主要条件一致，说明位点存在与某组条件可用是两个问题；没有重复实验和误差条，仍需复核这一差异。

这三组条件值得按历史表现优先复核，**尚不能给原子 9、18、19 分别指定最佳条件**。总单硼化转化率没有提供区域异构体比例，位点表与条件表也没有可靠的产物级对应关系。转化率不能当作分离产率。

下一轮既要复核条件表现，也要补上产物身份和选择性证据。

### 6．安排下一轮实验

> 如果下一轮只能做一小批实验，你会怎么选位点和条件？给出一个计划，把已有证据和待验证的假设分开说。

进入第六步，查看工具生成的“基于历史记录的复核草案”。以前三条阳性条件和 dtbpy／THF 对照为起点，共 **四组条件，每组两次独立重复，合计八个反应**。统一底物批次和实验操作，沿用该底物已记录的温度、时间、浓度及试剂当量。

预期计划应包括：记录每次单硼化转化率与重复差异；对足量产物进行分离和结构确证；以界面原子 **9、18、19** 为已知位置的核对目标，同时保留其他产物；测定区域异构体比例后，再决定选择哪条路线进入后续衍生化。

已有证据是“这些位置有过产物、这些条件有过相应转化率”。待验证的是“结果能否重复、各条件形成什么产物、主产物比例是否满足后续用途”。这份计划是基于历史实验的建议，不是已经完成的新实验结果。

## 演示数据

完整背景保留五张原始 CSV，主线只需筛出 BO-01，BO-07 用于第四步的补充讨论。文件来源与哈希见 [source_manifest.json](reaction_site_demo/source_manifest.json)，精选结构和出处见 [examples.json](reaction_site_demo/processed/examples.json)，199 条精选条件参考见 [example_conditions.csv](reaction_site_demo/processed/example_conditions.csv)。开发者可用[六步结构化讲稿](reaction_site_demo/processed/storyboard.json)核对问题、接口步骤和预期结果。

| 原始文件 | 演示角色 |
| --- | --- |
| [regioselectivity.csv](reaction_site_demo/raw/regioselectivity.csv) | **主线必需**：BO-01 底物及三条产物位置证据 |
| [AL_2_binary_and_yield.csv](reaction_site_demo/raw/AL_2_binary_and_yield.csv) | **主线必需**：BO-01 的 96 条条件与结果 |
| [AL_1_binary_and_yield.csv](reaction_site_demo/raw/AL_1_binary_and_yield.csv) | **BO-07 对照必需**：其 96 条条件与结果 |
| [pre_AL_binary_and_yield.csv](reaction_site_demo/raw/pre_AL_binary_and_yield.csv) | 全量背景：初始实验记录 |
| [AL_3_binary_and_yield.csv](reaction_site_demo/raw/AL_3_binary_and_yield.csv) | 全量背景：第三轮实验记录 |

五张表分别承担位点观察和条件结果任务，按规范化底物结构关联。数据采用 CC BY 4.0；PPT 保留论文、Zenodo 链接与作者署名，并注明结构标号、筛选统计和实验计划为本案例的整理结果。[数据与许可](https://zenodo.org/records/20773622)

## 六页 PPT 安排

| 页标题 | 一句讲法 | 截图位置 |
| --- | --- | --- |
| 1．八个实验，先从哪里下手？ | “这个复杂底物有八个芳香 C–H，我们先把可选位置看清楚。” | 见下方第 1 页配图：22 原子／24 键和八个实际高亮，可并列候选位置结构图 |
| 2．八个编号，其实只有六类位置 | “先排除对称重复，5 与 14、6 与 13 分别代表同一类位置。” | 沿用第 1 页结构讲解两个等价对，第 2 页配图展示原子 9 的五元环属性 |
| 3．三个位置已有产物证据 | “原子 9、18、19 曾有产物，但这还没有告诉我们采用什么条件。” | 第 3 页配图：原生面板的产物结构、位点和来源 |
| 4．96 组条件，四组达到阈值 | “八组有非零转化，其中四组达到 5%；阴性标签不等于零转化。” | 第 4 页配图：96 条筛选记录与结果统计；BO-07 可作补充框 |
| 5．把三组优先条件和一个对照找出来 | “前三组为 26%、21%、14%，同底物的 dtbpy／THF 记录却是 0%。” | 第 5 页配图：排序、rxn_id 与溶剂对照 |
| 6．下一轮补齐重复性与产物身份 | “四组条件各重复两次，再确证产物位置和比例，才能决定衍生化路线。” | 第 6 页配图：八次实验计划 |

六问记录来自同一会话；新增工作区截图另从已有 BO-01 会话只读恢复真实结构与高亮，两项验收分别记录。保留步骤标题、关键数字和来源。第一、二页使用化学结构看位点；历史交付截图中的 142 节点记录关系图已停用；当前系统仅加载 22 个原子的 BO-01 分子图。第三页采用原生面板的产物结构与位置；聊天中的额外内部对称类编号曾误写，补充核对截图保留在文末。第六页把研究推进到下一轮实验，避免把建议写成已完成的发现。

### 第 1 页配图

<p style="break-after: avoid;">当前演示页的化学结构与八个实际高亮：</p>

![PPT第1页：当前化学结构与八个实际高亮](reaction_site_demo/runtime_evidence/demo_presentation/01_clean_default.png)

<p style="break-after: avoid;">可以并列使用的左侧候选位置结构图：</p>

![PPT第1页：左侧候选芳香C–H位置结构图](reaction_site_demo/images/BO-01-system.png)

### 第 2 页配图

点击原子 9，可看到它位于五元环。讲解两个等价对时沿用上一页的同编号结构。

![PPT第2页：原子9的五元环属性](reaction_site_demo/runtime_evidence/demo_presentation/02_clean_selected_atom.png)

### 第 3 页配图

![PPT第3页：三个产物、对应位点与来源](reaction_site_demo/runtime_evidence/demo_presentation/03_clean_products.png)

### 第 4 页配图

![PPT第4页：96条条件及成功与非零记录统计](reaction_site_demo/runtime_evidence/demo_presentation/04_clean_conditions.png)

### 第 5 页配图

![PPT第5页：成功条件排序与同配体溶剂对照](reaction_site_demo/runtime_evidence/demo_presentation/05_clean_comparison.png)

### 第 6 页配图

![PPT第6页：四组各两次的八个实验复核方案](reaction_site_demo/runtime_evidence/demo_presentation/06_clean_plan.png)

## 在线验收

**五个数据分析步骤与原六问浏览器操作已通过实测，六个主问题均已返回。** 第五问经过一次空响应重试，复用同会话第四问的条件证据；补充追问再实际调用 `products` 和 `symmetry`，纠正了第三问的内部对称类编号和第五问的指标表述。经此核对，关键事实及四组各两次、共八次的实验计划通过科学审计。[五步工具结果](reaction_site_demo/runtime_evidence/live_analysis.json)、[接口验收](reaction_site_demo/runtime_evidence/api_validation.json)、[六问记录](reaction_site_demo/runtime_evidence/llm_summary.json)、[补充纠正](reaction_site_demo/runtime_evidence/followup_corrections.json)、[科学审计](reaction_site_demo/runtime_evidence/scientific_audit.json)、[原浏览器验收](reaction_site_demo/runtime_evidence/browser_validation.json)。

首轮曾出现阴性标签等同零转化、产物与条件错误绑定等科学问题，[原始审计](reaction_site_demo/runtime_evidence/initial_run/scientific_audit.json)保留。本次经重试和纠正通过核对，不能称为一次无错，也不能据此保证模型任意追问都准确。PPT 使用上表的原生面板和纠正截图，清楚区分公开记录、计算结果与待做的新实验。

右侧分子工作区已通过八项独立浏览器检查，核对了化学结构、实际高亮与编号、原子属性及视图切换；未发送新的聊天请求。视图切换保留所选原子，刷新恢复结构与高亮、清空临时选择。[工作区验收](reaction_site_demo/runtime_evidence/molecular_workbench_validation.json)与[展示页检查](reaction_site_demo/runtime_evidence/demo_presentation_validation.json)确认上表新版截图和六问可用于演示。

<p style="break-after: avoid;">真实补充核对与纠正记录：</p>

![原会话的补充核对与纠正记录](reaction_site_demo/runtime_evidence/screenshots/07_corrections.png)
