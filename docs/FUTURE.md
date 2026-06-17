# iGraph 未来方向：竞争力与壁垒

> 本文档讨论：在系统开源后，**为了让"形似的复刻"无法在短期内追上我们**，我们应该把工程与研究重心放在哪里。
>
> 写作前提：架构本身（FastAPI + MCP + LangChain 风格的 Agent 循环）的代码壁垒有限——任何认真投入的团队 2-4 周可以拼出一个看起来一样的骨架。**真正能拉开差距的从来不是骨架，而是骨架之外的资产**：经过验证的解题模式、工具/算法的 LLM 友好度、垂直领域的 know-how、可自我演化的能力。
>
> 本文档共识贯穿全文：**绝对不靠"工具数量"或"算法覆盖度"建立壁垒**——这两个维度最容易被衡量、也最容易被堆体力赶上。

---

## 0. 一句话判断

> 把所有精力投到 **"会自我演化的图 Agent"** 这一件事上：
> Skills/Playbooks 沉淀解题经验、Sub-Agents 完成专业化分工、GraphML 提供超越规则的推理能力、Memory 跨会话累积用户/项目知识——四者闭环，形成一个**用得越多越强**的系统。
>
> 单点极致都不构成壁垒；**四者协同 + 持续迭代 1-2 年**才构成。

---

## 1. 现状盘点（2026-05）

为避免在已建好的能力上重复投入，先明确 **已经有什么** vs **还差什么**。

### 1.1 已有资产（开源后立即可见，但难以快速复刻细节）

| 类别 | 资产 | 隐性价值（别人短期看不到） |
|------|------|----------------------------|
| 架构 | 三层服务分离（API Gateway / MCP / GraphData）+ ProcessSupervisor | 状态宿主分离踩过的坑、stdio + streamable-http 双模式适配 |
| 工具 | 50+ 图算法工具（compute.py），统一截断协议（`_truncated/_total`） | 每个工具的参数裁剪、返回结构对 LLM 友好度的反复打磨 |
| Sub-Agents | Planner / PlanSolver / QueryAnalyzer / LLMToolSelector / ContextCompressor / MemoryUpdate / SkillGenerator / PlaybookReviewer / TextGraphExtractor / PlaybookDedup | 每个子 Agent 的 prompt + 失败 fallback + per-turn 缓存策略 |
| Skills | 11 个内置 playbook（centrality / community / path / RAG / ML training / ML inference / state check / dataset / data ingestion ×2 / reporting） | 每个 SKILL.md 的步骤、激活条件、工具组合是反复打磨的产物 |
| GraphML | GCN/GAT/SAGE × 节点分类/链接预测/预训练（Node2Vec / 对比学习 / MAE） | 训练超参默认值、artifact 版本化、与 LLM 工具调用的衔接 |
| Memory | Frozen snapshot 模式 + 安全扫描 + 多 provider + REST governance | prefix cache 友好的设计抉择、注入防御、scope 抽象 |
| 协议 | 文本双标签协议（`<think>+<tool_call>`/`<answer>`） + native FC 双模式 | 三家 LLM（OpenAI/Anthropic/Qwen）的格式归一化经验 |
| 自我演化 | PlaybookReviewer（validated playbook → review queue → user skill） | 闭环路径已铺设——但还没有真正"跑起来"的飞轮 |

### 1.2 已知缺口（DESIGN.md §25）

1. 工具调用串行执行
2. TextExtractor LLM 辅助抽取未完整集成
3. Neo4j 后端骨架未接入 GraphSession
4. Memory `MemoryScope` 仍硬编码 `default_user`
5. LLM Selector 每 turn 增加 300-500ms
6. 本会话内写入的记忆要到下次会话才生效

这些是**战术性缺口**，本文档第 6 节列出补齐节奏。**战略方向**在第 2-5 节。

---

## 2. 核心方向（按"短期不可超越性"排序）

### 方向 ①：Skills / Playbook 飞轮——把"解题经验"沉淀为可执行资产 ★★★★★

**这是最强的壁垒**。代码可以被读懂，但"对每一类业务问题，应该用什么工具组合 + 什么 prompt 模板"是只能靠时间累积的隐性资产。

#### 2.1 现状

- 已有 `SkillRegistry` 扫描 `bundled / user / review_queue` 三层
- 已有 `PlaybookReviewer` 把 validated playbook 转成 review queue 中的 skill 候选
- 已有 `SkillGenerator` 从对话中识别出可复用的工作流
- **但飞轮没有真正运转起来**——绝大多数会话不会产生新 skill，已生成的也没有被衡量、迭代、淘汰

#### 2.2 目标（12 个月）

让 Skills 成为**用得越多越强**的核心资产：

1. **Skill 召回准确率**：在 100+ 用户会话集上达到 ≥85% top-3 命中（"该激活时被激活"）
2. **Skill 冷启动**：从用户上传 1 份业务数据 + 5 轮对话，自动产出 ≥1 个候选 skill
3. **Skill 治理**：每个 skill 有调用次数 / 成功率 / 平均时长指标，低质量 skill 自动归档

#### 2.3 关键工作

| 任务 | 描述 |
|------|------|
| Skill 评测集 | 维护 200+ 真实业务 query → 期望 skill 列表的对照集，作为 `SkillSelector` 改进的基线 |
| Skill 元数据扩展 | 在 `SkillSpec` 中加入 `usage_count` / `success_rate` / `last_used_at` / `domain_tags` |
| 自动 A/B | 对每个 query，同时跑"激活 skill"和"不激活"两路，记录步数/工具调用次数/成功率，作为 skill 价值度量 |
| Skill 合并/分裂 | `PlaybookDedup` 已有雏形——扩展为定期跑：相似度高的合并、覆盖步骤过多的分裂 |
| 行业 skill 包 | 金融反欺诈 / 生物网络 / 知识图谱 / 社交分析 4 个领域，每个 5-10 个 skill，作为开源版的"垂直 demo"，闭源版做更深 |

#### 2.4 为什么这是最强壁垒

- 评测集本身就是壁垒——别人可以抄代码但抄不到"真实用户在问什么"
- 行业 skill 的步骤 + prompt 是几十次迭代的产物，新人重新走一遍需要等量时间
- 一旦用户在 iGraph 上产生了自己的 skill 库，迁移成本就非常高（**用户侧的网络效应**）

---

### 方向 ②：Sub-Agent 生态深化——从"支撑型"到"专家型" ★★★★★

#### 2.1 现状

当前 9 个 sub-agents 都是**支撑型**（Planner / Selector / Compressor / MemoryUpdate / TextExtractor 等），它们辅助主 agent，但本身不解决业务问题。

这一层做得已经不错，但天花板有限——支撑能力的边际收益递减。

#### 2.2 目标：扩展出"专家型"Sub-Agents

按图问题的天然分解，引入**领域专家 sub-agent**，每个负责一类深度任务：

| Sub-Agent | 负责什么 | 与主 Agent 的协议 |
|-----------|----------|--------------------|
| `SubgraphAnalyst` | 给定子图描述，深入分析其结构特征（密度、模体、瓶颈节点） | 主 Agent 给定 node_set / edge_set，子 Agent 返回结构化报告 |
| `CommunityProfiler` | 给定社区检测结果，对每个社区做画像（核心节点、跨社区桥接、稳定性） | 输入 community_id 列表，输出每社区的 profile |
| `PathReasoner` | 复杂多跳推理（"A 通过哪些节点最可能影响 B"），结合中心性 + 路径分析 + 链接预测 | 输入查询，输出排序后的解释链 |
| `AnomalyDetector` | 综合多种检测信号（中心性异常 / 社区异常 / GNN 异常分） | 输入图状态，输出异常节点/边列表 + 置信度 |
| `GNNExperimenter` | 自动化模型选择 + 超参搜索 + 评估报告 | 输入任务类型 + 标签，输出最佳模型 artifact + 评测 |
| `GraphRewriter` | 图操作重写（合并节点、压缩、抽样、加噪），用于稀疏图增强或可视化简化 | 输入策略，输出新 GraphData |

#### 2.3 关键设计点

- **共享图状态**：所有专家 Sub-Agent 通过 `agent_session_id` 访问同一份 GraphSession，这是 iGraph 区别于 AutoGen / CrewAI 等通用多 agent 框架的关键优势
- **结构化交接协议**：每个专家 sub-agent 的输入输出 schema 严格化，让主 Agent 可以稳定 orchestrate
- **冲突解决**：当多个 sub-agent 给出不一致结论（例如 AnomalyDetector 和 CommunityProfiler 对同一节点判断不同），需要明确的仲裁规则
- **失败传播**：sub-agent 失败时，主 Agent 应该能降级而非崩溃

#### 2.4 与方向 ① 的协同

每个专家 Sub-Agent 都对应一组 Skills——专家是"能力载体"，Skill 是"调用模式"。两者一起构成"专业化 + 经验化"的双层结构。

---

### 方向 ③：GraphML 第二曲线——从"训练工具"到"图原生智能" ★★★★

#### 2.1 现状

已有 GCN/GAT/SAGE 的训练/推理 + 三种预训练任务，是个完整的**功能集**，但**还停留在"LLM 调用 ML 工具"层面**。LLM 自己并不真正理解何时该用 GNN、用哪种 GNN、训练结果该如何与图算法结合。

#### 2.2 目标：让 LLM 能"自主进行图机器学习"

| 能力 | 当前 | 目标 |
|------|------|------|
| 任务识别 | 用户明确说"训练节点分类" | 用户说"预测哪些用户会流失"，LLM 自动判断这是节点分类 + 选定标签字段 |
| 模型选择 | 用户/skill 指定 GCN | LLM 基于图规模/异配性/特征维度自动选模型 |
| 超参调整 | 默认值 | 内置小规模 NAS / HPO，给出训练前预估 + 训练后调优 |
| 结果整合 | 推理结果作为列表返回 | LLM 能把 GNN 输出（embedding / 概率）与传统算法（中心性、社区）联合解释 |
| 持续学习 | 一次训练一次推理 | 当图变化超过阈值，自动建议重新训练并对比新旧模型 |

#### 2.3 关键工作

1. **Graph Profiler**：在训练前自动统计图特性（同配/异配、稀疏度、特征分布），驱动模型选择
2. **超参先验库**：根据图规模 + 任务类型，给出经验最优默认（来自我们自己跑过的 1000+ 实验）
3. **混合推理**：开发 `combined_reasoning` 工具，输入"GNN 节点 embedding + 中心性"，输出综合排序——这是单独看 GNN 或单独看中心性都给不出的视角
4. **跨图迁移**：基于预训练 + fine-tune 的工作流，让用户在新图上几分钟内得到可用模型（"iGraph 自带行业基础模型"成为开源版的稀缺特性）

#### 2.4 长期愿景

**自家训练一个"图 Agent 专用的小模型"**（参数 1B-7B 量级），专门做"自然语言图问题 → 工具调用序列 + 参数"的 planning 任务。这是**最硬核的护城河**——开源代码不会让别人得到我们的模型权重。

数据来源：
- 我们已有的 SFT 数据导出能力（`api_gateway/app.py` 中已经预留）
- 文本协议天然产生可学习的 trace
- Skills 提供高质量正样本

不需要从头训，可以基于 Qwen2.5-7B / Llama 3 8B 之类做 LoRA + 拒绝采样。第一个版本目标：在我们自己的评测集上 planning 准确率超过 GPT-5-2 / Claude Sonnet 4-6。

---

### 方向 ④：图原生 Memory 与 Long-Context ★★★★

#### 2.1 现状

Frozen Snapshot 模式 + Memory provider 抽象 + 安全扫描，已经是相当完善的 memory 工程。但当前 memory 是**通用 KV/playbook 形态**，并未利用"图"这个领域特性。

#### 2.2 目标：图特化的对话记忆

让 long-running session（用户跟同一张图对话数百轮）能稳定运行：

| 维度 | 当前 | 目标 |
|------|------|------|
| 操作历史 | 全量在 messages 里 → ContextCompressor 摘要 | 结构化"图操作历史 DAG"：每个工具调用的输入/输出关系，可重放、可追溯 |
| 已确认事实 | 散落在对话中 | "图属性账本"：本会话已确认的 `nodes_count = 1234`、`is_connected = false` 等，主 Agent 直接读 |
| 活跃焦点 | 无显式追踪 | "活跃子图栈"：用户当前关注的节点/社区，会话切换焦点时自动 push/pop |
| 跨会话记忆 | Skill 形态（步骤性） | + "图实体记忆"：上次会话中识别出的关键节点（如金融图中的"高风险账户"），下次会话提示用户 |

#### 2.3 与 ContextCompressor 的关系

ContextCompressor 已经做的是**通用文本摘要**。新增的"图操作历史 + 活跃子图栈"是**结构化记忆**，两者并行：长上下文 = 通用摘要 + 结构化记忆 + frozen snapshot。

#### 2.4 为什么这是壁垒

通用 LLM 框架不会做"图原生记忆"——它们的 memory 抽象是文本/向量。我们做这一层等于定义"图 Agent 应该如何管理记忆"的事实标准。**别人就算抄了代码，没有同款的"操作历史 DAG schema 设计"也跑不起来**。

---

### 方向 ⑤：评测体系与"图 Agent Benchmark" ★★★★（隐藏的最强护城河）

> **如果只能做一件能"出圈"的事，应该是这个。**

#### 2.1 为什么重要

LLM 时代，**谁定义评测，谁定义赛道**。LangChain 通过 LangSmith 占据 LLM 应用评测心智，PyG 通过 OGB 占据图 ML 评测心智——但**"图 Agent"目前没有公认的 benchmark**。

我们有四点优势：
1. 真实用户数据沉淀（SFT 导出能力已具备）
2. 已有 50+ 工具的统一接口可作为评测环境
3. Skills 系统天然提供"标准解法"作为参考答案
4. 多 LLM 后端，可以做横向对比

#### 2.2 目标

发布 `iGraphBench`：

| 维度 | 内容 |
|------|------|
| 任务集 | 200+ 自然语言图问题，覆盖 10+ 领域，难度三档 |
| 标准答案 | 每个任务有"理想工具调用序列"和"理想答案" |
| 评测维度 | (a) 答案正确性 (b) 工具调用次数 (c) 消耗 token (d) 完成时间 |
| Leaderboard | 不同 LLM × 不同 Agent 框架（我们 vs LangChain vs AutoGen vs naive ReAct）的对比榜 |
| 配套论文 | 一篇短 paper 描述设计与发现 |

#### 2.3 收益

- **学术影响力**：被引用 → 进入"图 Agent"领域的 default reading list
- **品牌**：开发者第一次想到"图 Agent benchmark"就想到我们
- **数据壁垒**：评测集本身是不开源的子集（开源 1/3 + 闭源 2/3），形成长期护城河
- **指引内部研发**：我们自己的迭代有了客观参照

---

## 3. 守住已有优势的工程项

下列不是"新方向"，而是**防守性投入**——不做的话，已有的优势会被拆解。

### 3.1 工具 LLM 友好度的体系化

- 把 `compute.py` 的截断协议从"约定"升级为"测试"：自动化测试每个工具在大图（1M 边）下的输出大小
- 给每个工具补"LLM 调用样例 + 期望输出 + 常见错误"三件套，作为 PR 必填项
- 维护"工具调用准确率回归测试"：每次改 system prompt 都跑一遍 200 case 的工具选择测试

### 3.2 多 LLM 适配层的稳定性

- `ProtocolStreamParser` 的容错覆盖应该是**回归测试套件**，每接入一个新模型先跑一遍格式遵从性
- native FC 模式和 text 模式之间应有自动一致性测试（同样输入 → 同样工具调用序列）

### 3.3 自我演化飞轮的"启动燃料"

PlaybookReviewer + SkillGenerator + PlaybookDedup 已经存在，但**飞轮还没真正转起来**。需要：
- 内部 dogfooding：团队成员自己用，每周梳理 review queue
- 收集 100+ session 后才能判断飞轮是否成立——需要明确的"启动期 KPI"

### 3.4 Neo4j / 大规模图后端

存储抽象层 ABC 已经在，但 Neo4j 接入只有骨架。**单机内存图**是开源版完全够用的形态，但**亿级图必须有外置后端**才能撑得住企业用户。这是"工程壁垒"维度的关键投入。

---

## 4. 不该做的事（避免误投入）

| 反模式 | 为什么不要做 |
|--------|--------------|
| 把工具数量从 50+ 堆到 200+ | 工具数量不是壁垒，且会让 selector 和 system prompt 噪音爆炸 |
| 自研一个 Agent 编排框架取代 LangChain | 通用 Agent 框架是红海，我们的差异化在"图"，不在"通用 Agent" |
| 上多模态（图像/语音输入） | 与图分析的交集小，扩散精力 |
| 自研图存储引擎 | NetworkX + Neo4j 双后端足够，自研图存储是 5 年级工程，吞噬资源 |
| 急于做"完整可视化前端" | 优秀的图可视化（Cytoscape / Gephi 量级）需要专门团队，做半截不如不做 |
| 提前做"多用户协作 / SSO / RBAC" | 在产品形态稳定前做企业版功能，会被路线图变更不断推翻 |

---

## 5. 1 年路线图建议

按"做完后能否用一句话讲出对手追不上的点"来排序。

### Q1（重点：飞轮启动 + 评测铺设）
- [ ] Skill 评测集第一版（50 case → 100 case）
- [ ] Skill 元数据扩展（usage_count / success_rate）
- [ ] PlaybookReviewer 的"启动期 dogfood"——团队每周一次
- [ ] `iGraphBench` 设计稿 + 任务集起草（30 case）
- [ ] 工具回归测试套件第一版

### Q2（重点：专家 Sub-Agent ×2 + 图原生 Memory）
- [ ] `SubgraphAnalyst`、`AnomalyDetector` 两个专家 sub-agent 落地
- [ ] 图操作历史 DAG schema + ContextManager 集成
- [ ] 图属性账本（已确认事实的结构化存储）
- [ ] LLM 辅助 TextExtractor 完整集成（DESIGN §25.6）

### Q3（重点：GraphML 智能化）
- [ ] Graph Profiler + 自动模型选择
- [ ] 超参先验库 + HPO 集成
- [ ] `combined_reasoning` 工具（GNN + 传统算法联合推理）
- [ ] Neo4j backend 接入 GraphSession（DESIGN §25.7）

### Q4（重点：差异化输出）
- [ ] `iGraphBench` 公开版发布 + Leaderboard 网站
- [ ] 配套短 paper（arXiv）
- [ ] 行业 skill 包 ×2（金融 + 知识图谱）
- [ ] 自训 planning 小模型 v0（LoRA）的可行性 spike

---

## 6. 战术性 Backlog（DESIGN §25 的逐项消化）

| # | 项 | 优先级 | 估时 | 备注 |
|---|----|-------|------|------|
| 1 | 工具调用并行 | P1 | 中 | 影响多步骤 skill 的延迟 |
| 2 | TextExtractor LLM 集成 | P1 | 中 | 是 data ingestion 飞轮关键 |
| 3 | Neo4j backend 接入 | P2 | 大 | 大规模图必备 |
| 4 | MemoryScope 多用户/工作区 | P2 | 中 | 企业化前必做 |
| 5 | LLM Selector 延迟优化 | P3 | 小 | 已有 fallback 兜底 |
| 6 | 本会话内 memory 实时生效 | P3 | 小 | 设计上可接受，但用户偶尔会困惑 |

---

## 7. 风险提示

1. **路线分散是最大风险**：上述 5 个方向都做不到，最多专注 2-3 个。优先级从高到低：① Skills 飞轮 → ⑤ Benchmark → ② Sub-Agent 专家化 → ④ 图原生 Memory → ③ GraphML 自主化。
2. **开源节奏**：在 ① 和 ⑤ 形成可见资产**之前**完全开源是危险的——会被大公司基于代码做出更精致的版本。建议**核心代码开源 + 评测集闭源 + 行业 skill 包付费**的混合策略。
3. **小模型自训的硬投入**：方向 ③ 末尾的小模型方案是高风险高回报项，需要至少 1 个全职研究员 + 一定算力预算才有意义，不能"试试看"。
4. **生态主动权**：不要等开源后再思考治理——MCP 工具规范、Skill frontmatter 标准、benchmark 任务格式应该在开源前就定型，避免被社区分叉。

---

## 8. 一年后的成功标准

如果一年后能讲出下面这五句话中的至少三句，本路线就成功了：

> 1. "iGraph 的 Skills 库已经是图分析领域最大的开源 playbook 集合。"
> 2. "iGraphBench 是图 Agent 领域被引用最多的 benchmark。"
> 3. "我们有 X 个专家 sub-agent，处理图问题的成功率是 baseline 的 1.5 倍。"
> 4. "我们的图原生 Memory 让长会话（500+ 轮）依然稳定。"
> 5. "我们自训的 7B planning 小模型，在 iGraphBench 上超过了 GPT-5-2。"

如果只能讲第 1 句，依然是壁垒；
如果只能讲第 5 句，是奇迹但脆弱；
如果一句都讲不出，说明这一年我们在堆功能，没有在沉淀。
