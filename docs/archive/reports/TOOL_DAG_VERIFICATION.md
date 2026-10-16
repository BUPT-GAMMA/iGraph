# 构图、配色与 DAG 并行验收（2026-09-07）

## 真实模型与完整服务链

在隔离测试部署中使用 Qwen 3.6 Plus，经 Backend → AgentOS → MCP → Graph Engine 执行两道问题。图为有向边 `A→B(1), B→C(2), A→C(5), C→D(1)`。

| 问题 | 实际工具结果 | 同轮调用数量 |
| --- | --- | --- |
| 加权最短路、全部节点度、强弱连通性 | `A→B→C→D`，权重 4；C 总度 3；弱连通、非强连通，4 个强连通分量 | 4（含图状态核验） |
| 忽略边权的最短路与有向环检测 | `A→C→D`，2 跳；无有向环 | 2 |

持久化工具的 `metrics.start_time/end_time` 确认第一轮 4 个调用时间区间有共同交集，第二轮 2 个调用也实际重叠；不是仅一次提交后串行执行。最终回答的路径、权重/跳数、度和连通性与已知答案一致。

测试环境原先使用占位模型地址，验收时临时复用已有提供商配置，完成后恢复。测试没有修改正式服务或把凭证写入仓库。

## 自动化回归

以下分组包含重叠测试，不合计为单一总数。

| 分组 | 结果 |
| --- | --- |
| 运行时：DAG、工具目录、自定义模型、审批、HTTP 取消、原生取消契约、持久化与构图 hooks | 170 passed，365 subtests passed |
| 最终异常处理调整后的 DAG/MCP/构图 hooks 针对性复验 | 64 passed，365 subtests passed |
| 隔离计算、worker 与图引擎并行调度 | 35 passed |
| 既有 GraphData API | 30 passed |
| 构图 Backend API | 5 passed |
| 构图存储与 HTTP | 22 passed，17 subtests passed |
| 搜索缓存并发与只读算法图不可变性 | 3 passed，19 subtests passed |
| 前端 Vitest | 166 passed |
| 前端浏览器 Playwright | 18 passed |
| TypeScript + Vite 生产构建 | 通过 |

主要正确性证据：

- 3 个不同 ProcessPool worker PID 在计算前完成会合，然后实际执行最短路、度和连通性，结果逐项等于串行对照。
- 8 次并发追加图数据没有丢失更新；图写入等待读者完成，后来的读者不越过排队写入。
- 后台构图及嵌套 job 在真正完成后才释放依赖；失败、取消、缺失结果和循环 job 链不会被包装成成功。
- Agno 真实暂停后 `acontinue_run` 执行已批准操作，并重新规划被阻塞后继；确认前没有执行副作用。
- 取消普通协程、异步生成器、后台任务和真实 worker 后检查清理与资源占用；普通 ProcessPool 工作在实际退出前不会提前归还容量。
- 文档与分类数据集工具不再因独立图写入被误判为版本冲突；真正的图读写仍拒绝过期 worker 结果。
- 查询缓存测试可在原无锁实现上稳定复现 `KeyError`；短临界区锁修复后通过。

测试使用 Python 3.12、固定 Agno 2.9.0。镜像中 AnyIO 4.14.2 与项目门槛不同的问题通过在隔离测试依赖目录使用规定的 4.14.1 解决，没有降低版本门槛。仍有既有 PyTorch `jit.script` 和 Agno `updated_tools` 的弃用提示，不影响上述测试。

构图范围是算法接入接口、数据源保存、已构图版本复用和六类图优化的预览/提交；没有接入算法时仍明确返回 `algorithm_unavailable`。资源调度的进程边界及配置见 [DAG 执行说明](../../architecture/TOOL_DAG_EXECUTION.md)。
