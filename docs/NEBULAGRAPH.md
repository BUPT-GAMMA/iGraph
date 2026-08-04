# NebulaGraph 混合存储

当前实现保留 NPZ/GraphData 作为会话快照、版本回滚和 GraphML 数据源，
并通过显式工具把当前图同步到 NebulaGraph，用 nGQL 执行节点、邻居和最短路径查询。

## 1. 前置条件

- NebulaGraph Server 3.8.x。
- Graph Service 地址可从 GraphData Server 访问，默认端口为 `9669`。
- 应用账号能够创建 Space、TAG、EDGE 和索引，并能够读写图数据。
- Python 环境已执行 `pip install -r requirements.txt`。

## 2. 配置

在本地 `.env` 中配置，禁止提交真实密码：

```bash
NEBULA_ENABLED=1
NEBULA_HOSTS=127.0.0.1:9669
NEBULA_USER=root
NEBULA_PASSWORD="nebula"
NEBULA_SPACE=igraph_dev

NEBULA_POOL_SIZE=10
NEBULA_BATCH_SIZE=256
NEBULA_SCHEMA_WAIT_SECONDS=30
NEBULA_MAX_RETRIES=3
```

`NEBULA_HOSTS` 支持逗号分隔的多个 Graph Service 地址。首次同步会幂等创建
Space、`igraph_graph`、`igraph_entity`、`igraph_relation` 及所需索引。
NebulaGraph DDL 和索引传播是异步的，首次同步会在
`NEBULA_SCHEMA_WAIT_SECONDS` 窗口内轮询，直到索引能够实际执行 `LOOKUP`。
如果发现同名但字段不兼容的 Schema，同步会返回 `SCHEMA_MISMATCH`，不会删除或重建已有 Space。

## 3. 启动与验证

启动 GraphData Server：

```bash
python -m graph_data_service
```

创建会话并加载一个小图：

```bash
curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo \
  -H 'X-User-ID: admin'

curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo/graph/load_text \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: admin' \
  -d '{"edge_text":"A B 1\nB C 2\nA C 4","append":false}'
```

检查连接并提交同步：

```bash
curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo/tools/nebula_status \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: admin' \
  -d '{"arguments":{}}'

curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo/tools/nebula_sync_graph \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: admin' \
  -d '{"arguments":{}}'
```

同步返回 `job_id`。使用现有 `get_job_status` 和 `get_job_result` 工具轮询。
同步完成后可调用：

```text
nebula_get_node_info(node_name="A")
nebula_get_neighbors(node_name="A", direction="out", limit=20)
nebula_shortest_path(source="A", target="C", direction="out", max_steps=10)
```

`nebula_shortest_path` 使用数据库原生 `FIND SHORTEST PATH`，按跳数计算；
加权最短路径仍使用现有 NetworkX `shortest_path` 工具。

## 4. 数据映射与限制

- 节点和边使用固定的 `igraph_entity`/`igraph_relation` Schema，类型保存在
  `node_type`/`edge_type` 属性中。
- 用户、会话和图版本参与 SHA-256 VID 生成，不同用户的同名节点不会冲突。
- 节点/边标签以 JSON 字符串保存。
- 高维 `node_feat`、多维 `edge_feat`、训练/验证/测试掩码继续保存在 NPZ；
  同步结果的 `features_omitted` 会列出未写入 NebulaGraph 的字段。
- NebulaGraph 不可用时，NPZ、NetworkX 和 GraphML 链路保持可用。
- 当前版本不开放任意 nGQL，不自动双写，也不直接从 NebulaGraph 向 GraphML 提供整图。

## 5. 常见错误

| 错误码 | 含义 |
|---|---|
| `NEBULA_DISABLED` | 未设置 `NEBULA_ENABLED=1` |
| `NEBULA_UNAVAILABLE` | 客户端缺失、网络或认证失败 |
| `SCHEMA_MISMATCH` | 已有 Schema 与项目要求不兼容 |
| `NOT_SYNCED` | 当前会话尚未同步 |
| `STALE_SYNC` | NPZ 图版本已经变化，需要重新同步 |
| `STALE_DURING_SYNC` | 同步过程中图版本发生变化，结果未发布 |
| `QUERY_FAILED` | 固定 nGQL 查询执行失败 |
