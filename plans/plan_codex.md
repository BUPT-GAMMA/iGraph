# iGraph 详细迭代升级计划

> 本计划综合 [compare_cc.md](/Users/idealist/vscode_local/compare_cc.md) 与 [compare_codex.md](/Users/idealist/vscode_local/compare_codex.md) 两份分析结论形成。目标不是把 iGraph 改造成重型 YiGraph，而是在保持 iGraph 现有工程优势的前提下，补齐规划层、知识层、数据层与平台层能力。

---

## 1. 升级目标

iGraph 当前已经具备以下优势：

- 多服务边界清晰：`API Gateway -> iGraph -> MCP -> GraphData/GraphML`
- 会话系统成熟：惰性恢复、日志持久化、SSE 流式交互
- 协议抽象完善：Text Protocol / Native FC 双栈统一
- 图学习已落地：`train / infer / config / visualize`
- 长期记忆与技能注入已具备基础能力

但当前主要短板也很明确：

- 缺少显式任务规划层
- 算法覆盖深度不足
- 多模态数据建图能力弱
- 图状态偏“会话图”，缺少数据集层
- 缺少大图/持久化图后端
- 报告化、结构化分析输出偏弱

本计划的核心目标是把 iGraph 从“强工程的图工具型 Agent”升级为“强工程 + 强规划 + 强图学习 + 可平台化”的图智能体系统。

---

## 2. 升级原则

### 2.1 保留 iGraph 的核心差异化

后续升级必须保护以下核心价值，不做破坏式重构：

- 透明协议能力
- 轻量部署能力
- 多会话隔离能力
- GraphML 一体化能力
- 低耦合服务边界

### 2.2 吸收 YiGraph 的“规划方法”，而不是复制其“重基础设施”

借鉴重点：

- DAG/步骤化分析思路
- 算法知识库
- 数据集与建图思维
- 多模式分析流程

不建议直接照搬：

- 重型 Neo4j + Milvus 依赖作为默认路径
- 过度内聚的巨型 Scheduler
- 全量重型 DAG 引擎一次性落地

### 2.3 采用“低侵入、渐进增强”路线

迭代方式应优先：

1. 在现有架构上增加新层，而不是推倒重来
2. 优先做可选能力，再做默认能力
3. 优先补齐闭环，再追求能力广度

---

## 3. 目标架构

建议将 iGraph 演进到如下结构：

```text
Client / Web / CLI
        ↓
API Gateway
        ↓
iGraph Session Orchestrator
        ├── Plan Layer
        │   ├── Query Complexity Detector
        │   ├── Step Planner
        │   ├── Plan Store
        │   └── Plan Executor
        ├── Memory Layer
        ├── Skills Layer
        └── Protocol Layer
                ↓
MCP Service
        ↓
GraphData Service
        ├── Graph Session / Graph Snapshot
        ├── Algorithm Engine
        ├── GraphML Module
        ├── Dataset Layer
        ├── Retrieval Layer
        └── Storage Adapter
            ├── MemoryStorage
            └── Optional Neo4jStorage
```

其中新增的关键层有 4 个：

- `Plan Layer`：显式任务规划与执行
- `Dataset Layer`：从“会话图”走向“数据集图”
- `Retrieval Layer`：轻量 RAG/算法知识召回
- `Storage Adapter`：为大图/持久化图预留扩展

---

## 4. 总体阶段划分

建议拆成 5 个阶段推进：

- Phase 0：低成本高收益增强
- Phase 1：规划层落地
- Phase 2：数据集层与建图层落地
- Phase 3：平台化与生产化增强
- Phase 4：大图能力与高级协作能力

建议实施节奏：

- Phase 0：1-2 周
- Phase 1：2-4 周
- Phase 2：3-5 周
- Phase 3：2-4 周
- Phase 4：按资源独立排期

---

## 5. Phase 0：低成本高收益增强

## 5.1 目标

在不改变主架构的前提下，优先补齐最明显的短板：

- 算法覆盖不足
- token 浪费
- 记忆召回偏弱
- 图数据不持久

## 5.2 交付项

### A. 扩展图算法目录

目标：

- 将图工具从当前 19 个扩展到 40-60 个
- 仍以 `tool_catalog.py` 为唯一真相源

优先新增：

- 中心性：`harmonic_centrality`、`eigenvector_centrality`、`voterank`
- 社区：`girvan_newman`、`leiden` 或兼容替代方案
- 结构：`k_core`、`core_number`、`ego_graph`
- 路径：`has_path`、`k_shortest_paths`
- 链路相关：`jaccard_coefficient`、`adamic_adar`
- 连通性：更细粒度 components/connectivity 工具

涉及模块：

- `graph_data_service/compute.py`
- `graph_data_service/tool_catalog.py`
- `tests/servers/test_graph_data_app.py`
- `tests/servers/test_mcp_modules.py`

验收标准：

- 新增工具可被 MCP 自动注册
- Text Protocol 与 Native FC 均可调用
- 至少补齐 15 个自动化测试

### B. 工具目录动态裁剪注入

目标：

- 避免 Text Protocol 下每轮注入全部工具
- 降低 token 成本

方案：

- 新增 Tool Retrieval 逻辑
- 对当前 query + recent context 做工具相关性排序
- 默认只注入 top-K 工具
- 当置信度不足时退回全量注入

涉及模块：

- `backend/agent/igraph.py`
- `backend/agent/text_protocol_driver.py`
- 可新增 `backend/agent/tool_selector.py`

验收标准：

- 平均工具 prompt token 降低 40% 以上
- 不影响现有核心任务成功率

### C. 记忆召回语义化

目标：

- 把当前关键词匹配升级为语义召回

方案：

- 为 memory item / playbook 构建 embedding
- 使用轻量本地 embedding 模型
- 保留关键词打分作为 fallback

涉及模块：

- `backend/agent/memory_engine.py`
- 新增 `backend/agent/memory_retriever.py`

验收标准：

- memory prompt 命中率明显提升
- 单次召回延迟控制在可接受范围

### D. 图快照持久化

目标：

- 服务重启后恢复最近图状态
- 支持有限版本回退

方案：

- 在每次 `set_graph` / `clear_graph` / `to_undirected` 后写快照
- 每 session 保留最近 N 个图快照
- 在 `API Gateway` 恢复 agent 时同步恢复图 meta

涉及模块：

- `graph_data_service/session.py`
- `graph_data_service/io.py`
- `backend/api_gateway/app.py`

验收标准：

- 重启服务后图可恢复
- 支持 `undo_graph` 或等价回退能力

## 5.3 Phase 0 里程碑

### M0.1

- 工具扩展完成
- 工具测试补齐

### M0.2

- 动态工具注入上线
- 记忆语义召回上线

### M0.3

- 图快照恢复完成
- 文档与运行指南更新

---

## 6. Phase 1：规划层落地

这是整个升级计划里最关键的一阶段。

## 6.1 目标

为 iGraph 增加“显式任务规划层”，让系统从单纯的 ReAct Agent 升级为 Plan-aware Agent。

## 6.2 设计思路

不直接上重型 DAG 引擎，而是分两步走：

### 第一步：Step Plan

先实现轻量计划对象：

```json
{
  "plan_id": "...",
  "mode": "direct|staged|planned",
  "steps": [
    {
      "step_id": "s1",
      "goal": "加载图并确认图结构",
      "tool_candidates": ["load_graph", "get_graph_info"],
      "depends_on": []
    },
    {
      "step_id": "s2",
      "goal": "计算关键节点",
      "tool_candidates": ["pagerank", "degree_centrality"],
      "depends_on": ["s1"]
    }
  ]
}
```

### 第二步：轻量 DAG

当 Step Plan 稳定后，再引入：

- 依赖边
- 中间结果引用
- 用户可编辑 plan
- plan replay

## 6.3 交付项

### A. Query Complexity Detector

目标：

- 判断问题是否值得进入规划模式

触发信号：

- 用户输入中出现多步骤语义
- 问题过长
- 工具调用历史复杂
- 预估至少需要 2 个以上图工具

涉及模块：

- 新增 `backend/agent/query_analyzer.py`

### B. Plan Builder

目标：

- 基于问题生成步骤化 plan

建议实现：

- LLM 输出严格 JSON plan
- 结合工具 catalog 做 plan 校正
- 过滤不存在或不可用工具

涉及模块：

- 新增 `backend/agent/planner.py`
- 新增 `backend/agent/plan_schema.py`

### C. Plan Executor

目标：

- 将 plan 执行映射回现有 iGraph/MCP/GraphData 工具链

核心原则：

- plan 只负责定义步骤
- 具体工具执行仍由 iGraph 执行
- 保持当前协议层和 MCP 层不变

涉及模块：

- `backend/agent/igraph.py`
- 新增 `backend/agent/plan_executor.py`

### D. Plan Store

目标：

- 让计划可查看、可恢复、可回放

存储建议：

- 先写入 `logs/{session_id}/plan.json`
- 后续再抽象到统一 store

### E. 前端只读计划展示

目标：

- 在 Web UI 中显示计划步骤与当前执行状态

先做：

- 步骤列表
- 当前步骤高亮
- 步骤完成状态

后做：

- DAG 图视图
- 编辑能力

涉及模块：

- `index.html`
- `backend/api_gateway/app.py`

## 6.4 验收标准

- 对复杂任务能先给出结构化计划
- 用户可看到步骤列表
- 每一步执行结果可追踪
- 复杂问题成功率高于现有纯 ReAct 模式

## 6.5 风险

- 计划生成质量不稳定
- 计划执行与工具真实能力不一致
- 计划层引入后，简单问题反而变慢

对应控制：

- 只对复杂任务启用计划模式
- 保留原始 direct 模式作为 fallback
- 增加 planner 校验与工具可用性校验

---

## 7. Phase 2：数据集层与建图层落地

## 7.1 目标

把 iGraph 从“单会话图工具”升级为“可持续分析数据资产的图平台”。

## 7.2 交付项

### A. Dataset Registry

目标：

- 增加 graph/text/table 三种数据资产类型

核心对象：

- Dataset
- Dataset Version
- Materialized Graph Snapshot

建议目录：

```text
datasets/
  {dataset_id}/
    dataset_meta.json
    versions/
    derived_graphs/
```

涉及模块：

- 新增 `graph_data_service/datasets/`
- `backend/api_gateway/app.py`

### B. Text/Table/Graph 统一接入

目标：

- 统一上传、注册、解析与加载流程

建议先支持：

- graph: `.json` / `.npz` / edge list
- text: `.md` / `.txt` / `.pdf` 基础解析
- table: `.csv`

### C. Text/Table -> Graph Pipeline

目标：

- 建立轻量建图链路

建议分成两个阶段：

1. Schema Extraction
2. Graph Construction

不要一步到位让 LLM 直接吐全图。

建议新增工具：

- `extract_graph_schema_from_text`
- `extract_entities_relations`
- `build_graph_from_records`
- `materialize_dataset_graph`

### D. Dataset-bound Session

目标：

- 会话可绑定数据集版本
- 图分析结果与数据资产关联

这样后续才能支持：

- 相同数据集多会话分析
- 分析可复现
- 模型结果与图版本对齐

## 7.3 涉及模块

- `graph_data_service/app.py`
- `graph_data_service/session.py`
- 新增 `graph_data_service/dataset_manager.py`
- 新增 `graph_data_service/ingestion/`
- `backend/api_gateway/app.py`

## 7.4 验收标准

- 可创建数据集并上传数据
- 可生成图快照并绑定会话
- 会话恢复后仍知道自己关联的数据集版本

## 7.5 风险

- 数据 schema 抽取不稳定
- 数据集与会话耦合过深
- 文本建图质量参差不齐

控制策略：

- 明确区分原始数据集与派生图
- 所有自动建图都要输出中间结构，支持人工确认

---

## 8. Phase 3：平台化与生产化增强

## 8.1 目标

提升 iGraph 在真实场景中的可用性、可管理性、可观测性。

## 8.2 交付项

### A. 轻量 RAG 层

目标：

- 为每个 session 或 dataset 提供文档检索能力

建议：

- 默认采用 `sqlite-vec` 或本地向量存储
- 不引入重型外部依赖作为默认方案

新增工具建议：

- `retrieve_context`
- `index_documents`

### B. 报告化输出

目标：

- 不只输出聊天式答案，还输出分析报告

支持格式：

- `markdown`
- `json`
- `report`

报告结构建议：

- 问题定义
- 分析计划
- 使用工具/算法
- 关键发现
- 风险与限制
- 下一步建议

### C. 多用户记忆隔离

目标：

- 让长期记忆从 `default_user` 升级为 `per-user`

涉及模块：

- `backend/agent/memory_engine.py`
- `backend/api_gateway/app.py`

### D. 图前端可视化增强

目标：

- 用交互式图替代当前有限展示

建议：

- 使用 `Cytoscape.js`
- 支持算法结果高亮
- 支持路径、社区、关键节点着色

### E. SFT/Replay 数据导出

目标：

- 强化 iGraph 在训练数据沉淀上的独特优势

导出类型：

- ShareGPT
- Alpaca-like
- Tool-call replay 格式

### F. 可观测性增强

目标：

- 增加 planner/工具/GraphML 的性能与质量指标

至少增加：

- plan 生成耗时
- 工具调用成功率
- 回答完成率
- GraphML 作业耗时
- memory 命中率

---

## 9. Phase 4：大图能力与高级协作能力

这一阶段不应过早启动，建议在前 3 个阶段稳定后推进。

## 9.1 可插拔图存储后端

目标：

- 支持 MemoryStorage + Optional Neo4jStorage

原则：

- GraphDataService 仍为统一门面
- 上层 Agent/MCP/Tool catalog 不感知存储细节

## 9.2 Graph Query 专用模式

目标：

- 引入独立于图算法模式的图查询模式
- 支持自然语言图查询

这部分可以吸收 YiGraph 的：

- `QueryRouter`
- graph query route

## 9.3 GraphML 编排化

目标：

- 让 planner 主动规划图学习步骤

例如：

- 先预训练 embedding
- 再做节点分类
- 再基于分类结果做风险分析

## 9.4 多 Agent 协作

目标：

- 在复杂任务中拆分子任务并并行执行

建议只在以下场景启用：

- 大图多指标分析
- 报告与计算分离
- 图学习 + 图分析组合任务

---

## 10. 模块改造清单

## 10.1 需要新增的核心模块

建议新增：

```text
backend/agent/
  query_analyzer.py
  tool_selector.py
  planner.py
  plan_schema.py
  plan_executor.py
  memory_retriever.py

graph_data_service/
  dataset_manager.py
  datasets/
  ingestion/
  retrieval/
  storage/
```

## 10.2 需要重点修改的现有模块

### `backend/agent/igraph.py`

要承担的新职责：

- 复杂度判定
- 计划模式切换
- 计划执行编排
- 结构化结果组织

### `backend/api_gateway/app.py`

要承担的新职责：

- dataset/session 绑定
- plan API 暴露
- 多用户隔离
- 导出接口

### `graph_data_service/app.py`

要承担的新职责：

- 数据集管理入口
- 文档检索入口
- 图快照版本入口
- 存储适配入口

### `graph_data_service/tool_catalog.py`

要继续保持：

- 唯一真相源

新增建议：

- 增加工具分组
- 增加 planner 可消费的 metadata
- 增加算法适用场景字段

---

## 11. 依赖顺序

建议严格按下面顺序推进：

1. 先做 Phase 0
2. 再做 Phase 1 规划层
3. 然后做 Phase 2 数据集层
4. 再补 Phase 3 的体验与平台能力
5. 最后考虑 Phase 4 的大图与协作

原因：

- 没有稳定工具与快照能力，规划层难落地
- 没有规划层，数据集层价值无法充分体现
- 没有前 3 阶段的闭环，大图与多 Agent 只会放大复杂度

---

## 12. 里程碑与验收建议

## 12.1 功能里程碑

### R1：强化版 iGraph

完成条件：

- 算法工具扩展
- 动态工具注入
- 语义记忆召回
- 图快照恢复

### R2：可规划的 iGraph

完成条件：

- 复杂任务进入 plan mode
- 计划可展示
- 步骤执行可追踪

### R3：数据集化 iGraph

完成条件：

- 支持 dataset
- 支持 text/table/graph 接入
- 支持派生图快照

### R4：平台化 iGraph

完成条件：

- 报告输出
- 轻量 RAG
- 多用户记忆隔离
- 前端交互图可视化

## 12.2 质量验收

每阶段都应配套：

- 单元测试
- 集成测试
- 端到端 replay 测试
- 文档更新

建议重点构建以下基准集：

- 简单图分析问题集
- 多步复杂图分析问题集
- 图学习任务集
- 数据建图质量验证集

---

## 13. 关键风险与应对

## 13.1 规划层过重

风险：

- 把 iGraph 拉向重型工作流引擎

应对：

- 先做可选 plan mode
- 只对复杂任务启用

## 13.2 数据集层复杂度失控

风险：

- 会话系统与数据系统混杂

应对：

- 严格区分 `session graph` 与 `dataset graph`
- 用独立 manager 管理数据集

## 13.3 GraphML 与 planner 融合过早

风险：

- 规划层尚未稳定，先编排 GNN 会放大复杂度

应对：

- GraphML 编排化放到 Phase 4

## 13.4 引入外部依赖过多

风险：

- 破坏 iGraph 轻量部署优势

应对：

- 默认采用轻量本地方案
- 外部数据库全部走 optional adapter

---

## 14. 最终建议

最推荐的执行策略是：

### 第一优先

- 扩展算法
- 优化工具注入
- 语义记忆召回
- 图快照恢复

### 第二优先

- 做轻量计划层
- 让复杂图分析从隐式 ReAct 升级为显式 Step Plan

### 第三优先

- 做数据集层和建图层
- 让 iGraph 从“会话图工具”升级为“图数据分析平台”

### 第四优先

- 做轻量 RAG、报告输出、交互图和多用户能力

### 最后再做

- Neo4j 可选存储
- Graph Query 独立模式
- GraphML 编排化
- 多 Agent 协作

最终目标应是：

> 用 iGraph 的工程底座承载 YiGraph 的规划思想，但坚持轻量、模块化、可部署、可演进的路线。

