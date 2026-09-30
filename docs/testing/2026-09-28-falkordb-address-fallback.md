# FalkorDB 多地址建连补修

日期：2026-09-28。基线 `7418cd9`。补修交付后独立验收发现的 P2：首个已核准地址不可达时，没有尝试其余地址。用户的原始独立验收文件保持不变。

## 行为与边界

`NetworkPolicy.resolve` 仍一次解析并校验整个地址集合；`open_connection` 现在依次尝试这个集合中的数字 IP，不重新解析、不发现新目标。

- 每个地址按“当前剩余时间 / 剩余地址数”获得一次建连时间份额；TCP、TLS 和等待工作线程共享该份额，整个调用的原始 deadline 不变。立即失败会把剩余时间留给后续地址。
- socket 创建、TCP 连接或 TLS 握手失败时关闭当前 socket，再尝试下一个地址。IPv6 socket 不可用也能尝试 IPv4；TLS 仍使用原主机名校验证书。
- 总期限耗尽后不再尝试新地址；外部取消直接传播，关闭当前独占 socket，不转为回退。
- 回退范围在 AUTH 之前结束。认证及调用方查询即使只发送了一部分、认证回复丢失或查询回复读取失败，也不会换地址或重放。连接成功后使用原始总期限执行命令。

这是顺序建连回退，没有增加并行拨号、连接池或命令重试。多地址时单个候选的等待时间会缩短，以避免首地址消耗全部预算。

## 验证

证据目录：`output/multi-database-phase1/address-fallback/`。

| 检查 | 实际结果 |
| --- | --- |
| 原问题 RED | 修复前新增连接测试 7 失败 / 6 通过；`red-confirmed.log`。更早 `red.log` 是测试替身误影响事件循环的夹具错误，不计作产品缺陷证据。 |
| 工作线程排队 RED | 仅 socket 超时无法约束尚未开始的 worker，新增聚焦测试 1 失败 / 16 deselected；`queue-red.log`。 |
| 连接、Adapter、网络策略和隔离回归 | `pytest tests/test_falkordb_connection.py tests/test_falkordb_adapter.py tests/test_remote_graph_policy.py tests/test_remote_graph_adapter_contract.py -q`：122 通过；`regression-final.log`。 |
| 全部相关后端回归 | 所有 `test_remote_graph_*.py`（排除 agent/integration），加 `test_falkordb_adapter.py`、`test_falkordb_connection.py`、`test_import_cora_falkordb.py`：270 通过、1 跳过，6.52 秒；`backend.log`。跳过项为缺少隔离 PostgreSQL URL，不声称本次重验迁移。 |
| 真实数据库 | 加载私有测试环境，`pytest tests/test_remote_graph_integration.py tests/test_falkordb_integration.py --require-live -q`：17 通过、零跳过，3.25 秒；`live.log`、`live-neo4j/`、`live-falkordb/`。 |

17 项新增连接测试覆盖首地址拒绝/超时、IPv6 socket 创建失败、TLS 握手/证书失败、TCP 与 TLS 共用份额、工作线程排队、全部失败、总期限耗尽、取消第二个候选且不尝试第三个、已发送认证/查询不重放、混入未核准 DNS 地址时完全拒绝拨号。测试在生产 `open_connection` 和真实 AUTH/RESP 实现上控制网络边界；超时测试使用可控时钟，不声称实测外部网络黑洞。

另有真实本机 TCP 服务：一次 DNS 策略快照先返回未监听的 `127.0.0.2`，再返回监听中的 `127.0.0.1`，连接回退后实际完成一次认证和命令交换。原有真实 TLS 证书测试与取消/并发隔离测试同时通过。

本次未重跑模型、浏览器、前端、原始协议压力探针或全仓 Python；这些部分没有实现变化。原有回答证据质量和全仓 `hiv.pt` 契约失败不在本次补修中解决。

## 本地部署

已根据当前运行项目执行：

```bash
docker compose -p igraph -f compose.yaml -f compose.dev.yaml up -d --build --no-deps agent-runtime
```

`rebuild.log` 记录构建和重建成功；既有卷标签警告保留，数据卷未删除。`health.json` 记录主项目 7 个服务运行、5 个已配置健康检查通过，runtime 内 `falkor_protocol.py` 哈希与工作区一致。原有 5 个修改文件及用户新增验收文件哈希不变。

前端 [http://127.0.0.1:21108](http://127.0.0.1:21108) 和后端 [http://127.0.0.1:21109/health](http://127.0.0.1:21109/health) 均返回 200。没有合并或推送。
