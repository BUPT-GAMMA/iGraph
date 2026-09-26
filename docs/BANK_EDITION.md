# 银行项目专用分支

`codex/bank-edition` 用于银行项目的股权分析需求，以 `dev` 为公共功能基线，独立接收银行适配和修复。本分支不表示通用产品升级到 v0.2；`dev` 和 `release/v0.1.0` 继续维护各自的主线及发布版本。

2026-09-27 首次集成以 `dev` 的 `09364d5` 为基线，合入 `feature/finance` 的 `835bfd1`。公共主线后续更新应在本分支合并并回归验证；银行定制修改如需进入公共产品，应另行审核选择适合的部分。

## 功能入口

现有数据集上传、构图、会话加载流程保持可用。金融工具读取当前授权会话中的机构、个人、持股关系三表，提供快照质检、第一大股东、控制链、集团树和经济权益分析。聊天工作流使用 `finance-ownership-workflow`；对应会话 API 位于 `/api/sessions/{session_id}/finance/`。本次没有增加独立银行业务页面。

接口、输入字段和规则含义见 [金融工作流实现说明](FINANCE_WORKFLOW_IMPLEMENTATION_2026-09-26.md)。该文档记录的服务器验收属于原功能提交；不能替代本分支的部署和验收记录。

## 本次兼容性修复

- Agent 精简镜像包含金融工具目录依赖，避免共享执行策略导入失败导致服务无法启动。

- 金融投影复用当前逻辑记录解析，兼容类型重命名、属性提取、属性移动及构图算法将桥接记录转为边的无损变换。注销停止规则、比例和主体信息均读取当前有效值；冲突存储返回错误，不从历史快照补造事实。
- 普通 FK、优化捷径和属性连接不会变成持股事实。节点记录的来源使用 `source_node_ids`；当前边记录使用 `source_edge_ids`，`source_count` 统计全部来源。来源 ID 保持完整。
- 控制链、集团树边和证明同时返回带类型的主体标识，机构与个人使用相同业务 ID 时仍能准确衔接后续分析和绘图。
- 大图 MCP 检查仍限制返回大小，但保留样本的完整元素 ID、端点和类型名，使后续编辑能够准确引用；有界样本不能替代完整图计算。

## 验证与边界

核心回归命令：

```sh
python -m pytest -q tests/finance tests/test_finance_api.py tests/test_finance_skill.py tests/test_finance_tool_contract.py tests/servers/test_finance_tool_integration.py tests/test_bounded_construction_results.py
cd frontend
pnpm test
pnpm build
pnpm test:e2e
```

本地仓库不包含银行原始三表或服务器已保存的验收图。真实数据测试分别需要 `IGRAPH_FINANCE_TEST_DATA` 与 `FINANCE_REAL_GRAPH_PATH` 指向用户提供的输入；未提供时相关用例跳过。合成数据测试和结构化接口验证不能替代银行真实数据验收。

默认采用 `equity_only`。权威 GP、法人和特殊主体规则需要由银行提供并版本化，缺失时保留候选及缺失状态。结构化计算通过也不代表模型生成的自然语言报告逐句正确，报告仍应核对工具状态、计数和证据。
