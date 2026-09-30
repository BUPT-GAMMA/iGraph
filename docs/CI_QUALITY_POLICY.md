# CI 测试策略

自动 CI 当前暂停，推送或合并到 `main`、`dev` 均不会触发工作流。
仅保留手动运行配置；仓库工作流已禁用，需要时先在 GitHub Actions 中重新启用。

## 日常检查

未勾选 `full` 的手动运行执行三个并行任务。
CI 以服务端功能正确性为重点，前端只检查能否编译构建：

| 任务 | 检查范围 |
| --- | --- |
| `Python tests` | 后端基础及业务回归、Graph ML、协议和断流恢复、PostgreSQL 控制并发、任务取消及进程退出；四种真实链路取消场景各执行一次；Docker Compose 配置校验 |
| `Frontend checks` | TypeScript 类型检查和 Vite 构建 |
| `Docker action compatibility` | 在 runner 本机临时仓库中验证 Docker 登录、构建、推送、拉取和缓存，并静态检查各服务 Dockerfile；不会访问外部镜像仓库 |

`Required checks` 要求三个任务均成功；后端测试断言、类型检查、构建和配置错误仍会阻断。
保留这些检查名称，兼容现有分支保护。流水线在汇总检查后结束，不发布 GHCR
镜像，也不连接或部署任何服务器。

日常 CI 不启用覆盖率采集、覆盖率硬门槛、Python 分片及覆盖率汇总，
Python 输出最慢的 10 项测试，便于定位耗时。
日常与完整模式均不运行 Vitest 或 Playwright，不安装浏览器；按钮、文案、动画和页面
交互测试不作为 CI 门禁。现有前端测试保留为需要时手动排查的工具。
登录鉴权、用户数据隔离、会话与数据集操作、流式响应及取消恢复应通过后端接口和真实服务链路测试验证。

## 手动完整回归

在 GitHub Actions 的 CI 页面选择 **Run workflow**，勾选 `full`：

- 运行全部 Python 测试，取消真实链路四种场景各重复 20 次，审批/取消竞争也重复 20 次。
- 包含日常暂不执行的三个较慢套件：
  - `tests/test_isolated_compute.py`：隔离计算、图版本与产物发布、跨模型训练推理等。
  - `tests/test_cancellation_failures.py`：数据库故障、请求丢失、辅助模型取消等故障注入。
  - `tests/test_execution_restart.py`：强制退出、重启、后代进程清理及提交恢复。
- 完整模式中任何后端测试失败均使本次运行失败；前端继续只检查编译构建。

测试文件与原有断言均保留。日常模式验证基本功能；上述故障场景、完整产物矩阵和
隔离计算与重启恢复场景仅在完整模式验证。修改执行器、取消/恢复协议或数据归属逻辑后，以及重要合并前，
建议对目标提交执行完整模式；当前流程不自动强制这一步。

## 覆盖率

覆盖率仅用于本地分析，不设 CI 最低百分比。统计范围和报告工具继续保留。

```bash
# 仓库根目录，使用 Python 3.12 和 requirements-ci.txt
python -m pytest -q --cov --cov-report=html --cov-report=json:/tmp/igraph-coverage.json

# frontend 目录
pnpm test:coverage
```

前端覆盖率来自 Vitest，未合并 Playwright 执行记录。
`scripts/check_coverage.py` 保留为可选分析工具，CI 不调用它。
`frontend/vite.config.ts` 是配置源，修改后通过 `pnpm build` 同步跟踪的 JS 编译产物。

## 本地回归

```bash
# 日常后端范围；提供专用 PostgreSQL 测试库 URL 可验证与 CI 相同的数据库锁语义
IGRAPH_CANCEL_TEST_REPETITIONS=1 python -m pytest -q --durations=10 \
  --ignore=tests/test_isolated_compute.py \
  --ignore=tests/test_cancellation_failures.py \
  --ignore=tests/test_execution_restart.py

# 完整后端范围；默认即为 20 次，也可显式指定
IGRAPH_CANCEL_TEST_REPETITIONS=20 python -m pytest -q --durations=10

# frontend 目录，CI 保留的检查
pnpm build
```

Compose 校验在临时目录复制 `compose.yaml` 和示例 `.env` 后执行，避免依赖或
覆盖开发者已有配置。`--env-file` 负责变量插值，服务级 `env_file: .env` 仍需对应文件。
