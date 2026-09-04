# 断流恢复契约与排障

## 保证与边界

在 Agent Runtime 进程持续运行、网络最终恢复、已保存结果可读取的条件下，V2 在回放窗口内按原始事件序号补齐，窗口外通过完整阶段快照最终补齐。恢复操作只读取已有 Run；用户问题和审批决定均不会由恢复流程自动重发。

暂停接收、切换会话和关闭页面只停止观察。取消后台任务属于独立操作。本实现不提供进程重启后的任务续跑，也不引入 Redis、额外消息表或新的 Agent 框架。

## 依赖与部署约束

- 保留 `agno==2.9.0`。`agent_runtime/recovery.py` 是私有接口的唯一适配边界，使用仅配置既有 DB 的读取 Agent，复用原生订阅、回放、去重和释放逻辑。
- 固定 `eventsource-parser==4.1.0`（MIT）；前端 Node 最低版本为 `22.12.0`，CI 保持 Node 22。
- Agno 默认每个 Run 缓冲最近 10,000 个事件；结束或暂停的缓冲保留约 30 分钟。事件索引在裁剪后仍单调递增。
- 缓冲和后台任务属于运行进程。现有单运行进程部署保持不变；将来增加副本或 workers 时，需要连接亲和性或重新设计事件存储。
- 升级 Agno 时必须先运行 `tests/test_recovery_runtime.py`。契约测试覆盖真实 AgentFactory、确定性模型、原生后台执行、原始缓冲、公开后端续传和快照路径，禁止在续传中重新解析业务 Factory。

设计依据：[Agno 回放生成器](https://github.com/agno-agi/agno/blob/main/libs/agno/agno/os/routers/agents/router.py)、[eventsource-parser](https://github.com/rexxars/eventsource-parser)、[AG-UI 快照/增量分离](https://docs.ag-ui.com/concepts/state)、[LangGraph join/rejoin](https://docs.langchain.com/oss/javascript/langchain/frontend/join-rejoin)、[resumable-stream 结束通知兜底](https://github.com/vercel/resumable-stream/blob/main/src/runtime.ts)。复用范围为 Agno 和 SSE 解析器，其余用于设计参考。

## 接口

`POST /api/sessions/{session_id}/runs/{run_id}/resume?last_event_index=-1`

身份、会话归属及 Run 归属检查先于缓冲读取；关闭 AgentOS 用户隔离开关仍执行归属检查。原生生成器的数据库历史事件回放被禁用，因为这条路径会重新编号，无法作为原始增量。

建连前不可连续回放时返回 HTTP 409：

```json
{"detail":{"code":"REPLAY_UNAVAILABLE","reason":"window_trimmed","message":"需要同步已保存的运行结果"}}
```

原因包括 `buffer_missing`、`window_trimmed`、`invalid_cursor`。流已开始后的缺口或传输异常通过无业务游标的 `stream_recovery_required` 通知。它们不会被当作 Agent 执行失败。上游 HTTP 错误在下游 SSE 响应头发出前传递，保留 `Retry-After`。

`GET /api/sessions/{session_id}/runs/{run_id}/snapshot`

返回目标身份、目标 `status`、会话 `messages`、最新 `active_run`、`pending_feedback`、`snapshot_complete` 和 `resume_cursor`。消息重建复用既有 Run 记录。

完成、失败、取消或暂停的持久化阶段与运行时状态一致时，快照才标记完整。审批刚继续但 DB 暂时还保存旧暂停阶段时，快照保持不完整。

当前 Agno DB 记录无法证明消息与原始事件游标的原子对应关系，因此 `resume_cursor` 保守返回 `null`。使用这种快照恢复暂停阶段后，该 Run 保持快照模式；仍可手动审批，后续完整阶段通过轮询补齐。运行中的部分快照不会推进游标，也不会被标记为恢复成功。

审批转发增加 `background=true`，保留 `requires_confirmation` 和原始执行元数据，以及同一 Run 已完成的工具/报告记录。Agno 2.9.0 的工具续跑同时依赖 `requires_confirmation=true` 和 `confirmed`，且会替换收到的工具列表，这两项均由真实审批契约测试覆盖。明确的上游 HTTP 拒绝通过 `X-iGraph-Feedback-Accepted: false` 区分，可在重新核对后恢复手动提交；响应丢失期间保留幂等保护，不自动重复决定。

## 前端状态与调度

`lastAppliedEventIndex` 仅表示完整应用到消息状态的 V2 批次。消息、工具、计划与游标由同一 reducer 事务提交。重复批次整体忽略；不连续或不可应用的批次停止确认并进入快照模式。

执行状态与 `reception` 分离。`synchronized=false` 时限制新任务和审批提交；用户暂停只改变接收状态。异步回调检查用户、Run、运行代次、连接对象/代次及查询版本。

- 同页面断流：保留现有内容，从已提交游标重放。
- 刷新或消息内存丢失：从 `-1` 暂存重建当前 Run，追平后发布；保留其他历史消息。
- 用户暂停：取消流、控制查询和定时器；切回该会话可核对一次状态，不自动恢复持续接收。
- 超出窗口：保留现有内容，轮询到完整阶段再原子替换服务端消息；草稿、附件和界面偏好保留。
- 完成事件带完整字符串时覆盖对应助手消息，包括空字符串；没有文本字段时保留已有内容。稳定消息 ID 防止重复消息。
- 恢复旧 Run 时保留服务器报告的新活动 Run，待新 Run 恢复核实后再更新交互权限。

`RunRecovery` 统一拥有重连、快照轮询、结束状态检查和网络唤醒。`RunConnectionManager` 每会话最多保留一个有效流、一个控制查询和一个定时器，查询截止时间与流监测共享时钟。全局最多四个有效观察连接。

退避为 1、2、4、8、15、30 秒并带 ±20% 抖动。新应用事件或完整同步才重置失败计数；心跳和“仍在运行”的查询结果不重置。自动恢复网络错误、408、429、5xx，429 尊重 `Retry-After`。身份、权限或明确资源错误停止自动重试。

服务端空闲流每 30 秒发送心跳，且保留上游注释心跳。客户端 90 秒无传输活动重建观察连接；控制请求 15 秒超时。持续观察但 30 秒无业务进展时进行低频状态检查，发现丢失的完成/暂停通知。断开观察不会取消 Agent 的后台生产任务。

V1 保留线格式兼容，发生断流后走快照恢复，不承诺按 V1 单事件索引去重。

## 测试和发布

```sh
.venv/bin/python -m pytest -q
cd frontend
pnpm test:coverage
pnpm build
pnpm test:e2e
pnpm exec playwright test e2e/recovery.spec.ts --repeat-each=20
pnpm test:e2e:native
pnpm exec playwright test --config playwright.native.config.ts --repeat-each=20
```

恢复测试覆盖文本完整性、空文本/仅 done、UTF-8 字节拆分、未终止帧、重复/缺口、刷新暂存、暂停与切换、旧连接/旧快照、审批单次提交、存储失败、退避/超时/心跳、10,000 事件裁剪、权限和订阅释放。

`test:e2e:native` 是需要同时具备根目录 `.venv` 和前端依赖的联合验收命令，与现有单独运行 Python/前端的 CI 作业分开。它启动本机隔离测试服务（11119/15174），使用真实 AgentFactory、原生缓冲、SQLite、公开 BFF 和浏览器，仅替换模型及无关图服务；测试数据使用自动清理的临时目录。故障注入丢弃首个 token 后的响应，核验增量续传、审批后的快照收敛、问题和决定各提交一次、工具执行一次、Factory 未被恢复流程重建及订阅归零。

现有侧栏动画测试保留原断言，独立核查其偶发时序问题。代码先在 dev 验证，再将运行时、后端、前端作为同组版本发布；发布前让活动任务收敛，回滚整组版本。本修复不自动提交、推送或部署。

### 本地验证记录（2026-09-04）

- Python 全量：338 通过、6 跳过，另有 42 个子测试通过；行覆盖率 52.29%、分支覆盖率 35.48%，既有门槛校验通过。
- 前端：112 项单元/组件测试通过；行/语句覆盖率 66.38%；TypeScript 检查和生产构建通过。
- 浏览器：常规 E2E 16 项、真实 Factory 联合 E2E 2 项通过。常规恢复两场景各连续 20 次，真实链路两场景各连续 20 次，合计 80 次通过。
- 本地环境为 Python 3.14.3、Node 24.2.0；CI 配置继续使用 Python 3.12 / Node 22。本地结果不替代合并前该环境的 CI 验证。
- 保留先前未提交的 CI 规则调整。代码仍在工作分支，尚未进行 dev 集成、提交、推送或部署。

## 诊断

后端日志记录 `stream_recovery_required reason=...`、`stream_transport_error`、`stream_cleanup_error`，不记录输出正文或凭证。浏览器发出 `igraph:recovery` 自定义诊断事件，含 `kind`（retry/gap/synchronized）、重试次数、受限原因码或快照同步耗时，供现有监控接入；不包含会话/用户身份、正文和凭证。

排查顺序：确认后台 Run 是否仍存活 → 查看 409 原因和当前 `snapshot_complete` → 核对暂停标记与 `reception` → 检查请求间隔、心跳及订阅释放。持续出现缓冲缺失时检查进程重启、缓存保留期和请求是否落到其他运行进程。持续快照不完整时检查数据库写入和目标阶段状态，保持发送/审批限制以避免对过期状态操作。
