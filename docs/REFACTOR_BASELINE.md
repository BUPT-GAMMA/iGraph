# Agent Runtime 模块化重构基线

## 基线身份

- 分支：`origin/integration/igraph-all`
- 提交：`ef4a6d7c3108774e43129f801ab0e0eb9d6443f5`
- Python：项目现有 `.venv`（Python 3.14）
- 记录日期：2026-08-26

本文件记录重构前真实可运行行为。后续目录、接口和 Docker 改造不得通过修改本基线、减少测试、放宽断言或增加跳过来制造“兼容”。

## 原运行拓扑

```text
Browser / CLI
      ↓
API Gateway（进程内持有 IGraph）
      ↓
MCP Service
      ↓
GraphData Service
```

原入口必须保留：

```bash
python agent_server.py
python main.py
python -m backend.api_gateway
python -m mcp_service streamable-http
python -m graph_data_service
```

## 契约规模

- Backend HTTP 路由：39 条。
- GraphData HTTP 路由：17 条。
- MCP/GraphData 公开工具：55 项。
- Frontend 只调用 Backend 的 `/api/*` 与静态资源入口。
- Web 聊天继续使用原 SSE event dict，不增加第二套 Run/Event 协议。

主要用户路径包括：

- 会话创建、激活、历史恢复与删除；
- 普通聊天、流式聊天、重新生成与反馈续跑；
- 模型、推理模式和规划模式切换；
- 文本建图、文件上传、数据集加载、图清理与保存；
- 图算法、图可视化、分析报告与 GraphML；
- Memory、Skills 与 SFT 导出。

## 真实用户体验阻断样例

使用原 `.env`、DeepSeek V4 Pro 和原 Web 页面输入：

```text
请加载以下图:
A B 4
A C 2
B C 1
B D 5
C D 8
D E 2
E F 3
```

实际结果：

```text
tool_select
→ load_graph
→ get_graph_info
→ 节点数 6
→ 边数 7
```

页面显示正常图信息和边权表格，不显示 `tool_calls` 协议 JSON。该流程必须在原本地入口和 Docker 入口均通过，否则不得交付。

## 完整测试基线

执行：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
240 passed, 6 skipped, 42 subtests passed
1093 warnings
192.65s
```

6 个跳过项和警告均为原版本既有环境/依赖条件。后续不得产生新增失败或新增跳过。

## 关键文件 SHA-256

```text
58ea01ab5f11b6893ed71a01dcf0506736877c333fb2ea955d3bef4c731d98a4  index.html
ffdbe02dfcb174692a84350ec8d6942624b983c6e3c19190740fc18245ccd2da  backend/api_gateway/app.py
5ee2955ca38cce4dd1be3c56d50f2a2d0fb71458adef8960c94fc5185c50c455  backend/agent/igraph.py
c37f5ae00e1ccd0b9be1bd67205a6e189500620b6f25a3dd3ea9a45352d47793  backend/agent/context/context_manager.py
bc7f6eb67ed7e30e37cc3fbfb9a4ce81b3f4c74a999149a9460fbef6113b8508  backend/agent/context/text_protocol_driver.py
1c07c609c5b506343f047d367e969392aea4b66c868b5793280300e8286a1a14  backend/agent/provider_gateway.py
7a486771329f52c898d9616202c7d98401be6ff79065e16348cef1624bc8b9c8  mcp_service/server.py
895b9380439cfa9ecd54bd00d673632697cbeb65b817960f5b7dadf7c32d6bed  graph_data_service/app.py
5a4fab5cec043d08afe67a9d0110e2b4ed9d1ae875ef9461887957000480dad9  graph_data_service/tool_catalog.py
```

Agent 文件迁移后允许因 import 路径发生哈希变化；提示词、协议文本、模型请求、工具循环和数据处理语句不得发生行为性改写。`index.html`、MCP 与 GraphData 业务实现应保持内容一致，除非容器资源路径需要最小兼容适配。
