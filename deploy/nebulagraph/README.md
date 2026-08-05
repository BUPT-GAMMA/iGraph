# iGraph 本地 NebulaGraph 3.8

该目录提供用于 iGraph 开发和联调的单节点 NebulaGraph 3.8.0，包含完整的
Meta、Storage 和 Graph 服务。Graph Service 仅绑定宿主机回环地址，图数据和
日志通过目录挂载持久化。

## 启动

```bash
cd iGraph/deploy/nebulagraph
docker compose pull
docker pull docker.io/vesoft/nebula-importer:v4
docker compose up -d
docker compose ps
```

首次启动时，`init-storage` 会自动向 Meta Service 注册 Storage Service。它成功
退出是正常行为，其余三个服务应显示为 `healthy`。

## 连接参数

在 `iGraph/.env` 中启用：

```env
NEBULA_ENABLED=1
NEBULA_HOSTS=127.0.0.1:9669
NEBULA_USER=root
NEBULA_PASSWORD=nebula
NEBULA_SPACE=igraph
NEBULA_POOL_SIZE=10
NEBULA_BATCH_SIZE=256
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
```

默认账号只用于绑定到 `127.0.0.1` 的本地开发服务，不应直接用于远程或生产环境。
Importer 是一次性批量导入进程，不作为常驻 Compose 服务运行；应用在大图任务中通过
`docker run --rm --network=host` 启动它，任务结束后自动删除容器和临时 CSV。

## 验证和维护

```bash
curl -fsS http://127.0.0.1:19669/status
docker compose exec graphd /usr/local/nebula/bin/nebula-graphd --version
docker compose logs --tail=100
```

停止服务不会删除数据：

```bash
docker compose down
```

持久化位置：

- `iGraph/deploy/nebulagraph/data/meta0`：元数据。
- `iGraph/deploy/nebulagraph/data/storage0`：节点、边和索引。
- `iGraph/deploy/nebulagraph/logs/`：各服务日志。

不要在日常启停时删除 `data/`。只有确认不再需要本地测试图后，才应停止服务并
人工清理该目录。
