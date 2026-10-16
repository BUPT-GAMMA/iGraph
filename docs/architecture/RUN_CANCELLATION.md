# 当前任务终止：实现、验收与发布门禁

## 实现状态

启动身份、持久化取消意图、Agent/MCP 取消传播、独占计算进程、前端停止及结果恢复主链路已落地。代码在工作分支维护，默认能力开关关闭，尚未部署。

本轮补齐了原生保存重试、服务退出证据、历史作业恢复、图结果短提交、图分类推理兼容及普通快照控制同步。Run 所属的 undo/load/discard 版本操作与数据集导出也纳入 worker。支持范围为同一部署主机上的 POSIX/local；NebulaGraph 能力继续关闭。生产启用前仍需目标 CI/dev、大图和 GPU 环境验收。

## 用户行为

- 右下角方块表示“终止当前任务”，无需确认。等待审批和提交审批时也可停止。
- 范围为可信用户、会话、Run，以及其子作业和后代作业。新作业、模型重试和审批继续受同一取消边界限制。
- 普通断网、刷新和会话切换继续使用断流恢复，保留后台执行。
- 停止期间继续接收收尾输出。`RunCancelled → RunCompleted` 可以补齐文本，取消终态保持不变。
- 部分文本、已提交图版本、已发布模型及草稿保留。原生 Run 尚未创建时取消，将输入恢复为草稿，不生成助手回答。
- Agent 正常完成后，历史训练可继续运行，下一轮聊天可正常开始；ML 作业卡片支持单独停止。

前端分别管理执行/取消、接收和内容同步，展示“正在停止…”、“停止请求尚未确认”、“已停止，正在保存结果”和“任务已结束”。执行退出、终态保存和最新准入全部核实后解锁发送；旧 Run 的快照补齐独立进行。

## 结构与复用

| 边界 | 实现 | 职责 |
| --- | --- | --- |
| BFF | `backend/app.py` | 身份校验、准备登记、稳定启动键、取消转发和快照 |
| 控制账本 | `agent_runtime/execution_store.py` | PostgreSQL 短事务、会话锁、幂等、代次和确认摘要 |
| Runtime 协调 | `execution_control.py` | 平行投递、实际退出核实、后台重试与启动扫描 |
| Agno 适配 | `cancellation_compat.py`、`execution_scope.py`、`native_persistence.py` | 原生收尾、待确认写入重试、请求级取消及审批终止 |
| 执行者寿命 | `execution_lifecycle.py` | 内核文件锁、原子保存及同步线程的实际退出 |
| MCP 适配 | `mcp_compat.py` | 复用原生工具描述，同 Task 打开/关闭传输上下文 |
| Graph 执行 | `execution_host.py`、`job_manager.py` | 先登记后排队、归属级联、执行者确认 |
| 独占计算 | `worker_process.py`、`compute_worker.py`、`isolated_compute.py` | 白名单进程、身份核对、暂存和提交 |
| 前端 | `RunCancellation.ts`、`RunConnectionManager.ts`、`sessionState.ts` | 稳定取消 ID、单一查询/时钟、刷新和代次隔离 |

直接复用 Agno **2.9.0**、AnyIO **4.14.1**，进程身份辅助使用 psutil **7.2.2**。OpenCode/Cline 的所有权和代次隔离、AutoGen 的关联取消、LangGraph 的订阅与执行分离用于设计参考，没有引入它们的运行框架。Agno 私有取消收尾接口集中在兼容模块，升级必须先运行版本契约测试。

MCP 的 AnyIO 上下文须由同一 Task 关闭。工具发现和实际调用各自拥有词法传输上下文；实际调用沿用服务端 operation ID，传输重试不产生新的业务操作。

## 协议与持久化

chat / chat/stream 增加可选 UUID `start_request_id`。新版前端在第一次异步发送操作前生成，同次发送复用，旧客户端由 BFF 补生成。数据库以 `(user_id, session_id, start_request_id)` 去重；相同键不同内容返回 `START_IDEMPOTENCY_CONFLICT`。只有一个调用方取得准备和执行权。

提前取消留下持久记录，迟到启动返回 `RUN_CANCELLED`。未知启动键查询返回 `not_seen`，客户端继续核实，恢复流程不重发问题或审批决定。

| 公共 API | 内容 |
| --- | --- |
| `POST /api/sessions/{sid}/runs/cancel` | cancel_request_id + target，run_id/start_request_id 二选一 |
| `GET /api/sessions/{sid}/runs/control` | 按 run_id/start_request_id 二选一查询 |
| `POST /api/sessions/{sid}/jobs/{jid}/cancel` | 稳定 cancel_request_id |
| `GET /api/sessions/{sid}/jobs/{jid}/control` | 作业及后代的退出、保存摘要 |
| `GET /api/sessions/{sid}/runs/{rid}/snapshot` | control 摘要及每条消息的 run_id |
| `GET /api/capabilities` | run_cancel_v1 / job_cancel_v1 |

控制摘要含 registration_status、run_status、cancel_status、execution_stopped、terminal_persisted、pending_components、revision、active_run、can_send。native_run_created 额外区分原生 Run 尚未建立的情况。聚合 revision 包含作业更新，旧版本不能覆盖新状态。

202 表示意图已记录且仍在停止；200 表示执行退出已确认，保存状态仍须检查独立字段。明确的身份/资源/参数错误停止自动重试，冲突使用 409。网络错误、408、429、5xx 用相同取消 ID 重试，429 保留 Retry-After。

新增 PostgreSQL 表 `igraph_execution_controls` 保存归属、父子关系、幂等键、执行代次/实例、版本和确认摘要，不保存正文、token、batch 进度或模型文件。唯一约束和事务级 advisory lock 保护准入；事务内不等待模型、网络或进程退出。删除会话留下封闭准入记录，阻止迟到启动。

启用控制能力时，`DELETE /api/sessions/{sid}` 原子保存会话删除意图、关闭新增执行入口，并向该会话所有仍有执行的 Run（包含历史后台作业）发出取消。首次受理返回 `202`，前端随即移除会话；列表及历史读取也立即隐藏该会话。Runtime 复用取消协调器，确认执行退出与终态保存后依次清理图资源和原生会话，读回确认删除结果。重复请求幂等，完成后返回 `200`；清理失败或服务重启后由后台扫描继续处理。封闭记录保留，阻止迟到启动和同 ID 的会话重建。其他会话不受影响，项目已发布产物沿用现有保留规则。

内部控制校验 `X-iGraph-Service-Key`，显式验证用户、会话、Run、Job 归属。关闭 AgentOS 身份隔离开关仍执行检查。原生启动/继续/删除入口受同一控制约束，模型参数不能覆盖归属。

## 故障收敛

- BFF 从登记到原生启动由独立 Task 持有，浏览器断开只关闭观察。准备实例 ID 与启动键保留本实例的退出证据，覆盖登记成功但响应丢失。
- Graph 从登记到入队由独立 Task 持有，响应丢失后重试同一个 operation ID。执行前再次核对持久取消，避免无人确认的 queued 记录。
- Runtime 启动扫描未完成的 Run/Job 取消，继续投递和核实。缺少执行者且没有证据时保留 pending，不自动重跑任务。
- 数据库临时失败时，对已有可信归属的活跃执行尽力停止，返回 `CANCEL_NOT_CONFIRMED`，继续核实/保存。
- 稳定审批阶段通过只读 Run 加载和终态更新取消，不重新构造业务 Agent。取消后历史审批不能重新获得操作权限。

## 进程、数据与结果

已隔离的路径包括训练、推理、图分类数据集加载/样本导出、NetworkX、图文本/文件/三元组加载、local 数据集加载、图转换/子图/搜索/可视化，以及文档入库/检索。白名单位于 `worker_contract.py` 和固定 worker 入口。

输入为固定版本图文件、已登记上传文件和已验证的数据集身份，不共享会话对象、锁或数据库连接。图和数据集版本以只读文件描述符固定，即使保留策略删除路径，所属 worker 仍可读到相同版本；数据和模型目录可位于不同文件系统。文档暂存位于会话目录，模型及数据集暂存位于产物目录，发布时的原子重命名保持在目标文件系统内。worker 启动目录绑定已安装的服务代码，服务从其他目录启动也可正确执行。

停止时立即协作取消，2 秒仍未退出发送 SIGTERM，再等 1 秒发送 SIGKILL，再用最多 2 秒核实。后代进程纳入专属进程组；操作权限来自受管理句柄及创建时间核对。身份异常、未知成员和超时保留未确认与资源额度，不根据超时宣称停止。约 5 秒目标适用于正常 OS 下的独占 worker，不涵盖供应商计费和异常文件系统。

模型先写 `.staging/<job-id>-…`；worker 完整读回 checkpoint、meta/metrics 和数组，保留各任务原有安全读取契约，并同步文件后原子发布。半成品不进入模型列表。图分类包使用逐图安全张量文件，继续要求显式加载。文档结果使用不可变目录与原子 manifest，一次发布索引、向量和原文；词法向量按执行进程重建，避免 hash 种子导致跨进程失配。

提交前核对取消、执行代次、图版本和数据集绑定代次。图的转换、校验和 NPZ 序列化在 worker 内完成；父进程原子替换 `graph_versions.json`，随后切换内存引用。新格式以 manifest 授予版本可见性，孤立候选文件不会成为正式版本。原 NPZ 协议继续可读，旧工具按需物化图。

图、模型、数据集和文档发布保留带 Run/Job/代次的提交凭据。凭据索引本身不证明发布；恢复还须读取正式 manifest 或正式目录中的匹配凭据。服务在发布与作业确认之间退出时，已提交结果仍可随取消/失败作业恢复。清理仅针对目标暂存资源，失败告警并受控重试。


## 保存与重启恢复的证据

- `native_run_created` 单调保持，原生 payload 缺失不会被解释成未启动或已保存。原生 phase 写入在调用 Agno 数据库前留存待确认副本；SQLite/PG 都执行读回核对。抛错或静默失败均保持 `terminal_persisted=false`，成功确认账本后才删除副本。等待审批的取消会撤销旧暂停 phase 的保存确认。
- 待确认副本位于 `agent_runtime/pending-writes`，仅保存未确认的原生写入，确认后清理。目录与控制数据必须一起持久挂载；这些文件含原生会话内容，权限与数据库备份一致。
- Runtime、BFF、Graph 使用独立 UUID 的内核锁文件表示执行者寿命。已登记文件可加锁表示原执行者已退出；文件缺失、损坏或不可读保持 unknown。worker 在 spawn 前创建并继承寿命锁，父进程与固定 worker 都在业务输入放行前保存进程身份。
- Graph 启动扫描持久账本，核对旧服务退出及 worker/后代创建时间，再继续停止和确认；worker 控制管道 EOF 触发本进程组退出。作业结果先保存在现有数据卷，再确认终态。不会自动重跑业务任务。
- BFF 原生交接失败与 Runtime 取得执行权使用同一账本边界；尚未取得执行权的迟到启动被拒绝，已经取得执行权的 Run 继续由其执行者协调。恢复不重发用户问题。
- 同步数据库操作和短提交在线程中执行时，取消会等待该线程真实退出。服务寿命锁在其操作结束后释放，避免重启恢复与仍在写入的旧线程重叠。
- 原生检查点和退出证据同时缺失时维持未确认并记录错误，供运维核实。已关闭模型连接后的供应商计算/计费继续由供应商决定。

## 上下文与轻量路径

已取消 Run 标记 `execution_interrupted`。Agno 默认排除其普通历史；后续上下文仅补充有明确来源的已确认工具结果预览，未完成回答不自动成为结论。LearningMachine、模型与嵌入等待归属当前 scope，停止后不启动新提取；已经初始化的学习存储也在写入边界检查取消。已正式保存的用户事实保留。

受控 `get_graph_info` 的结果不超过 64 KiB 时，按不可变图版本缓存，重复读取无需启动 worker；换版本后重新计算。重型算法保持独占进程。Runtime 回收已确认终态的内存记录，Graph 保留最多 256 条已保存作业和已完成启动缓存，进度缓冲保留最近 100 项。持久幂等键和待确认执行继续保留。

## 前端调度

取消优先使用既有每会话控制通道，每会话最多一个有效控制请求和一个实际定时器，全局仍最多四条观察流。取消超时 5 秒、普通查询 15 秒。取消退避为 1/2/4/8/15/30 秒及 ±20% 抖动，心跳/输出不清零取消失败计数。

本地仅持久化必要身份和待确认意图。刷新先查询再补发同一个取消 ID，存储失败不阻止本页停止。退出、删除及卸载清理客户端请求/时钟，已接受的服务端意图继续执行。旧暂停标记只做迁移清理，不转换为停止命令。

普通快照与会话激活均消费权威 control 摘要；另一页面发起停止、本地意图存储失败时仍按服务端准入限制发送。远端停止使用同一调度器只读核实，不伪造本地取消意图。较旧控制版本的快照不覆盖新状态，取消终态保持稳定。

旧 Run 快照按 run_id 合并相应消息，保留新 Run、草稿和当前图；独立控制通知不推进业务游标。现有 V2 原子批次、去重和快照恢复继续使用。

升级前的历史 Run 可以缺少控制账本记录；经过原生归属校验的快照仍可读取，返回 `control: null`，不会自动创建控制记录或触发执行。

## 验证

PostgreSQL 并发验收应使用隔离测试库，测试自行创建/清理独立 schema。未配置时单元测试使用临时 SQLite，不能替代 PostgreSQL 验收。

```sh
export IGRAPH_CONTROL_TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/TEST_DATABASE'
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_cancellation_http.py -q
.venv/bin/python scripts/benchmark_execution_isolation.py --mode baseline
.venv/bin/python scripts/benchmark_execution_isolation.py --mode isolated
cd frontend
pnpm test:coverage
pnpm build
pnpm test:e2e
pnpm test:e2e:native
pnpm test:e2e:cancel
```

联合测试使用真实 Factory、MCP、Graph worker、BFF、数据库、HTTP/浏览器，仅模型服务替换为确定性生产者；检查连接关闭、实际 Task/进程退出、调用次数、终态及产物。CI 使用 Python 3.12 和 PostgreSQL 验证后端真实 HTTP 链路，Node 22 仅用于前端类型检查和构建，不运行浏览器或侧栏动画测试，也不设覆盖率门槛。浏览器测试保留为手动排查工具，其 Python 可用 `IGRAPH_TEST_PYTHON` 指定，默认项目 `.venv/bin/python`。

### 本轮验证（2026-09-05）

- 全量 Python：510 通过、6 跳过、42 子测试通过。跳过项为真实数据集/训练、NebulaGraph、CUDA 的既有环境门禁。全量行覆盖率 55.68%、分支 37.79%，通过恢复后的 51%/34% 门槛。
- 全量通过后，针对同步线程退出和缓存回收继续补测；最终取消专项 174 通过，与全量有重叠。覆盖真实 PostgreSQL 的抛错/静默保存失败、待审批保存失败、首次交接失败、真实模型/学习/嵌入连接关闭和 SIGKILL 后的本地恢复。首 token、流式、训练、审批各 20 次连续场景包含其中。
- 前端 129 项通过；行/语句 70.43%、函数 60.04%、分支 72.23%，通过 44%/44%/59%/68% 门槛。类型检查和生产构建通过。
- 浏览器：通用 16 项、原生恢复 2 项、取消 7 项通过。
- 最后的图发布、manifest 和轻量图信息回归 5 项通过；取消浏览器组重跑 7 项通过。`git diff --check`、Compose 配置校验通过。完整本轮说明见 [修复记录](../archive/reports/RUN_CANCELLATION_REVIEW_FIXES.md)。
- 本机 Python 3.14.3 / Node 24.2.0；目标 CI 为 Python 3.12 / Node 22。GPU、大图和目标部署环境性能验收继续保留。

以下为改造初期的小图基准；本轮重测记录在修复说明中。

小图 CPU 基准：500 节点/1000 边，degree_analysis，各模式独立启动进程、连续执行三次，输出 SHA-256 完全相同。

| 模式 | 三次耗时（ms） | 采样进程树峰值 RSS（MiB） |
| --- | --- | --- |
| 原有进程池 | 506.00 / 5.03 / 0.72 | 581.05 |
| 作业独占进程 | 664.44 / 657.74 / 661.29 | 587.77 |

独占进程每次承担初始化成本，对短小计算影响明显。该基准只揭示固定开销，不能代替生产大图、GPU 和多任务压力验证。后续优化短工具路由时仍需保留实际退出保证。

## 发布与剩余门禁

1. 保持 `IGRAPH_RUN_CANCEL_ENABLED=false`，先执行 `python -m agent_runtime.migrate_execution_controls`。迁移只增表/索引，可重复执行，运行时启动也核对建表。
2. 验证代表性大图、GPU、多任务负载和异常文件系统；5 秒从执行服务收到停止信号起计，本机文件系统或 OS 异常时保留 pending。仅 local 首发可保持 Nebula 探针 false；启用远端前另行实现远端执行/提交取消契约。
3. 完成目标 Python/Node CI、dev 联合验证和代表性性能评估。
4. 排空旧 Run 和无完整归属的 Job，发布同组 Runtime、Graph、MCP、BFF、前端。
5. 四个服务配置相同非空随机 `IGRAPH_CONTROL_SERVICE_KEY`，验收后统一开启取消开关并核对 capabilities。Compose 内部地址已适配，健康检查保留单向依赖。
6. 保留整组回滚版本和控制表；回滚前处理活动 worker。禁止删除控制表来消除 pending 或幂等冲突。

本次不提供任务进程重启续跑、多副本调度或跨系统回滚；已提交外部副作用保留，供应商连接关闭后的计算及计费由供应商决定。

## 排障

关注 `run_cancel_accepted`、`run_cancel_dispatched`、`model_request_closed`、`native_terminal_save_pending`、`job_executor_exit_pending`、`run_cancel_confirmed`、`model_stream_closed`、`worker_stop_signal`、`worker_exit`、`worker_exit_confirmation_pending`、`job_registration_confirmation_pending`、`job_cancel_confirmation_pending`、`worker_staging_cleanup_pending`。控制日志仅记录必要 ID、版本、耗时、RSS 和错误类别。

先查 pending_components，再核对执行者/进程身份、服务地址/凭据、数据库保存，最后检查旧 Run 快照。未知执行者或进程成员、保存失败均保留未确认；请求重试保持原取消 ID，避免错误解锁。
