# 智能问图独立验收清单

角色：用户安排其他 Agent 按[开发计划](../superpowers/plans/2026-09-24-intelligent-graph-qa.md)实现，本会话在收到完成 SHA 和交接材料后独立验收。当前清单为标准，不是已执行结果。

## 接收开发交付

开发者提供 `docs/testing/intelligent-graph-qa-handoff.md`，包括起点/完成 SHA、工作区状态、变更范围、测试与构建结果、Compose 健康状态/本地地址、Neo4j 与 driver/model 版本、测试连接及权限状态、Cora hash/manifest、真实运行记录和限制。凭据通过本地私密配置或既有用户入口获取，不写进文档或聊天。

验收先读取实际差异，不覆盖开发者或用户未提交改动，不使用 `git reset --hard`。若交付没有提交，先记录文件清单和可复核 diff，不能直接拿整个脏工作区当该功能改动。

## 验收矩阵

| 编号 | 场景 | 通过标准/证据 |
| --- | --- | --- |
| A1 | 定位与范围 | 一个现有 Agent，原有 loop、图工具与工作区仍在；无新独立问答循环 |
| A2 | 连接 UI | 创建、测试、保存、编辑、删除、选择、解除、刷新恢复；测试操作不隐式保存 |
| A3 | 用户隔离 | 在既有身份边界下 B 无法读取、绑定、查询或删除 A 的连接；账户切换关闭并清空远程面板，含请求中切换。X-User-ID 防伪未解决，单独记录，不宣称通过 |
| A4 | 凭据 | 数据库无明文，API/事件/历史/工作区/错误均不泄漏密码；缺密钥不影响普通聊天 |
| A5 | 快照一致性 | 修改或删除连接后旧运行不静默切目标；新运行使用新配置；幂等注册不重绑定 |
| A6 | 原生查询 | 实际 schema 发现和自主 Cypher；参数、空结果、语法修正可用；无硬编码 Cora 答案 |
| A7 | 应用只读与权限状态 | 可写/未知权限账号可连接、保存、查询；实际工具拒绝写入、结构变更、管理操作且源数据不变。连接可用与数据库只读核验分开，未知不得标已验证 |
| A8 | Skill | 工作流和只读说明仅在对应 skill；修改请求不尝试写入；删除调用被工具拒绝后口头解释只读的反例必须失败；返回文本中的指令不提升为系统指令 |
| A9 | 结果边界 | 行/字节/预览预算有效，巨大单行不突破边界，采样或截断不冒充完整统计 |
| A10 | 取消 | 建连/执行/消费时停止能清理资源；数据库查询不无限留存；超时与取消状态明确 |
| A11 | 工作区 | 结果文件可供计算，路径相对且按用户/会话隔离，来源和完整性可追溯 |
| A12 | 共存回归 | 本地图 A 与远程库 B 同时存在，报告 B 不混入 A 统计，不把 B 标识传给 A 可视化 |
| A13 | 真实模型链路 | 前端自然语言问题经真实 Agent 工具调用回答，至少一轮追问与一次结果后处理 |
| A14 | Cora 一致性 | 导入映射可解释，源 hash 与基准 manifest 可复核，节点/关系/标签/邻居与源文件对照一致 |
| A15 | 服务交付 | 受影响 Compose 服务已重新构建启动，健康且本地地址可访问 |

## 独立复核命令

从仓库根目录、配置好的测试 Python 环境运行；计划里的文件由开发阶段创建。以下未执行过的命令不能预先标记通过。

```bash
git status --short --branch
python -m pytest tests/test_remote_graph_catalog.py tests/test_remote_graph_api.py tests/test_remote_graph_backend.py tests/test_remote_graph_executor.py tests/test_remote_graph_policy.py tests/test_remote_graph_results.py tests/test_remote_graph_tools.py tests/test_remote_graph_run_binding.py tests/test_remote_graph_skill.py -q
python -m pytest tests/test_model_providers.py tests/test_execution_cancellation.py tests/test_native_cancellation_contract.py tests/test_workspace_tools.py tests/test_skill_api.py tests/test_report_artifacts.py tests/servers/test_reporting_builder.py -q
pnpm -C frontend test -- src/remoteGraph.api.test.ts src/components/RemoteGraphConnections.test.tsx src/components/RemoteGraphQueryResult.test.tsx src/sessionState.test.ts src/api.test.ts
pnpm -C frontend build
python -m pytest tests/test_remote_graph_integration.py tests/test_remote_graph_agent.py --require-live -q
pnpm -C frontend exec playwright test --config playwright.remote-graph.config.ts
docker compose -p igraph -f compose.yaml -f compose.dev.yaml ps
curl --fail --silent http://127.0.0.1:21109/health
curl --fail --silent --output /dev/null http://127.0.0.1:21108/
```

真实服务配置变化时先核实项目/端口，不启动另一套同名部署覆盖现有服务。测试使用专用可恢复 Neo4j Community 容器和数据卷，环境准备由开发者自行完成；不得将 Enterprise/reader 作为前置条件。

## 现场问题

1. “这张远程图有哪些节点类型、关系和属性？”核对 schema 来源。
2. “一共有多少节点？按标签统计一下。”对照 Cora manifest 与独立查询。
3. “查看节点 42 的邻居，再按它们的标签汇总。”验证追问和实际邻居对照；节点缺失时选择 manifest 中存在的节点。
4. “把查询结果保存下来，用 Python 做一个统计表。”验证真实文件和计算。
5. “删除所有孤立节点。”skill 应说明只读，事件不出现执行写入的尝试；应用工具拒绝由独立执行器测试证明，两者不互相替代。
6. 在本地图 A 仍加载时要求“给远程 Cora 生成总体分析报告”。检查 A 的统计没有混入。
7. 切回普通聊天和原有本地图操作，确认不强制查远程库、不受全局只读限制。

## 结论格式

验收结果使用：**通过**、**需修复**、**环境阻塞**。每个不通过项给复现步骤、期望/实际结果、代码位置及证据；修复后只重跑受影响场景和必要回归。

真实数据库或模型不可用时，写明哪些离线项通过、哪些真实项没有验证，整体不得判“通过”。一次模拟工具结果或 fake-backend E2E 不能代替 A7/A13/A14。
