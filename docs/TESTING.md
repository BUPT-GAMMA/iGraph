# 本地测试

测试由开发者在本地按变更范围执行。
验证以后端功能正确性为重点，包括登录鉴权、用户数据隔离、会话与数据集操作、
流式响应、任务取消和恢复。前端修改检查 TypeScript 类型与 Vite 构建。

## GitHub Actions CI

推送到 `main` 时，CI 只做核心检查：Compose 配置与 Dockerfile 检查、前端构建，
以及排除耗时细节用例后的后端回归。被排除的是算法逐项结果校验
（`tests/servers/test_graph_data_app.py`、`tests/servers/test_large_graph_all_algorithms.py`）、
大图执行集成（`tests/test_large_graph_execution_integration.py`）、故障注入、隔离计算、
重启恢复，以及在共享 runner 上对时间敏感的 `test_workspace_bash_timeout_is_structured`。
需要完整回归时，在 Actions 页面手动运行 CI 并勾选 `full`。CI 不包含部署，
上线见[手动部署](MANUAL_DEPLOYMENT.md)。

## 后端回归

使用 Python 3.12 和 `requirements-ci.txt` 中保留的测试环境依赖。
需要验证 PostgreSQL 控制并发时，提供专用测试库的 `IGRAPH_CONTROL_TEST_DATABASE_URL`。

```bash
# 日常后端范围，四种真实链路取消场景各执行一次
IGRAPH_CANCEL_TEST_REPETITIONS=1 python -m pytest -q --durations=10 \
  --ignore=tests/test_isolated_compute.py \
  --ignore=tests/test_cancellation_failures.py \
  --ignore=tests/test_execution_restart.py

# 完整后端回归，包含故障注入、隔离计算、重启恢复和重复取消验证
IGRAPH_CANCEL_TEST_REPETITIONS=20 python -m pytest -q --durations=10
```

修改执行器、取消与恢复协议或数据归属逻辑后，应验证相应的故障、并发和恢复场景。
在 PR 中记录实际执行的命令、结果和未覆盖范围。

## 前端构建

```bash
cd frontend
pnpm build
```

现有 Vitest 和 Playwright 用例供需要时手动排查，不作为自动门禁。
`frontend/vite.config.ts` 是配置源，修改后通过 `pnpm build` 同步跟踪的 JS 编译产物。

## 覆盖率与配置检查

覆盖率用于本地分析，不设最低百分比门槛。可运行 `python -m pytest --cov --cov-report=html`，
前端可运行 `pnpm test:coverage`。`scripts/check_coverage.py` 保留为可选分析工具。

Compose 校验可在临时目录复制 `compose.yaml` 和示例 `.env` 后执行，避免依赖或
覆盖开发者已有配置。`--env-file` 负责变量插值，服务级 `env_file: .env` 仍需对应文件。
