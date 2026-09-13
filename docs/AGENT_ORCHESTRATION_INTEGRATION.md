# Agent orchestration 功能移植记录

本分支以 dev 为基底，按功能移植 agent-orchestration-optimization 的增量。原 dev 工作区不变；没有把源分支的历史删除、图工具替换或整份 UI 重构直接覆盖到 dev。

## 比较基准

- 基底：dev / origin/dev，`244f82cf24bb3ffc3a9237b366bd95032739099c`。
- 来源：origin/agent-orchestration-optimization，`d0869367b766828c6b2a8f77b8e11ba8991b9a55`。
- 公共祖先：`42869c65ce221c67023d64e48e34c80f363e345e`。
- 集成分支：`codex/integrate-agent-orchestration`。
- 核对日期：2026-09-13。最后一次 fetch 后两条远程分支仍是以上提交。
- 两边分别有 22 / 124 个独有提交。此前机械试合并出现 31 个冲突文件、70 个冲突块；本次采用功能移植，源分支不成为 Git 合并父提交。

## 功能保留与适配

| 功能 | 本次实现 | 与 dev 的兼容方式 |
| --- | --- | --- |
| 图分析、构图、训练/推理 | 保留 dev 全部 61 个 MCP 图工具 | 保留动态工具目录、DAG 资源排序、构图权限头、后台作业协议 |
| 工作区 | 新增 workspace_list / workspace_bash / workspace_publish | 标准库共享存储；按用户和会话隔离 Uploaded、Ephemeral、Derived |
| 上传与产物 | 上传副本、文件列表、版本化发布、认证下载 | SQL/CSV/TXT 仍进入 dev 数据集注册；同内容不同格式使用独立 token |
| Shell 执行 | bubblewrap 隔离，限制耗时和输出，取消时清理进程组 | 沙箱不可用时拒绝执行；不影响原有图工具 |
| 循环保护 | 每 Run 最多 32 次调用、重复参数/无进展检测、连续失败检测 | 在 dev 原生 hook/DAG 中共享计数；活跃作业轮询按进展处理；停止写入真实 error 终态 |
| 持久化与过程面板 | 保存内容事件、工具批次 checkpoint、历史/实时过程投影 | 保留 dev SSE 游标、快照、审批、取消和任务进度；不重写对话主记录 |
| Agent 状态 | update_agent_notes，只允许写 agent_notes 字段，限制 8KB | 禁止覆盖 project_id、data_state 等受控会话字段 |
| 上下文优化 | 可选工具结果压缩、会话摘要 | 默认关闭，保持 dev 历史上下文与 auto 规划 |
| Skill 管理 | 完整内容查看、ZIP 导入、审批/拒绝入口 | 沿用 dev 身份校验和候选队列；校验路径、条目数量和解压大小 |
| 对话操作 | 复制、编辑填回输入框、附件名称、拖放上传 | 复用 dev 上传接口、账户绑定和发送门禁 |
| 重新生成 | 校验最新 Run，保存显式 regenerate_of 关联 | 保留完整上下文、原回答和普通重复提问；有未发送草稿时禁用 |
| 请求日志 | JSON 请求开始/结束日志、关联 ID、递归敏感字段脱敏、可选轮转文件 | 使用纯 ASGI 中间件，保留流式响应和取消上下文 |
| Docker | Runtime 加入 bubblewrap/bash；两镜像带上共享模块 | Backend 不依赖 Agno；保留现有多服务部署和非 root 用户 |

## 修复的集成问题

1. **审批被 checkpoint 覆盖。** Agno 2.9 的工具批次 checkpoint 会把等待审批状态改为 running，继而可能保存为 completed。DAG 模型在暂停批次跳过该 checkpoint，交由原生暂停流程保存；正常批次继续保存。已用原 dev 对照、原生持久化断言和真实 MCP 审批取消验证。
2. **取消时子进程逃逸。** 子进程创建期间收到取消，原实现可能尚未拿到进程对象。现在先拥有创建结果，再终止整个进程组；30 秒等待场景在回归中须在 3 秒内退出。
3. **MCP 失败未计入循环保护。** 同时解析 Agno ToolResult、JSON 和业务 success/status，避免把传输成功当成业务成功。
4. **运行上下文副本导致计数丢失。** Guard 存在 RunScope 上，并在 DAG 复制上下文前初始化；原生 RunError 和数据库 error 状态一致。
5. **下载和路径边界。** 下载使用 dev 认证请求，检查当前账户；服务端限制在当前会话 Derived 内，拒绝穿越和符号链接。
6. **上传破坏数据集注册。** 保留 dev 图服务 ingest 与项目数据集注册，不采用源分支的内容去重跳过路径。
7. **历史及进度兼容。** 缺失/非法事件编号使用顺序回退；新增元数据允许缺省；训练轮次和跨 Run 后台作业状态继续使用 dev 的权威状态。
8. **旧后台训练测试夹具失效。** 原始 dev 的相同浏览器用例也失败。当前 dev 会等待作业完成，而旧用例期待长训练立即返回，且仍定位旧的 ML 标签。现只对显式标记的测试会话模拟旧版本的异步生产者，真实 MCP、worker、控制记录和取消路径仍参与测试；定位更新为当前“机器学习”，原有断言与取消时限不变。
9. **快照清空历史训练卡。** 部分原生记录的 tools 摘要为空，但保存的 ToolCall 事件完整。BFF 现在在摘要缺失时从事件恢复原有工具卡，再经过 dev 的后台作业更新逻辑，避免下一轮加载快照时取消入口消失。真实浏览器已验证停止旧 worker 时，新模型流不受影响。
10. **受控状态和失败展示。** 仅允许 Agent 写备注；失败/取消工具不再显示为运行中，结构化错误显示 message。

## 已验证范围

测试日志位于本工作区的 `output/integration-validation/`，该目录按仓库约定不提交。测试模型使用确定性替身，不消耗真实模型额度。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| Python 日常回归 | 1024 passed，11 skipped；514 subtests passed；84.59 秒 | python-daily-final.log |
| 独立计算、取消故障、重启恢复 | 39 passed；97.83 秒 | python-resilience.log |
| 最终复核相关 Python 用例 | 77 passed，1 skipped；含新增历史兼容断言 | final-review-tests.log |
| PostgreSQL 16 并发、持久化、真实取消 | 21 passed；夹具最终默认路径另有 4 passed | postgres-check.log / postgres-fixture-final.log |
| 历史工具卡最终兼容修复 | 44 passed；历史后台训练浏览器定向 1 passed | event-history-final.log / browser-historical-training-final.log |
| 前端单元/组件 | 238 passed，24 个文件 | frontend-tests.log |
| TypeScript / Vite 生产构建 | 通过 | frontend-build.log |
| 普通浏览器 E2E | 23 passed | browser-e2e.log |
| 真实 AgentOS / DB / BFF 恢复浏览器用例 | 2 passed | browser-native.log |
| 真实 MCP / worker 取消浏览器用例 | 7 passed；43.0 秒，含历史训练停止/新 Run 隔离 | browser-cancellation-final.log |
| Backend / Runtime Docker 构建 | 均通过 | *-image-build.log |
| 最终镜像导入检查 | 均通过；Backend 无 Agno | *-image-smoke.log |
| Linux 沙箱实测 | 正确拒绝执行；当前 Docker 内核不允许创建命名空间 | linux-workspace-smoke.log |
| 补充检查 | git diff --check 通过；新任务页面已做浏览器截图检查 | ../playwright/integration-composer.png |

本地 Python 为 3.14.3，Agno 2.9.0，日常数据库回归使用 SQLite，补充检查使用临时 PostgreSQL 16；最终本地 Docker 镜像为 Python 3.12 / linux/arm64；未做生产 linux/amd64 全栈启动验收。修改后的原有业务断言均保留。Skill 详情测试补充新增的请求响应 fixture；旧后台训练浏览器夹具的调整见上文。

## 运行和发布条件

Backend 与 Runtime 必须共享同一工作区存储。默认位置为 `IGRAPH_DATA_ROOT/workspaces/<user>/<session>`；跨容器部署可显式把 `IGRAPH_WORKSPACE_ROOT` 指向同一个持久卷。仅设置相同路径字符串但未共享卷，无法互相读取上传和产物。

Shell 默认要求可运行的 bubblewrap。Runtime 镜像安装了二进制，但宿主机及容器策略仍必须允许 rootless namespace。目标环境部署前，以实际服务用户执行：

~~~bash
python -c 'from agent_runtime.workspace_tools import workspace_sandbox_startup_check; workspace_sandbox_startup_check()'
~~~

当前本机 Docker 在该检查返回 `No permissions to create a new namespace`。没有扩大容器权限，也没有静默切到宿主机执行。原有图工具、文件列表和已发布产物下载仍可用；不能将此结果写成“Linux 隔离执行已通过”。

可配置项见 `.env.example`：

- `IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS`：默认 30 秒。
- `IGRAPH_WORKSPACE_OUTPUT_LIMIT`：默认 100000；执行过程中有界保留输出。
- `IGRAPH_MAX_UPLOAD_BYTES`：默认 50MB；Skill ZIP 另限 2MB、解压 10MB、256 条目。
- `IGRAPH_COMPRESS_TOOL_RESULTS`、`IGRAPH_SESSION_SUMMARIES`：设为 true 才启用；会增加模型请求，需按实际供应商验证。
- `IGRAPH_RUNTIME_LOG_DIR`：可选轮转文件目录；默认输出到标准错误日志流。
- `IGRAPH_WORKSPACE_SANDBOX_MODE=fallback`：仅供显式的本地可信代码开发。它不提供操作系统隔离，不能用于不可信上传代码或生产环境。

正式并入 dev / 发布前，应对目标提交运行 CI 完整模式，包括 Python 3.12、PostgreSQL 17、取消竞争 20 次重复，并在目标 Linux 环境完成隔离执行验收和真实模型业务验收。本次测试证明所列场景，不能保证所有环境下零 bug。

## 暂未采纳的源分支行为

- 源分支的 22 个原生 HTTP 图工具不能覆盖 dev 的 61 个 MCP 工具，且缺少 dev 的部分权限/执行字段，故保留 MCP。后续若优化传输，应共享业务契约后再替换。
- 没有按消息文本相等去重，没有为重生成清空历史，也没有折叠回答版本选择器；显式 Run 关联保留了后续扩展空间。
- 没有开放整个 session_state 给模型随意改写。
- 没有用 XHR 上传替换 dev 的账户绑定与数据集返回契约；上传百分比进度条未移植。
- 没有整体覆盖布局、字体、模型图标和图工作台；新增组件使用现有布局。
- 没有移入大批历史文件删除。工作区文件目前在删除会话后保留，原会话鉴权会阻止继续访问；尚未实现保留期限、自动归档或垃圾回收，应纳入容量管理。
- 保留 dev 审批、取消、会话/项目删除的执行停止屏障，不在删除接口中无条件递归清理工作区。

## 审查与回退

这是 dev 之上的功能移植提交，不应再把源分支 124 个提交整批 cherry-pick 一遍。审查时以 `244f82c..codex/integrate-agent-orchestration` 的最终差异为准，后续将源分支的新功能按同样的契约逐项迁入。

提交按运行时/API（483c35f）、前端（48d9dba）、验收文档分组。若最终经 PR squash 合入，可整体 revert 该 squash 提交；若保留分组提交，按文档、前端、运行时/API 的逆序回退。无需修改已有会话数据库结构。回退代码不会自动删除新增工作区文件。
