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
