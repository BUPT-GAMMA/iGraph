# iGraph 文档导航

本目录包含当前代码实现对应的完整文档。

> 当前架构入口是 [`AGENTOS_FOUR_MODULE_REFACTOR.md`](AGENTOS_FOUR_MODULE_REFACTOR.md)。
> `DESIGN.md`、`RUN_AND_TEST.md`、`DEPLOY_SERVERS.md` 和 `SKILLS_MEMORY.md`
> 主要记录重构前的进程内 Runtime，保留用于行为对照，不再作为启动依据。

## 文档列表

- `AGENTOS_FOUR_MODULE_REFACTOR.md`：**当前主文档**。四模块职责、AgentOS 映射、迁移阶段和依赖方向。
- [CI_QUALITY_POLICY.md](CI_QUALITY_POLICY.md)：当前 CI 强制检查、覆盖率过渡底线、改善目标和 PR 补测约定。
- [TOOL_DAG_EXECUTION.md](TOOL_DAG_EXECUTION.md)：工具资源目录、DAG 并行调度、后台作业与并发配置。
- [TOOL_DAG_VERIFICATION.md](TOOL_DAG_VERIFICATION.md)：真实模型问答、算法并行正确性和构图/前端回归验收记录。
- [GRAPH_CONSTRUCTION.md](GRAPH_CONSTRUCTION.md)：构图接口、数据集复用、优化预览与提交。
- [CONSTRUCTION_P1_VERIFICATION.md](CONSTRUCTION_P1_VERIFICATION.md)：构图资源隔离、候选扫描与超时修复的回归及性能验证。
- [CONSTRUCTION_P2_VERIFICATION.md](CONSTRUCTION_P2_VERIFICATION.md)：业务属性编辑、重试状态与 SQLite 外键修复的回归及基线差异。
- [STREAM_RECOVERY.md](STREAM_RECOVERY.md)：断流续传、完整快照、审批安全、依赖契约、故障测试与排障。
- [RUN_CANCELLATION.md](RUN_CANCELLATION.md)：当前任务终止、作业隔离、持久控制、故障验收及剩余发布门禁。
- [RUN_CANCELLATION_REVIEW_FIXES.md](RUN_CANCELLATION_REVIEW_FIXES.md)：终止功能审查问题的修复、故障回归与性能复测记录。
- `DESIGN.md`：重构前完整系统设计，作为兼容行为参考。
- `SKILLS_MEMORY.md`：重构前自研 Skills / Memory / Tools Selector 机制，保留用于数据映射和行为对照。
- `RUN_AND_TEST.md`：环境准备、`.env` 配置、启动方式、API 联调、测试命令与常见问题。
- `DEPLOY_SERVERS.md`：单机/拆分部署建议、完整环境变量清单、运维建议。
- `GRAPH_DATA_JSON_FORMAT.md`：`graph_data.v1` JSON 存储格式与服务交换文档格式。
- `GRAPH_ML_SERVICE.md`：GraphData 内置 GraphML 子模块说明、训练/推理参数。
- `NEBULAGRAPH.md`：NPZ/GraphData + NebulaGraph 混合存储的配置、同步工具、原生查询与验收方式。
- `guides/datasets/`：数据集加载完整指南、搜索索引与快速参考。
- `archive/plans/`：已完成迭代的历史计划与审查记录，不作为当前实施入口。

## 重构前基线摘要

> 以下条目记录旧进程内 Runtime 的能力基线，不代表当前默认实现。当前 Memory、Learning、
> Session Context、DecisionLog 与 Skills 以 AgentOS 四模块文档为准。

- **GraphML 内置于 GraphData**：`graph_ml_service/` 独立项目已合并为 `graph_data_service/graph_ml/` 子模块，无需单独部署，直接访问会话内存中的图数据。
- **ProtocolDriver 双模式**：通过 `LLM_TOOL_PROTOCOL=text|native` 切换文本协议和原生 Function Calling，前端接口不变。
- **ContextManager 内置 ProtocolDriver**：`context/` 包统一管理上下文组装与流式驱动，iGraph 通过 `context_manager.stream_turn()` 调用。
- **StatefulStreamParser**：合并流式解析与结果提取，消除了原有的三步解析链（stream → normalize → parse）；解析失败时自动重试（`_MAX_PROTOCOL_RETRIES` 次）。
- **模块重组**：`context/`（协议驱动）、`memory/`（记忆引擎）、`skills/`（渐进披露）和 `tools/`（LLM selector）拆分为独立包；新增 `model_registry.py` 集中管理模型名称。
- **planning_mode**：新增 `never / auto / always` 规划模式，可通过 `PATCH /api/sessions/{id}/planning-mode` 动态切换。
- **数据集 API**：新增 `GET /api/datasets`、`POST /api/sessions/{id}/load-dataset`、`POST /api/sessions/{id}/save-dataset`。
- **tool_catalog 唯一真相源**：工具规格集中在 `graph_data_service/tool_catalog.py`，MCP Server 动态注册，不再硬编码。
- **Agent 惰性创建**：`activate_session` 只读 meta，Agent 在第一次 `chat_stream` 时才创建，切换历史会话不再销毁其他 Agent。
- **Memory 架构升级**：引入 `MemoryManager` + `MemoryProvider` 抽象层；`UserProfile` 独立存储（`user_profile.json`），字段级优先级截断；frozen snapshot 机制保护 prefix cache；`memory_read` / `memory_delete` virtual tools。
- **去 embedding 化**：`backend/agent/embedding.py` 已删除；`LLMToolSelector` 用 LLM sub-agent 替代语义向量选工具；Memory 改为全量加载 frozen snapshot，不再做语义召回；零 sentence-transformers 依赖（GraphML 训练/推理相关的 torch 保留）。
- **Skills 渐进披露**：SKILL.md 升级为 YAML frontmatter 结构化格式；compact index 常驻，全文按需 `skill_view(name)` 加载；支持 `~/.igraph/config.yaml` 配置变量注入。
- **Memory 安全加固**：注入 pattern 检测（`safety.py`）覆盖 memory + playbook + skill；文件写入统一走原子操作（temp+fsync+os.replace）+ 迁移幂等标记。
- **Per-turn 工具选择缓存**：`LLMToolSelector.set_turn_id()` 在每个 user turn 入口重置，同一 turn 内多步 react 循环共享同一决策；fallback 改为关键词匹配而非全量工具加载。
- **Plan-aware Agent**：新增 QueryAnalyzer + Planner，复杂多步任务自动生成结构化分析计划并按步执行（`_run_plan_solver`，solve_* 事件前缀）。
- **算法扩展**：图算法从 19 种扩展到 50+，新增 Algorithm Catalog 算法知识层。
- **数据集平台**：Dataset Registry（数据集 CRUD + 版本）、Table/Text → Graph 建图管线、轻量 RAG。
- **图快照持久化**：GraphSession 支持快照写入/恢复/undo，服务重启后自动恢复图数据。
- **NebulaGraph 混合存储**：NPZ/GraphData 继续负责快照和 GraphML，当前图可显式异步同步到 NebulaGraph，并通过固定的 `FETCH PROP`、`GO`、`FIND SHORTEST PATH` 工具执行原生查询。
- **实体搜索工具**：新增 `search_node` / `search_edge`，支持名称精确匹配、语义模糊搜索、类型/度数/权重过滤，复用版本化索引缓存；配合路径和子图工具使用，可先模糊定位再精确分析。
- **结构化三元组建图**：新增 `build_graph_from_triples` 工具，从结构化三元组列表直接建图（relation→边类型，evidence→边文本，confidence→边特征）。
- **LLM 辅助文本抽取**：`TextGraphExtractor` sub-agent（`text_graph_extractor.py`）接入 `build_graph_from_text`，支持长文本分块、逐块 LLM 抽取、三元组去重与置信度过滤。
- **Playbook 去重**：新增 `PlaybookDedup` sub-agent（`playbook_dedup.py`），定期检测语义重复 playbook 并输出合并/归档方案，保持 playbook 库精简不冗余。

## 当前入口

- Backend：`python -m backend`（`python agent_server.py` 为兼容入口）
- Agent Runtime：`python -m agent_runtime`
- Graph Engine HTTP：`python -m graph_engine api`
- Graph Engine MCP：`python -m graph_engine mcp http`

## 快速开始

1. 复制 `.env.example` 为 `.env`，配置模型与 embedding 端点，并为四个服务设置同一随机 `IGRAPH_CONTROL_SERVICE_KEY`（完整部署必填）。
2. 使用 `docker compose up --build -d` 启动四个业务模块及 PostgreSQL。
3. 打开 `http://localhost:21108`。
