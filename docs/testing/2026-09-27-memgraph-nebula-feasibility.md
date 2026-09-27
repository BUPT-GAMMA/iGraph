# Memgraph / NebulaGraph 独立技术验证

Task 7；基线 `3e8d6e3`，实测跨越 2026-09-27 至 2026-09-28（Asia/Shanghai）。**两项验证完成，但当前候选执行路径均不准入产品实现。** 这不是“数据库本身不支持只读查询”的结论。注册表、UI、默认模型和第一批 A 的 Neo4j/FalkorDB 均未修改。

| 准入项 | Memgraph 3.13.1 | NebulaGraph 3.8.0 |
| --- | --- | --- |
| 强制认证、可写账号正控 | 通过，错误密码被拒绝，8 条可执行正控均改变预期状态 | 通过，错误密码被拒绝，9 条可执行正控均改变预期状态 |
| 原生读取、参数、结构、类型 | 测试范围通过 | 测试范围通过；FETCH VID 位置不支持参数 |
| 可用的执行前安全策略 | **失败**：EXPLAIN 摘要对读取和写入均为 `rw`，假设不能区分 | **未解决**：计划有算子但未验证完整的副作用分类器；入口拒绝所有查询 |
| 拒绝全部的负控 / 状态不变 | 通过；正常读也被拒绝，不能称安全执行路径已通过 | 通过；正常读也被拒绝，不能称安全执行路径已通过 |
| 同一连接配置的并发请求 | 独立请求 session 返回各自参数，通过 | 独立请求 session 返回各自参数，通过 |
| 服务器期限 | 2 秒默认期限、0.1 秒请求期限通过 | 通用 wall-clock 期限尚未验证 |
| 主动取消 | 找到本次事务 ID、终止、服务端不再运行，通过 | 有界任务结束前未找到其 query ID，未验证 KILL QUERY |
| 客户端断开 | 客户端退出后服务器仍继续约 1.95 秒，由服务器期限结束 | 未验证；客户端超时不计为服务器停止 |
| 反序列化前字节预算 | **原始 driver 路径失败**：64 KiB 单元格完整读入 | **原始 driver 路径失败**：64 KiB 单元格完整读入 |
| 隔离网络 / 主机及容器读取 | 通过本地范围 | 通过本地范围 |
| TLS / DNS 重绑定 / 集群 / IP 更换发现 | 未验证 | 未验证 |
| 产品准入 | **not admitted** | **not admitted** |

“字节预算失败”指本轮直接使用客户端的候选路径没有实现目标 4 KiB 预算，不能声称配置了不存在的客户端限额；不推断产品或其他协议实现无法有界读取。两项均有必需门槛未过，因此没有生成假定已获准入的 Adapter 实施计划；下文记录重新验证所需机制。

## 固定环境、网络与资源

所有镜像都按 digest 拉取；实际平台均为 linux/amd64。完整 image ID、RepoDigest、有效 cgroup 值、卷、健康与网络记录见 [environment-final.json](../../output/multi-database-phase1/task7/environment-final.json)。

| 用途 | 固定镜像及拉取摘要 |
| --- | --- |
| Memgraph | `memgraph/memgraph:3.13.1@sha256:bd3fe13228f40881365486729c78110dde6e04f3c1f102c84720d31f38b693f8` |
| Nebula Meta | `vesoft/nebula-metad:v3.8.0@sha256:ab687bd32d3e441d436842b41427ba0961a5e169c46997ef03fa4dcfd5388b35` |
| Nebula Storage | `vesoft/nebula-storaged:v3.8.0@sha256:7142642ee69001a5b5c50520196538d58bb2ec3930707c067cb4c4e2be3890f9` |
| Nebula Graph | `vesoft/nebula-graphd:v3.8.0@sha256:1040573cc684ea6cc5e673b667422e9abab607c9d553c2c17c7bac6ad8a74e05` |
| 固定目标入口转发器 | `python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` |

Memgraph `SHOW VERSION` 实际为 3.13.1。Nebula 三个二进制均报告 `3.8.0, Git: 7458486, Build Time: May 15 2024 19:58:09`；`SHOW HOSTS GRAPH` 同时确认 ONLINE / 3.8.0。没有使用 Console 镜像。客户端实际为 `neo4j==6.3.0`、`nebula3-python==3.8.3`，另置于 [graph-probes.txt](../../deploy/requirements/graph-probes.txt)，未更改产品依赖。

- Compose 项目为 `igraph-memgraph-probe`、`igraph-nebula-probe`。只使用各项目新命名卷；旧 `deploy/nebulagraph/data` 和 A 产品 QA 卷未读取/改写/删除。Memgraph 的数据、配置、日志卷均独立。
- 数据库只连接 `internal: true` 网络。rootless Docker 的内部网络没有发布主机端口，已据实际 `NetworkSettings.Ports=null` 改为固定目标入口转发器：`127.0.0.1:21189 → memgraph:7687`、`127.0.0.1:21190 → graphd:9669`。每个转发器最多 16 个连接、4 KiB 转发块、15 秒无数据期限、64 MiB / 0.25 CPU / 32 PID；它**不是结果预算或安全分类器**。转发器同时连接入口网络和内部网络；数据库本身没有默认路由。
- 从两个数据库内部网络分别尝试 TCP `1.1.1.1:443`，均返回 `Network is unreachable`；主机真实客户端完整探针与同网容器的真实认证读取均通过，见 [container-clients.json](../../output/multi-database-phase1/task7/container-clients.json)。不把网络隔离当作文件副作用保护，也不把本机直连当作 TLS/DNS 安全通过。
- Memgraph 有效 cgroup 为 1 GiB、2 CPU、256 PID，进程 `--memory-limit=512` MiB、`--query-execution-timeout-sec=2`、`--allow-load-csv=false`、`--storage-enable-schema-metadata=true`。
- Nebula 每个数据库容器有效 cgroup 为 2 GiB、2 CPU、512 PID。启用认证、32 个最大连接、65536 字节查询文本上限与 0.8 内存水位。`--containerized=true`，cgroup v2 路径存在；未用 OOM 实验宣称查询级内存追踪覆盖所有分配。`max_allowed_query_size` 是查询文本上限，不是结果大小上限。
- Nebula 在此 128 核主机上以 256 PID 启动时 Meta 报 `Resource temporarily unavailable`，退出 139，`OOMKilled=false`。降低 worker/io 参数及 taskset 仍不足；512 PID 后稳定，实测 Meta 约 310 线程。Compose `cpuset` 在本 rootless 环境不受支持，最终使用镜像已有 `taskset`，默认 CPU 0,1，可用 `IGRAPH_PROBE_CPUSET` 覆盖。该故障是启动资源边界，不是 nGQL 能力失败。Storage 必须先启动，再通过 Graph 执行 `ADD HOSTS "storaged":9779` 才健康，故 Graph 不等待 Storage 健康以免初始化死锁。

## Memgraph 的实际结果

证据：[memgraph-final3.json](../../output/multi-database-phase1/task7/memgraph-final3.json)、[运行日志](../../output/multi-database-phase1/task7/memgraph-final3.log)。探针 [remote_graph_memgraph.py](../../scripts/probes/remote_graph_memgraph.py) 仅连接固定 QA 端口，并核对 Compose 项目与卷归属。

`qa_probe` 已建立为有完整写能力的认证用户。首次试跑发现：无用户时 Memgraph 会接受提供的任意凭据，因此脚本已修正为检查 `SHOW USERS` 并要求错误密码确实拒绝。最终证据中 `SHOW TRANSACTIONS.username=qa_probe`。最初匿名轮次 `memgraph-run1.json` **不计入认证验收**。

参数映射保留中文、引号、注释样式字符串、数字列表；固定 id 参数读取和空集均通过。Neo4j driver 原生 Node / Relationship / Path 保留标签、属性、边方向和路径顺序；整数 `9007199254740993` 精确，布尔和 null 保留类型。实体编码仅用于小型探针证据，不是已交付的产品结果编码器。

结构以 `SHOW NODE_LABELS INFO`、`SHOW EDGE_TYPES INFO`、`SHOW INDEX INFO`、`schema.node_type_properties()`、`schema.rel_type_properties()` 五项验证。前两项初始因 schema metadata 开关未启用而失败，启用后通过；后两项是本镜像自带过程，无 MAGE 依赖。

候选假设是：先执行 `EXPLAIN <完整查询>`，仅在 summary 为 `r` 时执行原查询。**实测 EXPLAIN 正常 RETURN 与 CREATE/SET/MERGE/DELETE/CALL 均给出 `rw`；DDL/管理的 EXPLAIN 则可能解析失败。** 因此假设无法开放读取。探针保留计划文本和 summary，不以 Neo4j 语义解释 Memgraph。拒绝所有内容的负控虽然保持状态不变，不能提供业务查询功能。`READ_ACCESS` 下直接 CREATE 仍成功，路由提示没有写入保护。

9 类控制中，CREATE、SET、MERGE、DELETE、索引、用户管理、自建过程和前置注释共 8 类正控实际成功，且节点/边/schema/用户/文件计数快照发生变化；恢复图夹具后交给候选入口均拒绝，相关快照不变。多语句 `RETURN 1; CREATE ...` 被目标服务器语法拒绝，只记该语法不支持，不冒称应用安全策略覆盖了它。管理员命令的密码和事务 ID 位置不支持 `$参数`，探针只在固定管理语句中使用严格验证的随机十六进制密码及服务器返回的十进制 ID；模型不控制这些值。

额外 [qa_probe 模块](../../deploy/memgraph-probe-module.py) 声明为 `mgp.read_proc`，却向容器 `/tmp` 的专用计数文件写入。直接 CALL 的摘要为 `r` 且计数增加；EXPLAIN 没有增加计数。这证明**图只读过程元数据不足以保证无外部副作用**。计数是单调证据，不伪装为事务回滚后的文件状态。

默认 CPU 工作负载由三个各 1000 元素的有限列表形成计数运算，服务端约 2 秒返回事务超时；没有创建十亿元素列表或大结果。请求 `Query(..., timeout=0.1)` 约 102 毫秒返回同类服务器超时。主动取消只终止 `SHOW TRANSACTIONS` 中与本轮完整查询文本匹配的服务器 ID；最终脚本为每次取消请求增加随机唯一注释标记，追加聚焦验证见 [memgraph-owned-cancel.json](../../output/multi-database-phase1/task7/memgraph-owned-cancel.json)。终止操作返回 `killed=true`，工作线程报告 `abort by another user`，最终轮约 67 毫秒；独立观察确认事务消失且服务可读。异步客户端 task 取消并断开后，服务器任务仍存在约 1.95 秒，随后由 2 秒期限结束；这不是立即服务器取消。

重新考虑准入至少需要：可执行前的可靠语义分类机制（含管理/DDL/多语句/扩展），独立的过程允许策略，有界结果解码，以及网络/TLS/取消隔离的 Adapter 契约验证。当前没有为绕过失败门槛开发完整解析器。

## NebulaGraph 的实际结果

证据：[nebula-final2.json](../../output/multi-database-phase1/task7/nebula-final2.json)、[运行日志](../../output/multi-database-phase1/task7/nebula-final2.log)。探针 [remote_graph_nebula.py](../../scripts/probes/remote_graph_nebula.py) 只操作专用 `qa_task7` space、两节点一边及 canary。

`enable_authorize=true`，启动后修改 root 密码为私有随机 24 位十六进制字符串，错误密码返回 `AuthFailedException`。初始 48 位密码被目标版本“最多 24 字符”限制拒绝；已按实际约束调整。密码修改后存在缓存传播等待，脚本 bootstrap 等待 2 秒并重建客户端；若更慢可在心跳收敛后重跑，不能重置未知外部实例。测试使用 root 可写账户，不借只读权限拒绝负例。

使用原生 `execute_py` 参数 API。`RETURN $value` 对注入形状字符串原样往返，`MATCH (v:Probe) WHERE id(v) IN $ids RETURN v` 正常读取。`FETCH PROP ON Probe $id ...` 明确返回 `Parameter is not supported in vid`；不要插值用户 VID 来掩盖这一限制。`SHOW TAGS / DESCRIBE TAG / SHOW EDGES / DESCRIBE EDGE / SHOW TAG INDEXES / SHOW EDGE INDEXES` 六项均成功。节点、边、路径分别保留 Thrift 类型 9/10/11，含边 rank、源/目标；整数 3、布尔 2、null 1 和大整数值均对照。

INSERT、UPDATE、DELETE、CREATE TAG、DROP TAG、CREATE USER、SUBMIT JOB STATS、前置注释和 `RETURN 1; INSERT ...` 共 9 类直接可写正控成功，数据/tag/user/job ID 快照实际变化。复合语句不能只审查首词。`CALL example()` 和 Cypher `CREATE (:Probe ...)` 在固定版本解析失败，只属于这两种语法的结果，不把 nGQL 当完整 Cypher。

候选入口使用 `EXPLAIN { <原文>; }` 收集完整计划但不执行原查询。观察到 `InsertVertices`、`CreateTag`、管理/job 等原生算子，复合语句计划同时包含 Project 与 InsertVertices；执行前后状态一致。**目前未建立或验证覆盖全部副作用的算子/语法分类器**，因此入口明确拒绝全部，包括正常 RETURN；它不是通过验收的安全查询执行入口。没有据此证明 Nebula 无法实现可靠分类。

取消探针提前创建 worker 与 observer session 并分别 USE 固定 space，启动事件同步后并发执行有界 1000×1000 行计数与 `SHOW LOCAL QUERIES`。最终轮工作查询约 122 毫秒返回 1000000；观察列表仅见 observer 自己，没有捕获 worker 的 SessionID/ExecutionPlanID，因此**没有发出 KILL QUERY，主动取消仍未验证**。独立新 observer 的后快照成功，任务自然结束。原先 200000 行的短轮次及 `nebula-final.json` 中错误的 released-session 后观察保留作诊断，均不当取消通过；后者明确由 `nebula-final2.json` 的成功独立后观察替代。没有继续放大负载。官方存在 KILL 语法，不因本轮观察窗口失败声称该能力不存在。

客户端 4 秒 socket timeout 与 storage RPC timeout 都不构成通用服务器 wall-clock deadline；本轮未找到并验证该约束。服务器主动取消、客户端断开后的结束保证、查询级内存覆盖，以及有界 Thrift 响应解码仍需验证。`SHOW HOSTS` 中广告地址为内部 `graphd:9669` / `storaged:9779`，本轮 ConnectionPool 仅配置固定回环入口并禁用 session 重连；未测试服务器 IP 更换、TLS、其他驱动发现/重连路径。初次验收客户端在错误密码负控后关闭 pool 曾记录 `Closing a connection that is in use` 警告，原日志如实保留；后续审查修复采用独占 SDK Connection 并显式关闭，认证专用实测已无该警告，见下文。

## 复现与结果约定

只在本项目隔离 QA 环境执行；脚本会写固定夹具。已有私密文件是 `output/multi-database-phase1/private/task7/credentials.json`（0600，目录 0700），已被 Git 忽略，不把内容复制进命令或报告。新环境可生成：

```bash
.venv/bin/python - <<'PY'
import json, secrets
from pathlib import Path
p = Path('output/multi-database-phase1/private/task7/credentials.json')
p.parent.mkdir(parents=True, exist_ok=True)
p.parent.chmod(0o700)
if not p.exists():
    p.write_text(json.dumps({'memgraph': secrets.token_hex(24), 'nebula': secrets.token_hex(12)}))
    p.chmod(0o600)
PY

docker compose -f deploy/memgraph-probe.compose.yaml up -d --build
docker compose -f deploy/nebula-probe.compose.yaml up -d --build
# 首次启动 Storage 尚未健康是待 ADD HOSTS 状态；Graph/入口健康后运行 bootstrap。
.venv/bin/python scripts/probes/remote_graph_memgraph.py --bootstrap \
  --credentials output/multi-database-phase1/private/task7/credentials.json \
  --output output/multi-database-phase1/task7/memgraph-repeat.json
.venv/bin/python scripts/probes/remote_graph_nebula.py --bootstrap \
  --credentials output/multi-database-phase1/private/task7/credentials.json \
  --output output/multi-database-phase1/task7/nebula-repeat.json
```

部署首次返回后应等待 Memgraph、Meta、Graph 与两个入口 healthy，再运行探针；Storage 由探针初始化 host 后转 healthy。客户端可用独立 venv 安装固定的 `deploy/requirements/graph-probes.txt`，不需要更改产品容器。

**两脚本最终均退出 2**：完整矩阵已经写出，但存在 failed/unverified 必需门槛；不是绿色准入，也不是 harness crash。最终成功采集命令输出见各 `*-final*.log`。初期修正前的 exit 1、资源启动错误与匿名尝试保留作诊断记录，不纳入有效结果。对比快照去除耗时/session ID 和异步 job 进度字段，只保留真实图/schema/user/job ID；测试没有靠事务回滚掩盖写入。

最终执行两个专用 Compose 的 `up -d --build`，各数据库与入口健康；又分别重启 Memgraph 与 graphd，重复相同探针，`memgraph-repeat.json` / `nebula-repeat.json` 的准入矩阵与前轮一致，凭据与持久数据保留。未扩大负载。本机地址为 `bolt://127.0.0.1:21189`、Nebula `127.0.0.1:21190`。它们是开发探针环境，当前产品 UI 不提供这两个类型。静态编译、Compose 校验、重复 live 采集、私密值扫描及五个既有脏文件哈希核对均通过，命令和断言见 [validation-final.json](../../output/multi-database-phase1/task7/validation-final.json)。产品没有代码变化，未无故重建现有 `igraph` 或 A 产品数据库。

依据：[Task 7 原计划](../superpowers/plans/2026-09-27-multi-database-graph-qa-phase1.md)、[调研输入与官方源码链接](../research/2026-09-27-memgraph-nebula-probe-inputs.md)、[Memgraph EXPLAIN 官方说明](https://memgraph.com/docs/querying/clauses/explain)、[Nebula EXPLAIN/PROFILE 官方说明](https://docs.nebula-graph.io/3.8.0/3.ngql-guide/17.query-tuning-statements/1.explain-and-profile/)、[Nebula KILL QUERY 官方说明](https://docs.nebula-graph.io/3.8.0/3.ngql-guide/17.query-tuning-statements/6.kill-query/)。官方语法是候选依据，本报告的通过/失败以固定版本实测为准。


## 审查后的探针健壮性修复（2026-09-28）

首次独立审查通过，并提出三项 Minor。本次只修改探针、聚焦测试和文档，不增加工作负载、不重跑写入矩阵、不调用模型，也未修改 Compose/服务镜像或重建产品服务。

- 两个脚本要求正控和负控的观察均成功、非空且具有相同查询集合。观察失败/缺少结果时为 `unverified`，不把失败响应比较相等当作数据库状态不变；成功但未实际改变状态的正控为 `fail`。只有确认正控改变状态、候选拒绝、负控前后不变才为 `pass`，汇总保留失败/未验证状态，零个有效正控不能通过。以后输出同时保留 `positive_before` / `positive_after` 原始快照。
- Memgraph 服务器期限判定必须有成功的 `SHOW TRANSACTIONS` 结果并确认目标已消失；观察错误或缺少 rows 不能当作空列表和已终止。
- Nebula 3.8.3 的 `ConnectionPool.get_session` 在认证失败后留下池连接占用标记。认证负控及 bootstrap 认证检查改为独占 SDK `Connection`，认证成功时 signout，所有路径最终 close；没有过滤日志或修改 SDK。实际只执行一次正确/一次错误认证，错误返回 `AuthFailedException`，无清理警告，未执行查询或修改图。

`.venv/bin/python -m pytest tests/test_remote_graph_probe_evidence.py -q`：RED **30 失败 / 7 通过**，修复后 **37 通过**。覆盖失败/缺失/空快照、无变化正控、未拒绝/仍修改状态、汇总失败传播、服务器观察失败及认证关闭/异常保留。[RED 日志](../../output/multi-database-phase1/task7-review-fix/red.log)、[最终 GREEN](../../output/multi-database-phase1/task7-review-fix/green-final.log)、[认证实测](../../output/multi-database-phase1/task7-review-fix/auth-only.json)、[认证日志](../../output/multi-database-phase1/task7-review-fix/auth-only.log)。

只读回放命令：`PYTHONPATH=. .venv/bin/python output/multi-database-phase1/task7-review-fix/replay.py`。四份 final/repeat 报告、40 个已有控制状态及准入矩阵保持一致，两个产品仍不准入；[回放结果及源文件 SHA256](../../output/multi-database-phase1/task7-review-fix/replay.json)。**回放限制：旧 JSON 只有 `positive_changed_state`，没有原始正控前后快照；本次依赖其记录的变化标记，不伪称重新验证了缺失的观察。** 候选前后快照及 Memgraph 期限观察则确有原始成功结果可复核。原报告/日志未被改写。
