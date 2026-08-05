# NebulaGraph 存储

项目支持两种显式模式，默认保持兼容的 `local`：

| 模式 | 当前图的事实来源 | 会话磁盘 | 查询 |
|---|---|---|---|
| `GRAPH_STORAGE_BACKEND=local` | `GraphData` / NPZ | `graph_current.npz` 与版本快照 | NetworkX；可用 `nebula_sync_graph` 显式同步 |
| `GRAPH_STORAGE_BACKEND=nebula` | NebulaGraph 图版本 | 只保存 `graph_key`、版本号和规模摘要 | 精确查询走 nGQL；复杂算法有界物化到 GraphData/NetworkX |

主存储模式不会为当前图生成 `graph_current.npz`，也不会在 `GraphSession` 中长期保存完整
`GraphData`。上传文件、文本抽图、三元组和本地数据集会先短暂转换成 `GraphData`，完成
校验与字段映射后写入 NebulaGraph；提交成功后只保留远程引用。

## 1. 前置条件

- NebulaGraph Server 3.8.x。
- Graph Service 地址可从 GraphData Server 访问，默认端口为 `9669`。
- 应用账号能够创建 Space、TAG、EDGE 和索引，并能够读写图数据。
- Python 环境已执行 `pip install -r requirements.txt`，项目固定使用
  `nebula3-python==3.8.3`。
- 大图自动导入需要 Docker CLI 可用，并预先拉取
  `docker.io/vesoft/nebula-importer:v4`；也可以用 `NEBULA_IMPORTER_BINARY`
  指向本机 Importer 可执行文件。

本地开发环境可使用：

```bash
cd deploy/nebulagraph
docker compose up -d
docker pull docker.io/vesoft/nebula-importer:v4
docker compose ps -a
```

`metad0`、`storaged0`、`graphd` 应为 `healthy`，`init-storage` 正常状态为
`Exited (0)`。

## 2. 配置

在本地 `.env` 中配置，禁止提交真实密码：

```bash
GRAPH_STORAGE_BACKEND=nebula
NEBULA_ENABLED=1
NEBULA_HOSTS=127.0.0.1:9669
NEBULA_USER=root
NEBULA_PASSWORD=nebula
NEBULA_SPACE=igraph

NEBULA_POOL_SIZE=10
NEBULA_BATCH_SIZE=256

# auto: 小图用 Python 客户端，大图用 Importer
NEBULA_IMPORT_METHOD=auto
NEBULA_IMPORTER_THRESHOLD=100000
NEBULA_IMPORTER_IMAGE=docker.io/vesoft/nebula-importer:v4
NEBULA_IMPORTER_STAGING_DIR=data/nebula_importer
NEBULA_IMPORTER_BATCH_SIZE=1000
NEBULA_IMPORTER_READER_CONCURRENCY=4
NEBULA_IMPORTER_CONCURRENCY=16
NEBULA_IMPORTER_TIMEOUT_SECONDS=21600

NEBULA_SCHEMA_WAIT_SECONDS=30
NEBULA_MAX_RETRIES=3

# Nebula -> GraphData/NetworkX 的安全边界
NEBULA_MAX_MATERIALIZE_NODES=50000
NEBULA_MAX_MATERIALIZE_EDGES=200000
NEBULA_SEARCH_CANDIDATE_NODES=2000
NEBULA_SEARCH_CANDIDATE_EDGES=10000
```

首次写图会幂等创建 Space、`igraph_graph`、`igraph_entity`、
`igraph_relation` 和 `graph_key` 索引。NebulaGraph DDL 与索引传播是异步的，服务会在
`NEBULA_SCHEMA_WAIT_SECONDS` 窗口内轮询。如果同名 Schema 的字段不兼容，返回
`SCHEMA_MISMATCH`，不会删除或重建现有 Space。

可通过健康接口确认当前模式：

```bash
curl -sS http://127.0.0.1:21111/health
```

结果中的 `graph_storage_backend` 应为 `nebula`，`nebula_enabled` 应为 `true`。

## 3. 主存储数据流

```text
上传 NPZ/PT/JSON/边列表，或文本抽图/三元组/数据集
                 │
                 ▼
        临时 GraphData（校验和映射）
                 │
         ┌───────┴────────┐
         ▼                ▼
 小图客户端 INSERT   大图 CSV + Importer
         └───────┬────────┘
                 ▼
      NebulaGraph 新的不可变 graph_key 版本
                 │ 全部批次成功后才切换
                 ▼
       会话 active pointer + session_meta.json
                 │
        ┌────────┴─────────┐
        ▼                  ▼
  nGQL 原生查询       有界 GraphData/NetworkX
```

节点、边、会话和版本都带隔离作用域。相同节点名在不同用户、会话或版本中不会冲突。
版本切换只更新活动指针，不复制拓扑；数据集在主存储模式下保存不可变 `graph_key`
引用，不生成 `graph.npz`。

`NEBULA_IMPORT_METHOD=auto` 根据 `num_nodes + num_edges` 判断导入路径。超过
`NEBULA_IMPORTER_THRESHOLD` 时，服务将 GraphData 流式写成节点/边 CSV，再启动 Importer
并发导入；只有 Importer 全部成功且会话基础版本未变化时才发布 active pointer。失败时
旧版本继续可查询，临时 CSV 和包含密码的临时配置会自动清理。

Nebula 主存储模式下，NPZ/PT/JSON 上传总是作为后台 Job 执行，接口立即返回 `job_id`。
Agent 或调用方应使用 `get_job_status` / `get_job_result` 轮询解析、CSV 生成和 Importer
进度，避免长时间占用 HTTP/MCP 请求。

## 4. 工具路由

Agent 继续使用原来的通用工具名，不需要主动选择后端。

直接使用 NebulaGraph 的工具包括：

- `get_graph_info`
- `get_node_info`
- `get_edge_info` / `check_edge_exists`
- `get_neighbors`
- `shortest_path(use_weight=false)`
- `subgraph_extraction(method=ego_graph|node_subset)`
- `visualize_subgraph`

写入与版本工具包括：

- `load_graph` / `load_graph_from_upload`
- `load_graph_from_text` / `load_graph_from_triples`
- `clear_graph` / `undo_graph` / `to_undirected`
- `list_graph_versions` / `load_graph_version` / `discard_graph_version`
- `save_as_dataset` / `load_dataset`

P1 查询中，`search_node` / `search_edge` 使用数据库候选集再执行现有检索排序；
`all_shortest_paths`、`path_analysis` 和 `reachability` 只物化起点指定跳数内的子图。
`degree_analysis(node=...)` 直接返回单节点入度/出度。

仍需全局拓扑的 NetworkX 或 GraphML 算法会受
`NEBULA_MAX_MATERIALIZE_NODES/EDGES` 限制。超限返回 `SUBGRAPH_TOO_LARGE`，调用方应先用
`subgraph_extraction` 缩小作用域。加权最短路仍使用 NetworkX；非加权最短路使用
NebulaGraph 原生 `FIND SHORTEST PATH`。

兼容工具 `nebula_get_node_info`、`nebula_get_neighbors`、
`nebula_shortest_path` 仍保留。在主存储模式中 `nebula_sync_graph` 返回
`already_primary=true`；在 `local` 模式中它仍提交显式异步同步任务。

## 5. 启动与快速验证

```bash
python -m graph_data_service
```

创建会话并加载图：

```bash
curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo \
  -H 'X-User-ID: admin'

curl -sS -X POST http://127.0.0.1:21111/sessions/nebula-demo/graph/load_text \
  -H 'Content-Type: application/json' \
  -H 'X-User-ID: admin' \
  -d '{"edge_text":"A B 1\nB C 2\nA C 4","append":false}'
```

然后通过通用工具验证：

```text
get_graph_info()
get_neighbors(node_name="A", direction="out", limit=20)
shortest_path(source="A", target="C", use_weight=false, max_steps=10)
subgraph_extraction(method="ego_graph", node="A", k=2, create_version=false)
```

## 6. 数据映射与限制

- 节点和边使用固定的 `igraph_entity` / `igraph_relation` Schema，异质类型保存在
  `node_type` / `edge_type` 属性中。
- 节点/边标签使用 JSON 字符串保存。
- 单维 `edge_feat` 映射到 `confidence`。
- 高维 `node_feat`、多维 `edge_feat`、训练/验证/测试掩码无法原样写入当前固定 Schema；
  写入结果通过 `features_omitted` 明确报告。因此，依赖这些字段的 GraphML 任务应保留
  原始文件，或先扩展特征存储方案。
- 不开放任意 nGQL 执行入口。
- 主存储模式要求 NebulaGraph 可用；数据库不可用时写入失败，不会静默回退到 NPZ。
  `local` 模式仍保持原有 NPZ/NetworkX 行为。

## 7. 常见错误

| 错误码 | 含义 |
|---|---|
| `NEBULA_DISABLED` | 主存储模式未设置 `NEBULA_ENABLED=1` |
| `NEBULA_UNAVAILABLE` | 客户端缺失、网络或认证失败 |
| `SCHEMA_MISMATCH` | 已有 Schema 与项目要求不兼容 |
| `NOT_SYNCED` | 当前会话没有活动 Nebula 图 |
| `STALE_SYNC` | 会话版本与活动指针不一致 |
| `STALE_DURING_SYNC` | 混合模式同步期间本地图版本发生变化 |
| `SUBGRAPH_TOO_LARGE` | NetworkX/GraphML 所需物化子图超过安全上限 |
| `QUERY_FAILED` | 固定 nGQL 查询失败、Importer 导入失败或节点不存在 |
