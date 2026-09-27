# FalkorDB 协议实测交接（Task 1）

日期：2026-09-27。结论：**有条件的实现候选，尚未准入任意模型查询**。只读命令和部分服务器预算已经实测；单个 Cypher `range(1, 1000000000)` 在配置 8 MiB 查询内存上限、3 秒最大期限及 1 GiB 容器上限时仍使服务进程 OOM 重启。Task 4 必须完成下列限制和有界读取后，才能开放适配器。

## 固定环境与复现

- 官方 server 镜像：`falkordb/falkordb-server:v4.20.7@sha256:538fe1867a0a5dec55462eff5c1ea5edfc36246a9aa571cf1705b8b3f3d0f32c`，实测模块版本 `42007`，内置 Redis `8.10.2`。
- Python 客户端：`redis==8.1.0`（本地 `.venv` 与 agent-runtime requirements 固定）。直接使用 RESP2 `GRAPH.RO_QUERY`，测试的管理员连接只用于构造和核对夹具。
- 独立 Compose 项目：`igraph-falkordb-qa`；端口 `127.0.0.1:21188`，并接入 `igraph-backplane`，别名 `igraph-falkordb-qa`。密码来自 Compose secret 文件，没有写入仓库、文档或命令输出。
- 配置：`TIMEOUT_MAX=3000` 毫秒，`TIMEOUT_DEFAULT=1000` 毫秒，`RESULTSET_SIZE=128` 行，`QUERY_MEM_CAPACITY=8388608` 字节，容器内存 1 GiB。
- 可提交的真实 RESP2 帧：`tests/fixtures/falkordb_compact_path.resp2.json`。其中 Base64 编码的字节和 SHA-256 保留 `$` bulk 长度、`*` 数组数量、CRLF、UTF-8 原始编码，避免 Git 换行转换。专用测试在认证响应消费完毕后才读取 `GRAPH.RO_QUERY --compact` 回复；live 测试解码该文件并与新建测试图的服务器回复比较列和行。它不含密码或认证流量。另有本机 RESP 解码夹具 `output/multi-database-phase1/private/falkordb-protocol/*.json`（Git 忽略）；OOM 事件见同目录 `oom-events.txt`。

复现时自行建立权限为 `0600` 的密码文件，然后设置以下环境变量。密码通过文件读取，不放入 Compose YAML：

```bash
export IGRAPH_TEST_FALKORDB_PASSWORD_FILE=/path/to/private/password
docker compose -p igraph-falkordb-qa -f deploy/falkordb-test.compose.yaml up -d
export IGRAPH_TEST_FALKORDB_URI=redis://127.0.0.1:21188
export IGRAPH_TEST_FALKORDB_PASSWORD="$(cat "$IGRAPH_TEST_FALKORDB_PASSWORD_FILE")"
export IGRAPH_TEST_FALKORDB_GRAPH=igraph_admission
export IGRAPH_TEST_FALKORDB_EVIDENCE_DIR=output/multi-database-phase1/private/falkordb-protocol
.venv/bin/python -m pytest tests/test_falkordb_protocol.py --require-live -q
```

不设置三个必需的 `IGRAPH_TEST_FALKORDB_*` 连接变量时，`--require-live` 实际产生明确的九项 setup error；默认测试运行则跳过此专用服务。

## 协议结果

| 探针 | 实测结果与边界 |
| --- | --- |
| 可写账号只读路径 | 管理连接先创建节点；同一密码的被测连接通过 `GRAPH.RO_QUERY` 读到 1。`CREATE`、`SET`、`DELETE`、`CREATE INDEX` 均返回错误；每次管理连接复核节点数仍为 1，属性仍为原值。没有在失败时回退 `GRAPH.QUERY`。 |
| 参数与字符 | `CYPHER target=...` 绑定含中文、单引号、反斜杠的值，往返相等。参数序列化仍需 Task 4 实现和测试；本探针仅固定服务器协议语法。 |
| 空值、数值、实体 | 默认非 compact 响应中，空结果返回列名和空行集；`42` 为整数，`2.5` 与 `true` 是字符串，`null` 为 null；`9007199254740993` 保留精确整数。节点包含 `id`、labels、properties；边包含 `id`、type、`src_node`、`dest_node`、properties。非 compact 路径是形如 `[(1), [0], (2)]` 的字符串，信息不足。`--compact` 实测返回类型标签：整数 3、浮点 5、布尔 4、null 1、节点 8、边 7、路径 9；路径内含有序节点数组与边数组，边含源/目标 ID。`CALL db.labels()`、`db.relationshipTypes()`、`db.propertyKeys()` 可经 `GRAPH.RO_QUERY --compact` 获取 ID 映射。Task 4 应使用 compact 格式完整解码，不能把非 compact 路径文本作为最终结构。 |
| 结果行预算 | `UNWIND range(1, 100000) RETURN x` 实际只返回 128 行。一个单元格仍可返回 1,048,576 字节字符串；`collect` 可在一行中返回约 688,895 字节值。`RESULTSET_SIZE` **不是字节上限**。 |
| 查询内存 | `UNWIND range(1,300000) AS x RETURN collect(x)` 返回 `Query's mem consumption exceeded capacity`，图仍可读；但 10 亿元素 `range` 曾使 1 GiB 容器产生两次 Docker `oom`/`die`/`start` 事件。`QUERY_MEM_CAPACITY` 对某些聚合有效，**不能视为覆盖所有 Cypher 分配路径**。不得重复该危险压力探针作为常规测试。 |
| 服务器期限 | 两层 `UNWIND range(1,10000)` 聚合、命令 `TIMEOUT 1` 返回 `Query timed out`，约 2–4 毫秒内回到客户端；图仍在。配置读取证实 `TIMEOUT_MAX=3000`、`TIMEOUT_DEFAULT=1000`。 |
| 客户端断开 | 异步被测连接取消后，`CLIENT LIST` 中客户端 ID 消失；`GRAPH.INFO` 仍显示 1 个运行中查询，约 1 秒后归零。**断开连接不会立即取消服务器查询**；服务器期限提供有限的结束保证。 |
| 地址与连接 | 测试 URI 要求数字 IP；普通 `redis.Redis` 单连接模式查询前后 `CLIENT ID` 相同。该实验没有覆盖 TLS、DNS 名称或集群。`redis-py 8.1.0` 的 `Connection._connect()` 每次连接会对 `self.host` 调用 `getaddrinfo`，故 Task 4 应消费已校验的数字 IP，禁用集群/Sentinel/重定向，并单独保留原始域名作为 TLS SNI/证书主机名。其 `SSLConnection` 当前用 `self.host` 作为 `server_hostname`；直接把 host 改成 IP 会破坏域名证书校验。 |

## Task 4 准入条件

1. 模型查询只走 `GRAPH.RO_QUERY`，没有 `GRAPH.QUERY` 回退；保留写入、DDL、扩展入口负例。连接时核实目标模块版本与 `TIMEOUT_MAX`、`TIMEOUT_DEFAULT`、`RESULTSET_SIZE`、`QUERY_MEM_CAPACITY` 的**有效值**，预算未配置或超出适配器允许值就拒绝。每次请求设置不高于服务器最大值的 `TIMEOUT`，取消后关闭客户端连接，同时把服务器端终止表述为“在服务器期限内结束”。
2. 查询解析/允许范围必须排除 `range()` 及其他未验证的大量预分配函数；该限制需能区分字符串、注释、反引号，不能靠简单子串匹配。任意 Cypher 尚未获得内存安全准入。对 `collect` 等可能聚合成单个大值的查询，服务器内存上限与客户端响应上限都要生效。
3. **不能直接使用 `redis.Redis.execute_command` 获取模型查询的大响应。** 已安装的 redis-py RESP2 解析器对 `$<length>` 调用 `_buffer.read(int(response))`，对 `*<count>` 构建完整 Python 列表；`SocketBuffer` 又使用 `BytesIO` 缓冲。Task 4 需要在分配前检查 bulk 长度、数组数量、嵌套深度、累计原始字节和行数，限长 `recv` 并在超限时关闭连接；服务端行数限制之外再做客户端预算。可用受控命令打包和自有有界 RESP2 读取器，避免把任意 Redis 命令暴露给 Agent。
4. 目标地址应先按现有网络规则校验，固定实际拨号 IP；TLS 时另用原始主机名做 SNI 和证书校验。禁用自动发现、重定向与未经重新校验的重连。测试仅证明数字 IP 单连接，TLS 路径待 Task 4 验证。
5. 模型查询使用 `GRAPH.RO_QUERY ... --compact`，按 ValueType 标签显式解码节点、边、路径、布尔、浮点与大整数；通过只读 schema procedures 获取标签、边类型及属性 key 的 ID 映射。路径的节点顺序与边源/目标同时保留，schema 缓存失配须刷新或拒绝，不能默默降级为无类型字符串。

参考：[官方只读命令](https://docs.falkordb.com/commands/graph.ro-query)、[官方 compact 协议与映射](https://docs.falkordb.com/design/client-spec)、[官方查询与参数](https://docs.falkordb.com/commands/graph.query)、[服务器预算参数](https://docs.falkordb.com/getting-started/configuration)、[官方 server 镜像](https://hub.docker.com/r/falkordb/falkordb-server/tags)、[redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/)、[redis-py 取消行为](https://redis.io/docs/latest/develop/clients/redis-py/async/)。

## Task 4：实现后准入与产品限制（2026-09-27）

FalkorDB Adapter 已通过专用可写账号下的协议、安全、Cora 与边界夹具验收，并以 **experimental** 状态进入注册表。真实 Agent/浏览器验收由 Task 6 完成；这里不宣称整体产品已 verified。Neo4j 既有状态保持。

- 每次调用新建独占 socket；网络规则先解析并验证全部地址，实际连接固定数字 IP，TLS 使用原域名做 SNI/证书检查。无连接池、重试、重定向、Sentinel 或集群发现。TLS 测试使用本地 CA/真实握手，正确主机名成功、错误主机名失败。
- 异步 Adapter 使用 `asyncio.to_thread` 驱动独占阻塞 socket：自行逐帧、限长读取 RESP2，避免 redis-py 对任意响应预分配。读之前检查 bulk 长度、数组数量、嵌套深度、累计线长/帧字节与行数；所有 recv 不超过 4096 字节。协议超限失败整个查询，不把未完整读取的帧报告为完整结果。
- 连接时检查实际 `MODULE LIST` 版本为 graph 42007，并检查四个有效服务器配置均为正且不高于 Task 1 已测上限。ACL 账号必须允许这些只读配置/版本查询；无法核实预算就拒绝准入。
- 只允许专用 `GRAPH.RO_QUERY --compact`。函数采用 token 级白名单；明确禁止 `range`、`split`、未知函数/过程、动态/外部入口、变量长度路径和列表/模式推导的未验证扩展。列表推导按 `[变量 IN ...]` 的 token 绑定结构识别，即使省略投影 `|` 和 `WHERE` 也拒绝；普通列表、索引、字符串、注释和属性名不会仅因含 `IN` 而被拒绝。业务查询可自由组合允许的 Cypher 子集，任意 Cypher 尚未获准。聚合 `collect` 保留服务器内存限制和客户端帧预算两层保护。
- 实体元数据 ID 映射每次调用独立获取、无跨调用缓存；映射达到服务器行上限或出现未知 ID/类型就明确失败。compact 节点、边、路径、数组和标量保留类型；路径遍历顺序和边真实源/目标分别保留。未知 map/point 等类型尚未支持，绝不字符串化。超过 JavaScript 安全整数范围的普通值统一为 `{"type":"integer","value":"9007199254740993"}`，此契约也适用于 Neo4j；实体 ID 为精确字符串。
- schema 标签/关系名称来自元数据，属性从最多 32 个节点/关系采样，始终 `coverage.sampled=true`。查询达到服务器 128 行边界时保守 `truncated=true`；小于 128 的自定义服务器上限同理。工作区结果保存 source、原查询与参数、完整有界行；预览预算另计。
- 取消关闭当前调用 socket 并透传 `CancelledError`。实测客户端取消后服务器仍运行，约 1 秒后结束；不是服务器主动取消。每条查询 TIMEOUT 不大于剩余调用期限和有效 TIMEOUT_MAX，最大 3 秒。
- Cora 独立管理脚本复用原受限加载器和转换规则，输入 SHA-256 必须与可信 manifest 一致；2708 节点、5278 条去重边、标签分布及样本邻居已实际对照。拒绝非本批非空目标。FalkorDB 导入为独占目标下可重跑批次，不提供跨批原子事务；中断后以相同 hash 恢复。
- QA Compose 现有专用服务新增独立 `igraph-falkordb-qa_falkordb-qa-data` 持久卷和 AOF/RDB；没有删除任何已有卷。私有环境 `.env.falkordb-test` 与密码文件 `output/multi-database-phase1/private/falkordb-password` 均为 0600；复现时先 `set -a; source .env.falkordb-test; set +a`，不要打印变量。证据目录为 `output/multi-database-phase1/task4-falkordb/`，Task 1 原始证据保持。

运行：`.venv/bin/python -m pytest tests/test_falkordb_adapter.py tests/test_import_cora_falkordb.py -q`，以及加载私有测试环境后 `.venv/bin/python -m pytest tests/test_falkordb_integration.py --require-live -q`。管理导入入口为 `.venv/bin/python -m scripts.import_cora_falkordb --input cora.pt --manifest output/multi-database-phase1/task4-falkordb/cora-manifest.json --expected-sha256 <既有可信manifest的source_sha256>`。
