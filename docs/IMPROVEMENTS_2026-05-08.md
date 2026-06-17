# 改进

## 三个主问题

### ① SKILL/规划工具自动注入

`skill_view` 与 PlanStep 启动时，把 SKILL frontmatter 的 `requires_tools`/`any_tools` 与 `candidate_tools` 自动加入当前回合工具集，省去一次 `tool_select`。

### ② 同 batch 工具并行

多个 tool_call 用 `asyncio.create_task` 并发；含 high-risk 或 `request_user_feedback` 时强制串行。React loop 与 PlanSolver 双路打通。烟测 [0.4, 0.1, 0.2]s：串行 0.7s → 并行 0.4s。

### ③ compute 流式截断

- `find_cycles` / `find_cliques`：`islice(..., limit+1)`
- `link_prediction_score` 全图非边：`heapq.nlargest` + 单对评分（顺带修了 nx generator 的隐 bug）
- `transitive_closure`：`nx.descendants` 流式拼
- `topological_sort`：`islice` 预览

## 后续清理

- `GraphSession._project_root()` 多一层 `dirname`，cwd 切换后找不到 `uploads/`。
- 高风险工具可由 SKILL `risk_level: high` 的 `requires_tools` 扩展，无需改 Agent 代码。
- `MemoryEngine.refresh_snapshot()` 返回 `(changed, block)`，仅在 changed 时刷新 `_memory_prompt_block`，保 prefix-cache。解决 DESIGN §25.9。
- 3 处过期断言、`_FakeAgent.planning_mode`、MCP fake httpx 兼容真 httpx 0.28。

## 提交

`57ba629` → `0c47e76` → `785c046` → `407bc5b` → `049b093` → `8d0cd06`

## 测试

`pytest tests/ --ignore=tests/test_ml` → **86 passed / 0 failed**
