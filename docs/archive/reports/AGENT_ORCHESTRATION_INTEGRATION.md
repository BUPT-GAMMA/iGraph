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
- 保留完整历史，不按消息文本相等去重，不为重生成清空上下文；已在后续 UX 补齐中基于显式 Run 关联加入回答版本选择器。
- 没有开放整个 session_state 给模型随意改写。
- 上传百分比已在后续 UX 补齐中移植；XHR 适配继续保留 dev 的账户绑定、数据集返回和执行门禁。
- 没有整体覆盖布局、字体、模型图标和图工作台；新增组件使用现有布局。
- 没有移入大批历史文件删除。工作区文件目前在删除会话后保留，原会话鉴权会阻止继续访问；尚未实现保留期限、自动归档或垃圾回收，应纳入容量管理。
- 保留 dev 审批、取消、会话/项目删除的执行停止屏障，不在删除接口中无条件递归清理工作区。

## 审查与回退

这是 dev 之上的功能移植提交，不应再把源分支 124 个提交整批 cherry-pick 一遍。审查时以 `244f82c..codex/integrate-agent-orchestration` 的最终差异为准，后续将源分支的新功能按同样的契约逐项迁入。

提交按运行时/API（483c35f）、前端（48d9dba）、验收文档分组。若最终经 PR squash 合入，可整体 revert 该 squash 提交；若保留分组提交，按文档、前端、运行时/API 的逆序回退。无需修改已有会话数据库结构。回退代码不会自动删除新增工作区文件。

## 用户体验补齐复核（2026-09-13）

在集成基点 `67db5f8` 上重新对照上述两个原分支，补齐可沿用 dev 契约的交互。此次生产代码变更全部位于 frontend，无数据库迁移。

| 遗漏项 | 现在的行为 | 兼容边界 |
| --- | --- | --- |
| 上传百分比、服务器确认 | XHR 展示真实传输百分比；100% 后显示“正在确认”，收到成功响应及有效 token 才可发送 | 绑定发起账户，保留 dataset、construction_required 和结构化错误 |
| 选文件立即上传 | 已有会话立即上传；新任务选文件后先创建会话并上传，点击发送才运行 Agent | 保留 dev 数据集登记/构图入口；发送复用 token，不重复上传 |
| 上传重试与逐附件取消 | 当前页面保留 File，可重试失败项；移除进行中的附件会中止其请求 | 账户切换、删除及卸载会取消相关请求；晚到响应不能回填旧附件；浏览器取消不承诺回滚服务器已经完成的登记 |
| 普通会话草稿恢复 | 按账户、会话保存输入文字与已上传附件；发送后清理已提交内容 | 未完成上传刷新后提示重选；沿用新建草稿及首轮恢复，不保存 File 本体；存储失败不阻断输入 |
| 中文输入法回车保护 | 候选确认时不发送，正常 Enter 发送仍可用 | 同时检查 composition 状态、原生事件和兼容键码；执行中保留停止按钮 |
| 会话置顶 | 独立任务或项目内会话可置顶、取消置顶，刷新保留 | 偏好按账户隔离，在原分组内排序，支持其他标签页同步 |
| 编辑消息附带原附件 | 原文字与附件一起填回输入框，可修改后再次发送 | 保留不可变历史，执行/上传中禁用编辑 |
| 回答版本切换 | 对显式 regenerate_of 链提供前后切换；正文、过程、工具和复制内容同步切换 | 保留所有历史记录，只允许最新已结束 Run 重新生成；相同文本不自动合并 |
| 技能分页和布局 | 每页 9 项，搜索覆盖全部技能，桌面双列、窄屏单列 | 保留导入、详情及审批；侧栏切换入口同步当前标签，过期请求不能覆盖新标签 |
| 附件展示 | 文件类型图标、扩展名、正文前附件和输入框外附件区 | 保留 dev 图工作台、数据集入口、模型选择和原布局 |

另修复了历史加载的实际遗漏：`normalizeMessages` 原先丢弃 attachments、processItems、thinking、regenerate_of，导致刷新后附件编辑、过程展示和版本关联失效。新增应用回归验证历史元数据保留。草稿持久化使用内容指纹，避免每个流式 token 都重复写入相同草稿。

### 本轮验证

日志位于 `output/branch-merge-audit-2026-09-13/`；测试模型为确定性替身。下表是此次前端补齐后的结果，前文 Python 大套件记录属于上一轮集成验证，本轮没有重复运行全部 Python 用例。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 前端单元/组件/应用 | 261 passed，27 个文件，新增 23 项 | ux-frontend-final.log |
| 普通浏览器回归 | 23 passed | ux-browser-final.log |
| 原生 AgentOS / DB / BFF 恢复 | 2 passed | ux-native.log |
| 真实 MCP / worker 取消 | 7 passed | ux-cancellation.log |
| TypeScript / Vite 生产构建 | 通过 | ux-build.log |
| 前端 Docker 镜像构建 | 通过 | ux-frontend-image-build.log |
| 补充检查 | git diff --check 通过；桌面/390px 技能与附件截图；刷新恢复文字与附件，无横向溢出 | ../playwright/ux-*.png |

普通浏览器的账户双标签用例按“选择文件立即创建并上传”的新时序更新：先验证 A 的创建和上传使用 A 身份，再验证切换后 B 的创建、上传、发送全部使用 B 身份且不含 A 内容；切回 A 恢复 A 草稿及附件。原有“storage 事件到达前点击不能错用新账户发请求”的零写入断言保留。

视觉检查使用临时 Vite、示例 HTTP 后端和浏览器路由数据；确认传输百分比、100% 等待服务器、重试和取消由自动化回归验证。视觉服务初始缺失 capabilities/custom-model/favicon 路由出现预期 404，补齐相关示例 API 后刷新正常，未发现应用 JavaScript 异常。截图不能替代真实模型长会话与实际网络故障验收。

本地预览仅更新 frontend 容器，地址为 http://localhost:21108，其他服务和数据卷继续使用原集成版。此补丁可作为独立提交回退；新增置顶和草稿数据仅在浏览器按账户保存。

### 仍需独立适配的差异

跨项目移动、General 自动迁移及删除项目后保留会话涉及 dev 的项目数据和执行身份，未直接移植。任意历史 Run 重新生成及重写旧记录仍未开放。消息所属模型和准确思考耗时缺少可靠历史字段，未以当前模型或挂载时间代填。

原生 HTTP 工具替换、工具参数容错封装、摘要/压缩默认开启、包含 MCP 的开发启动器和诊断脚本，仍需分别验证运行语义或部署依赖。循环保护误判随后已修复（见下节）；当前 Docker 沙箱不可用仍未解决；已有审计中的上传磁盘失败一致性风险也未进行故障注入。此次回归通过不能解释为全部分支功能已等价保留或整个项目零 bug。

## 循环保护误判修复（2026-09-13）

修复之前，同一工具和参数在一个 Run 内累计超过 3 次便被拦截，即使每次查询都观察到新的图版本。现在按同一调用的可观察结果重置重复计数；成功且有进展的写操作按 dev 的共享资源契约使相关只读查询的旧计数失效。因此图已改变但节点/边数量相同，也能重新检查。构图资源按 dataset_id 匹配，其他数据集或无关工具不能清除该查询的重复计数。

计数重置需要成功且有可观察进展，单纯改变参数、重复返回相同结果或仅把任务排入队列不会触发资源失效。各调用分别比较自身结果，避免两个不同查询交替执行就绕过重复保护。总调用上限 32、连续失败限制、活动作业轮询及终态轮询保护保留。并发晚到结果不能清除已经触发的停止原因。

新增回归先在旧实现上复现 5 项失败，然后验证修复：

- 相关 Python 回归：107 passed，365 subtests passed；覆盖循环保护、共享资源策略、DAG、原生 AgentOS 持久化、构图 hook 和取消。
- 真正无进展的原生模型循环仍保存 error；有进展的重复调用能够完成并保存 completed。
- 真实 MCP / worker 浏览器取消：7 passed。
- Runtime Docker 构建通过；实际 Python 3.12 镜像内连续 6 轮修改后检查通过。
- 本地更新前确认未结束执行数量为 0，只重建 agent-runtime 容器，保留其余服务与数据卷。

日志位于 `output/branch-merge-audit-2026-09-13/guard-*.log`。这解决了先前已复现的循环误判；进展判断仍以工具返回结果和已登记资源契约为依据，总调用预算仍可能限制非常长的任务。沙箱环境限制和其他发布验收事项不因本次修复而自动消失。
