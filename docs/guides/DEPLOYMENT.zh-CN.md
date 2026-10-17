# iGraph 部署说明

> 更新日期：2026-10-17。本文依据当前工作副本、服务器实际运行配置以及 Docker/NVIDIA 官方文档修订，已进行部署信息脱敏。
>
> 本文面向 Linux 服务器上的单用户部署。应用与 Docker 均运行在服务器；本地电脑仅运行浏览器和 SSH 隧道。原 TXT 文件保留不变。

## 1. 服务器硬件与软件要求

### 1.1 CPU、内存、磁盘和显存

**三个预置案例、普通构图和图分析在调用远程模型 API 时，不要求 GPU，所需本机显存为 0 GB。** 当前服务器上的 Agent Runtime 和 Graph Engine 实际使用 CPU 版 PyTorch，容器中 `torch.cuda.is_available()` 为 `False`；主机即使装有显卡，也不会因此自动启用 GPU。

下表是根据当前服务结构和数据规模给出的容量规划建议，**不是经过逐档压测证明的最低配置或性能保证**。vCPU 指可用逻辑 CPU，内存指分配给此系统的可用资源，磁盘指安装前可用空间。

| 使用场景 | CPU 建议 | 内存建议 | 可用 SSD 空间建议 | GPU / 显存 |
| --- | --- | --- | --- | --- |
| 单用户轻量演示，仅三个预置案例，少量任务 | 4 vCPU 起 | 16 GB 起 | 50 GB 起 | 不需要，0 GB |
| 日常构图和分析，包含示例原始数据，可选小型 Neo4j | 8 vCPU 或以上 | 32 GB 或以上 | 100 GB 或以上 | 不需要，0 GB |
| arxiv 等较大图的加载、更多会话或较重图算法 | 16 vCPU 或以上 | 64 GB 或以上 | 200 GB 或以上 | 普通图算法仍可使用 CPU |
| 更大图、长时间批处理或较多并行任务 | 16–32 vCPU 起，按任务增加 | 128 GB 起，按实测峰值增加 | 500 GB 起，按快照和备份增长 | 取决于实际算法，不统一要求 |
| 可选 GPU 图学习、训练与推理 | 8–16 vCPU 起 | 32–64 GB 起 | 100–200 GB 起 | 小型图学习可从 8 GB 显存评估；较大任务建议 16–24 GB 或以上 |

需要注意：

- 构图全文、节点属性、图算法中间态和多个会话会增加内存；文件大小不能直接等同于运行内存。当前实例一次资源快照中，仅 Graph Engine API 就已占用约 5.7 GiB，因此不建议把 4 GB/8 GB 内存当作完整部署配置。
- 首次构建需要保存基础镜像、Python 计算依赖、前端视频与构建缓存。磁盘预算还应包含原始数据、图快照、临时文件和备份。
- 大图准备阶段当前默认磁盘预估放大系数为 `12`，还保留至少约 2 GiB 磁盘和部分内存；这不是所有图任务的容量上限。高复杂度算法仍需要单独评估。
- Agent 工具并行上限当前为 `3`，与图计算进程数是不同配置。多进程会增加内存开销；轻量服务器可将 `GRAPH_COMPUTE_CONCURRENCY` 降到 `1` 或 `2`。
- GPU 不会自动加速全部图工具。当前系统没有会话级 GPU 显存配额或 GPU 调度器。
- 若还要在同一服务器自行部署大语言模型，须另按模型参数量、精度、并发和上下文长度规划 GPU；上表的显存建议不包含本地大模型服务。把上下文设置为 300k 不会增加服务商实际支持的上下文容量。

### 1.2 操作系统与基础运行环境

| 项目 | 当前核实情况 / 建议 |
| --- | --- |
| CPU 架构 | 已验证 `x86_64 / amd64`；其他架构需要重新验证计算依赖和完整构建 |
| Linux | 已验证 Ubuntu Server 22.04.5 LTS，内核 5.15；新部署可选 Ubuntu 22.04 LTS 或 24.04 LTS 的更新版本 |
| Ubuntu 24.04 | Docker 官方支持；本次未重新执行 iGraph 全量安装验收，不能等同于已完成项目实测 |
| Docker Engine | 当前实例为 `29.8.1`，rootless 模式；建议采用组织维护的兼容稳定版本 |
| Docker Compose | 当前实例插件为 `5.5.1`；必须使用 `docker compose`，并支持 `--wait`、多个 `--env-file` 和当前 Compose 文件 |
| BuildKit / Buildx | 构建使用 `RUN --mount=type=cache`，需要支持该语法的构建环境 |
| 宿主机工具 | Git、SSH、curl、tar、Python 3.10 或以上、systemd 用户服务，以及 rootless 前置工具 |
| 本地电脑 | 浏览器与 OpenSSH 客户端即可，不需要安装本地 Docker、Python、CUDA 或 Node.js |

这里列出的 Docker/Compose 版本不是推断出的“最低支持版本”。旧版本若要使用，应先验证 `docker compose config --quiet`、构建和健康检查。Ubuntu 的 Docker 官方系统支持范围见 [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)。

### 1.3 容器内部依赖

这些依赖由 Dockerfile 安装，通常不应在宿主机逐一安装。

| 组件 | 当前代码指定版本 / 配置 |
| --- | --- |
| Python 基础镜像 | `python:3.12-slim`；当前运行容器为 Python `3.12.15` |
| 默认 CPU 计算栈 | PyTorch `2.12.1+cpu`、torchvision `0.27.1+cpu` |
| 可选 GPU 计算栈 | PyTorch `2.14.0+cu130`、torchvision `0.29.0+cu130` |
| 共用图计算依赖 | torch-geometric `2.8.0`、NumPy `2.5.1`、SciPy `1.17.1`、NetworkX `3.6.1` |
| 前端构建 | Node.js `22-alpine`、pnpm `10.28.1` |
| 前端服务 | Nginx `1.27-alpine` |
| 会话数据库 | `pgvector/pgvector:pg17` |
| 可选 Neo4j | `neo4j:5.26-community` |

版本依据为 `deploy/requirements/compute-cpu.txt`、`compute-gpu.txt`、各服务依赖文件、`frontend/Dockerfile` 与 Compose 文件。镜像标签可能更新补丁；严格复现时应额外固定镜像 digest 并保存构建产物。不要为了省事单独升级 Torch 而不检查 torchvision、PyG 和 Agent/Graph 共用环境的兼容性。

### 1.4 CUDA 与 NVIDIA 软件要求（仅 GPU 部署）

CPU 部署无需安装 NVIDIA 驱动、CUDA Toolkit、cuDNN 或 NVIDIA Container Toolkit。

启用当前 GPU 计算栈时：

1. 采用与 CUDA 13.0 及所选 GPU 兼容的驱动。CUDA 13.x 的驱动兼容基线为 **580 系列或更新版本**；具体补丁要求还应按 GPU 和发行版检查。参考 [NVIDIA CUDA Compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)。
2. 安装 NVIDIA Container Toolkit，并配置可被 rootless Docker 使用的 CDI 设备。建议使用支持自动生成 CDI 的 `1.18` 或更新兼容版本；不要只安装驱动就认为容器已获得 GPU。参考 [NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html)。
3. Docker 必须支持 CDI；Docker Engine 从 `28.3.0` 起默认启用 Linux CDI 支持。参考 [Docker device/CDI](https://docs.docker.com/reference/cli/docker/container/run/)。
4. 容器的 PyTorch CUDA wheel 安装相应用户态运行库。标准预编译包运行不要求宿主机另装相同版本的完整 CUDA Toolkit；如需编译自定义 CUDA 扩展，则需另行配置对应编译工具链。
5. `nvidia-smi` 顶部显示的 CUDA 版本是驱动支持能力，不能替代容器中的 `torch.version.cuda` 检查。

本次服务器检查确认主机装有 580 系列驱动，但当前应用采用 CPU 镜像，且未发现可用的 `nvidia-ctk` 命令。**本次没有将现有实例切换为 GPU，也没有对 GPU 路径重新验收。**

## 2. Linux 与 rootless Docker 准备

### 2.1 网络与权限

服务器需要访问所使用的代码仓库、容器镜像仓库、Python/Node 包源以及模型 API。若不具备公网下载条件，应事先准备完整源码、资源、镜像及离线依赖；仅复制前端文件不能部署完整系统。

以下系统安装操作由管理员执行；后续 Docker 应用操作由普通部署用户执行。

对于已配置 Docker 官方 apt 软件源的 Ubuntu，可安装：

```bash
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git openssh-client python3 uidmap \
  dbus-user-session slirp4netns fuse-overlayfs \
  docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras \
  docker-buildx-plugin docker-compose-plugin
```

如果尚未配置官方软件源，先按 [Docker Ubuntu 安装步骤](https://docs.docker.com/engine/install/ubuntu/) 完成。已有生产 Docker 环境应先核对，不能为了本文直接卸载或停止其他人的 daemon。

rootless 需要 `newuidmap` / `newgidmap`，且管理员应在 `/etc/subuid` 和 `/etc/subgid` 中为部署用户分配至少 65,536 个互不冲突的子 UID/GID。参考 [Docker rootless 前提](https://docs.docker.com/engine/security/rootless/)。

### 2.2 普通部署用户初始化

```bash
command -v newuidmap
command -v newgidmap
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
docker context use rootless
docker version
docker compose version
docker info --format '{{json .SecurityOptions}}'
```

最后一项应包含 `name=rootless`。已有 rootless 环境只需检查，不必重新安装。如果同一主机同时运行 rootful Docker，应依据官方说明选择 daemon，不应直接停止现有服务。

需要退出 SSH 后保持用户服务运行时，由管理员执行：

```bash
sudo loginctl enable-linger '<SSH_USER>'
```

后续不使用 `sudo docker`，避免连接到另一套 daemon。rootless 数据目录需要位于支持相应存储驱动的文件系统，并具有足够磁盘空间。

## 3. 获取完整发布代码和资源

以下示例项目名为 `igraph-demo`；这是新部署的通用名称。更新已有环境时，必须保留其原项目名、数据卷名、网络名和工作区归属 ID。

先在服务器 Bash 设置自己的目录：

```bash
export IGRAPH_APP_DIR='<APP_DIR>'
export IGRAPH_PROJECT='igraph-demo'
export IGRAPH_RELEASE_REF='<RELEASE_REF>'
```

确认这些值已替换，再选择一种方式：

**方式 A：已经确认远端可用的发布分支或标签。**

```bash
git clone --branch "$IGRAPH_RELEASE_REF" --single-branch \
  https://github.com/BUPT-GAMMA/iGraph.git "$IGRAPH_APP_DIR"
cd "$IGRAPH_APP_DIR"
git status --short
git rev-parse HEAD
```

克隆失败或版本不符时停止，不要自动退回旧分支继续部署。

**方式 B：维护者提供的完整发布包。**

先校验发布包摘要并确认归档内容；在一个新的空目录中解压。下面假定归档顶层直接包含 `compose.yaml`：

```bash
tar -tzf '<RELEASE_ARCHIVE>'
mkdir -p "$IGRAPH_APP_DIR"
tar -xzf '<RELEASE_ARCHIVE>' -C "$IGRAPH_APP_DIR"
cd "$IGRAPH_APP_DIR"
```

不要将未知来源归档覆盖到现有运行目录。发布包若有外层目录，按实际结构进入项目根目录。

### 3.1 完整性检查

```bash
test -f compose.yaml
test -f .env.example
test -f frontend/Dockerfile
test -f deploy/docker/agent-runtime.Dockerfile
test -f graph_engine/resources/finance_risk/manifest.json
test -f graph_engine/resources/borylation/manifest.json
test -f graph_engine/resources/tianlongbabu/manifest.json
test -f graph_engine/resources/example_inputs/manifest.json
test -f frontend/public/media/user-cases/financial-risk.mp4
test -f frontend/public/media/user-cases/molecular-graph.mp4
test -f frontend/public/media/user-cases/demi-gods-and-semi-devils.mp4
```

逐项确认成功后继续。manifest 只是元信息，还必须包含对应图文件、压缩源数据及 arxiv 分片；不要只复制 JSON 清单。

Neo4j 的可选部署配置为 `compose.neo4j.yaml`，Cora 导入脚本为 `deploy/neo4j/import_cora.py`，使用方式见第 9 节。

## 4. 首次生成配置

**仅在新环境执行本节。已有部署保留原 `.env`，不要重新生成密码、工作区 ID 或卷名。**

下面脚本从模板创建新的 `.env`；已存在时拒绝覆盖。数据库账号为通用示例账号，密码和内部服务密钥在本机随机生成，不打印到终端。

```bash
cd "$IGRAPH_APP_DIR"
umask 077
python3 - <<'PY'
from pathlib import Path
import os
import secrets

path = Path('.env')
if path.exists():
    raise SystemExit('.env 已存在：保留现有配置，停止初始化。')

db_user = 'app_user'
db_name = 'app_db'
db_password = secrets.token_hex(24)
values = {
    'POSTGRES_USER': db_user,
    'POSTGRES_DB': db_name,
    'POSTGRES_PASSWORD': db_password,
    'AGENT_RUNTIME_DATABASE_URL':
        f'postgresql+psycopg://{db_user}:{db_password}@postgres:5432/{db_name}',
    'IGRAPH_CONTROL_SERVICE_KEY': secrets.token_hex(32),
    'IGRAPH_ADMIN_USER_ID': 'workspace_owner',
    'FRONTEND_BIND_ADDRESS': '127.0.0.1',
    'FRONTEND_PORT': '21121',
    'IGRAPH_DATA_VOLUME': 'igraph-demo-data',
    'IGRAPH_POSTGRES_VOLUME': 'igraph-demo-postgres',
    'IGRAPH_BACKPLANE_NETWORK': 'igraph-demo-network',
    'BACKEND_SERVER_DATASETS_DIR': '/data/server_datasets',
    'AGENT_TOOL_MAX_CONCURRENCY': '3',
    'IGRAPH_GRAPH_ENGINE_IMAGE': 'igraph-graph-engine:release',
    'IGRAPH_GRAPH_ENGINE_MCP_IMAGE': 'igraph-graph-engine-mcp:release',
    'IGRAPH_AGENT_RUNTIME_IMAGE': 'igraph-agent-runtime:release',
    'IGRAPH_BACKEND_IMAGE': 'igraph-backend:release',
    'IGRAPH_FRONTEND_IMAGE': 'igraph-frontend:release',
}
lines = [line for line in Path('.env.example').read_text(encoding='utf-8').splitlines()
         if line.split('=', 1)[0].strip() not in values]
text = '\n'.join(lines + [f'{key}={value}' for key, value in values.items()]) + '\n'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as stream:
    stream.write(text)
print('.env 已生成，未输出任何凭据。')
PY
chmod 600 .env
```

- `IGRAPH_CONTROL_SERVICE_KEY` 用于服务间认证，不是模型 API Key；相关服务必须使用同一个值。
- 修改 `.env` 后需要 `docker compose up -d` 重建受影响容器；仅 `restart` 不会更新容器环境变量。
- PostgreSQL 已初始化后，修改 `.env` 中密码不会自动修改数据库内账号密码。
- `IGRAPH_ADMIN_USER_ID` 决定数据归属；更新或恢复旧数据卷时不得随意更换。
- `.env`、`.env.neo4j`、私钥、数据卷和含凭据的备份不应提交到代码仓库。

## 5. 统一 Compose 参数、构建和启动

### 5.1 CPU 基础部署

在项目目录定义本次使用的命令。新开 SSH 终端后重新设置变量并定义函数。若需要镜像加速，在执行 `dc build` 前先完成第 5.2 节的覆盖配置：

```bash
cd "$IGRAPH_APP_DIR"
IGRAPH_ENV_ARGS=(--env-file .env)
IGRAPH_COMPOSE_FILES=(-f compose.yaml)
dc() {
  docker compose "${IGRAPH_ENV_ARGS[@]}" -p "$IGRAPH_PROJECT" \
    "${IGRAPH_COMPOSE_FILES[@]}" "$@"
}

dc config --quiet
dc build
dc up -d --no-build --wait --wait-timeout 180
dc ps
curl -fsS http://127.0.0.1:21121/health
curl -fsS http://127.0.0.1:21121/api/workspace
```

逐条确认命令成功后再继续。`--wait-timeout 180` 只控制启动等待，不限制构图或模型问答时长。大型资源初始化耗时较长时，先看日志，再按需增加等待时间。

基础部署为 7 个服务：`frontend`、`backend`、`agent-runtime`、`graph-engine-api`、`graph-engine-worker`、`graph-engine-mcp`、`postgres`。

有健康检查的服务应为 `healthy`；独立 `graph-engine-worker` 当前没有 HTTP 健康检查，预期为运行状态，不能据此宣称其每类计算任务都已验收。`/api/workspace` 应显示 `single_user=true`，归属 ID 与自己的配置一致。

### 5.2 镜像源与其他覆盖文件

当前部署使用的是 **DaoCloud 公共镜像加速服务**，镜像域名为 `docker.m.daocloud.io`。下面列出部署时用到的地址，以及同一服务支持的另一种镜像地址写法。

| 用途 | 网站 / 地址 | 使用方式 |
| --- | --- | --- |
| 当前部署使用的镜像服务 | [DaoCloud Docker 镜像入口](https://docker.m.daocloud.io) | 把 `docker.io` 替换为 `docker.m.daocloud.io` |
| 同一服务的可选地址形式 | [DaoCloud 统一镜像入口](https://m.daocloud.io) | 在完整镜像名前添加 `m.daocloud.io/`，例如 `m.daocloud.io/docker.io/library/python:3.12-slim` |
| Docker Hub 官方镜像站 | [Docker Hub](https://hub.docker.com) | 网络可直连时使用项目默认镜像，如 `python:3.12-slim` |
| 镜像服务使用说明 | [DaoCloud public-image-mirror](https://github.com/DaoCloud/public-image-mirror) | 查看支持的镜像源、地址规则及服务状态入口 |

DaoCloud 官方目前推荐添加前缀的方式，也支持当前部署使用的域名替换方式。两者选一种即可，不要重复叠加；具体规则见 [DaoCloud 使用说明](https://github.com/DaoCloud/public-image-mirror#使用方法)。

镜像域名用于 Docker 拉取，不是普通下载网页。浏览器访问根路径或 `/v2/` 返回认证提示，并不等同于镜像不可用；是否可用应以实际镜像元数据查询、拉取或构建结果为准。

**步骤一：在项目根目录创建 `compose.server.yaml`。** 这是部署者维护的可选覆盖文件；如果已经存在，应合并以下配置，保留其他设置。以下内容与当前服务器使用的镜像覆盖配置一致：

```yaml
services:
  postgres:
    image: docker.m.daocloud.io/pgvector/pgvector:pg17
  graph-engine-api:
    build:
      args:
        PYTHON_IMAGE: docker.m.daocloud.io/library/python:3.12-slim
  graph-engine-worker:
    build:
      args:
        PYTHON_IMAGE: docker.m.daocloud.io/library/python:3.12-slim
  graph-engine-mcp:
    build:
      args:
        PYTHON_IMAGE: docker.m.daocloud.io/library/python:3.12-slim
  agent-runtime:
    build:
      args:
        PYTHON_IMAGE: docker.m.daocloud.io/library/python:3.12-slim
  backend:
    build:
      args:
        PYTHON_IMAGE: docker.m.daocloud.io/library/python:3.12-slim
  frontend:
    build:
      args:
        NODE_IMAGE: docker.m.daocloud.io/library/node:22-alpine
        NGINX_IMAGE: docker.m.daocloud.io/library/nginx:1.27-alpine
```

官方镜像 `python`、`node` 和 `nginx` 使用 `library/` 命名空间；`pgvector/pgvector` 保留原命名空间。Graph Engine API 和 Worker 共用镜像，两者基础镜像配置应保持一致。

**步骤二：将覆盖文件加入第 5.1 节已经定义的 `dc` 命令。** 在同一终端中只添加一次：

```bash
IGRAPH_COMPOSE_FILES+=(-f compose.server.yaml)
dc config --quiet
```

如需先确认镜像能被访问，可执行只读取元数据的检查：

```bash
docker manifest inspect docker.m.daocloud.io/library/python:3.12-slim > /dev/null
docker manifest inspect docker.m.daocloud.io/library/node:22-alpine > /dev/null
docker manifest inspect docker.m.daocloud.io/library/nginx:1.27-alpine > /dev/null
docker manifest inspect docker.m.daocloud.io/pgvector/pgvector:pg17 > /dev/null
```

检查成功表示对应镜像元数据可读取；完整镜像层下载仍以构建结果为准。CPU 部署继续执行：

```bash
dc build
dc up -d --no-build --wait --wait-timeout 180
dc ps
```

GPU 部署按第 10 节分服务构建，不执行上述不限定服务的 `dc build`。

**步骤三：可选 Neo4j 镜像。** 使用第 9 节 Neo4j 部署时，可在私有 `.env.neo4j` 中新增或修改下面这一项，保留已有密码配置：

```dotenv
IGRAPH_NEO4J_IMAGE=docker.m.daocloud.io/library/neo4j:5.26-community
```

当前 Neo4j 也使用该镜像站，并在部署配置中锁定了镜像 digest。上面的固定版本标签便于首次部署；需要严格复现时可使用经过确认的 `镜像名@sha256:摘要`。

以上方式直接在项目中指定镜像地址，rootless Docker 同样适用，无需为这一步修改全局 Docker daemon 设置。它只解决容器镜像获取，不会同时替换 apt、pip、pnpm 或模型 API 的地址。

之后 `build`、`up`、`ps`、`logs`、备份和恢复必须使用同一组环境文件、覆盖文件和项目名。不要公开输出完整 `docker compose config`，其中可能包含展开后的凭据。

### 5.3 CPU 版本验证

```bash
dc exec -T graph-engine-api python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())'
dc exec -T agent-runtime python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())'
```

默认配置应显示 CPU wheel、`None` 和 `False`。这属于预期结果，不是安装失败。

## 6. 通过 SSH 在本地访问前端

本节命令在本地 PowerShell 执行。先替换占位符；不要把私钥内容粘贴到网页或聊天中。

```powershell
$igraphServer = '<SERVER_HOST>'
$igraphSshPort = '<SSH_PORT>'
$igraphSshUser = '<SSH_USER>'
$igraphKeyPath = '<SSH_KEY_PATH>'

ssh -N `
  -L 127.0.0.1:21130:127.0.0.1:21121 `
  -p $igraphSshPort -i $igraphKeyPath `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
  "$igraphSshUser@$igraphServer"
```

保持该终端开启，然后打开 [iGraph 本地入口](http://127.0.0.1:21130/)。首次连接应核对服务器主机指纹后再信任，不要通过关闭主机密钥检查绕过验证。

- `21121` 是服务器前端监听端口，`21130` 是本机转发端口，两者职责不同。
- 本地端口已被正常隧道占用时直接使用现有隧道；需另建连接时可把命令最左侧端口改为 `21131`，浏览器相应访问该端口。
- 电脑休眠、网络中断或终端关闭会使隧道断开；恢复网络后重新运行上述命令。保活参数用于探测断线，不等于自动重连。
- 当前单用户模式没有登录注册保护。保留回环绑定和 SSH 访问；若需要公网使用，应另部署 HTTPS、访问认证及必要的访问限制，不能仅改为监听所有地址就当作完成公网部署。

## 7. 配置模型服务与构图模型

### 7.1 图分析模型

1. 打开网页右上角“模型服务”，添加提供商。
2. 根据服务商实际协议选择接口类型。通过 OpenAI 兼容网关访问其他厂商模型时，应选择兼容接口；模型名称本身不能决定协议。
3. 填写自己的 `<MODEL_API_BASE_URL>` 和 `<MODEL_API_KEY>`，保存连接。常见基础 URL 包含 `/v1`，不要填入完整 `/chat/completions` 请求路径。
4. 添加服务商实际支持的 `<MODEL_ID>`，确认工具调用能力，并按服务商限制设置上下文与最大输出。
5. 点击“检测”，通过后设为默认；在聊天输入区确认模型选择，发送一个简单图统计问题验证。

分析服务 URL 和 Key 默认留空。保存后重新打开不会回显原始凭据；留空通常表示保留已有值，清除密钥需使用界面的明确清除操作。展示名称、默认模型和候选模型随版本及服务商变化，本文不绑定任何私人网关。

### 7.2 构图模型

1. 打开“创建你的图”，上传 CSV、SQL 或 TXT，或选择已有构图数据源。
2. 在数据源页面的“构图模型配置”中选择模型；使用其他模型时选“自定义模型…”并填写实际模型 ID。
3. 填写构图 API URL 和 Key，点击“保存模型配置”。此入口使用 OpenAI 兼容聊天接口；只填写域名时当前实现会补齐 `/v1`，仍应以服务商文档验证。
4. TXT 小说等自由文本选择“智能抽取实体与关系（LLM）”或“小说人物关系”预设。自动模式可能按行读取，不能保证自动抽取出人物关系。
5. CSV/SQL 默认规则构图；启用 Agent 优化或 TXT 智能抽取后才需要对应模型能力。
6. 点击“开始构图”。完成后检查草稿，并按页面按钮保存到数据集；只有保存的构图结果才用于后续图分析。

分析连接与构图连接是两个独立入口。构图连接为单用户工作区共享配置，不是单个数据集私有配置。页面保存值优先；未保存完整连接时可回退到服务器环境变量。

需要服务器默认构图配置时，在私有 `.env` 中填写：

```dotenv
GRAPH_CONSTRUCTION_MODEL=<MODEL_ID>
GRAPH_CONSTRUCTION_BASE_URL=<MODEL_API_BASE_URL>
GRAPH_CONSTRUCTION_API_KEY=<MODEL_API_KEY>
```

替换以上占位符后执行 `dc up -d --no-build --wait`。不要把真实连接写入公开说明或示例配置。重新选择模型不会自动重建已保存的示例图。

## 8. 预存案例、原始数据与页面验收

### 8.1 首页的三个案例

“新建任务”首页标题现在为“从这里开始分析”。当前版本的三个示例为：

| 示例标题 | 保存图规模 | 说明 |
| --- | --- | --- |
| 金融股权穿透分析 | 19,436 个节点、26,872 条有向边 | 三表 PK/FK 图，保留机构、个人和持股记录节点 |
| 化学分子性质分析 | 22 个重原子、24 条无向化学键 | 底层双向存储为 48 条边条目；只加载 BO-01 第一张分子图 |
| 文学人物关系网络 | 304 个节点、715 条有向关系 | 《天龙八部》人物关系图，包含后续纠错和关系方向修订 |

这些数字来自当前资源 manifest，不是对任意重新构图结果的保证。尤其小说早期抽取记录中的 791 条边不是当前修订后保存图的边数。

分别检查：

1. 三个卡片的标题、介绍及四个简短问题按钮能够显示；问号可查看完整问题。
2. “使用示例图”能进入对应图分析；点击示例问题可发起提问。
3. 图工作台可以显示对应图及工具返回的高亮结果。
4. 已完成一个演示问题后，输入框中的下一题提示可通过 Tab 填入；不应自动提交问题。
5. 再点“新建任务”，能够回到三个示例卡片页面。
6. “帮助 → 使用流程”包含从数据源到图分析的指导；“帮助 → 用户案例”可播放三个随前端镜像打包的视频。

### 8.2 构图数据源

完整发布包包含以下原始输入或配套资源：

- 金融风控 CSV 三表：`individual_info.csv`、`institution_main.csv`、`institution_shareholder_relationship.csv`，在界面中以组合数据源呈现。
- `stackexchange-graph-15000.sql`，用于体验 SQL 构图。
- `arxiv.pt`，以压缩分片随资源发布，包含 169,343 个节点和 2,315,598 条边条目；它不是首页三个案例之一。
- 《天龙八部》原始 TXT 及已保存的人物关系图。
- 化学案例第一张分子结构图和配套背景元数据。

资源分别位于仓库的 `graph_engine/resources/finance_risk`、`borylation`、`tianlongbabu` 和 `example_inputs`。首次安装会将相关资源放入持久存储；构图数据源通常在首次访问数据列表时初始化。

“创建你的图 → 我的构图数据源”应能看到对应条目。自定义上传文件会创建独立数据源；仅同名不会覆盖预置图。当前实例另行上传的独立“天龙八部”TXT、用户新建项目、历史会话和自建图属于运行数据，**不会因为存在于当前服务器就自动进入代码仓库或全新部署**；迁移这些内容需要恢复数据备份。

不要将旧的金融股权示例目录仍留在源码中，误认为首页还应该显示第四个金融案例。具体可见案例以当前前后端案例注册和发布资源为准。

### 8.3 语言、流式输出与持久化

切换中文/EN 后，系统文案和内置示例问题应跟随界面语言；新生成的模型回复按当前界面语言请求。历史消息不自动翻译，模型本身仍可能不完全遵循指令，因此应实际验收新一轮回复。

当前分析结果使用流式事件传输；模型响应、工具耗时、代理缓冲等仍影响可见输出节奏。提示词要求先给简短结论，再展开分析，但不应把提示词要求写成每次均严格满足的格式保证。

保存一份构图、一个会话和模型配置后，使用同一 `dc` 参数重建容器验证持久化：

```bash
dc up -d --no-build --force-recreate --wait --wait-timeout 180
```

应在任务结束后的维护窗口执行。刷新后检查已保存图、会话和模型配置仍然存在。不要使用删除数据卷的方式模拟重启。

## 9. 可选：Neo4j 与 Cora 数据源

### 9.1 接入已有 Neo4j

在“创建你的图”或数据集面板点击“新增 Neo4j 数据集”：

1. 填写 `<NEO4J_URI>`、`<NEO4J_USER>`、`<NEO4J_PASSWORD>` 和 `<NEO4J_DATABASE>`。
2. 检查连接并读取 schema，选择节点标签和关系类型。
3. 按图语义选择有向/无向，并选择合适的节点名称属性。
4. 保存连接，选择使用项目，再加载进行图分析。
5. 数据库变更后使用“从 Neo4j 刷新”读取新快照。

连接由服务器的 Graph Engine 容器发起，地址必须从该容器可达。浏览器的本地 SSH 地址不是数据库地址；图分析中的编辑不会自动写回 Neo4j。

当前通用读取路径限制节点数、关系数和属性大小，超限会报错而非静默截断；以界面显示的部署上限为准。通用读取也不会自动把任意属性解释成训练特征或数据划分。

### 9.2 部署随应用运行的 Cora Neo4j

这是可选第 8 个服务。启动空 Neo4j 容器不等于已经导入 Cora；应自备可信的 `cora.pt`，不能依赖原测试机器上的私人目录。

在项目根目录创建私有配置，不覆盖已有文件：

```bash
umask 077
python3 - <<'PY'
from pathlib import Path
import os
import secrets
p = Path('.env.neo4j')
if p.exists():
    raise SystemExit('.env.neo4j 已存在，保留原配置。')
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write('IGRAPH_NEO4J_PASSWORD=' + secrets.token_hex(32) + '\n')
PY
chmod 600 .env.neo4j
```

为当前终端的 `dc` 加入配置；每个选项只添加一次：

```bash
IGRAPH_ENV_ARGS+=(--env-file .env.neo4j)
IGRAPH_COMPOSE_FILES+=(-f compose.neo4j.yaml)
dc config --quiet
dc build graph-engine-api backend frontend
dc up -d --no-build --wait --wait-timeout 180 neo4j-cora

export IGRAPH_CORA_DIR='<CORA_DIR>'
test -f "$IGRAPH_CORA_DIR/cora.pt"
dc run --rm --no-deps -v "$IGRAPH_CORA_DIR:/import:ro" \
  graph-engine-api python deploy/neo4j/import_cora.py /import/cora.pt

dc up -d --no-build --wait --wait-timeout 180
dc ps
```

以上构建方式针对 CPU 部署；GPU 部署使用第 10 节的统一计算栈构建方式。配置内置账号由 Neo4j Compose 文件定义，本文不展示实际凭据；已有数据库不能通过更换环境变量密码直接完成改密。

内置数据库在 Docker 网络中的地址为 `bolt://neo4j-cora:7687`。需要本地 Neo4j Browser 时，可额外转发 `27474 → 27474` 和 `27687 → 27687`，然后访问 [Neo4j Browser 本地入口](http://127.0.0.1:27474/)。这些端口当前仅绑定服务器回环地址。

Neo4j 的持久卷必须额外备份，不能只备份 iGraph 的 `/data`。当前示例配置中 Neo4j 最大堆为 1 GiB，page cache 为 512 MiB；它只是小型 Cora 的配置，不代表大数据库的资源需求。

## 10. 可选：切换为 GPU 计算栈

仅在第 1.4 节条件全部满足、且确实需要 GPU 的情况下操作。普通三个案例不需要本节。

1. 按 [NVIDIA Container Toolkit 安装说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) 安装适配的软件。
2. 使用 CDI 方式检查设备可见性；容器运行用户也必须有设备访问权限。

```bash
nvidia-smi
nvidia-ctk --version
nvidia-ctk cdi list
```

3. 为当前 `dc` 增加 GPU 覆盖文件：

```bash
IGRAPH_COMPOSE_FILES+=(-f compose.gpu.yaml)
dc config --quiet
```

4. 按以下顺序构建，避免对同名 Graph Engine 镜像分别构建 CPU/GPU 两种版本：

```bash
dc build graph-engine-api agent-runtime
dc build graph-engine-mcp backend frontend
dc up -d --no-build --wait --wait-timeout 180
```

当前 `compose.gpu.yaml` 只给 API 和 Agent Runtime 配置 GPU 计算依赖及 CDI 设备；Worker 共用 API 镜像，但没有配置 GPU 设备。不要在此组合下直接执行无服务限制的 `dc build`，以免 Worker 的默认 CPU 构建参数覆盖同名图引擎镜像。大图 Worker 仍按其现有 CPU 计算路径运行。

5. 在两个实际使用 GPU 的服务中分别验证：

```bash
dc exec -T graph-engine-api python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); assert torch.cuda.is_available(); print(torch.ones(1, device="cuda"))'
dc exec -T agent-runtime python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); assert torch.cuda.is_available(); print(torch.ones(1, device="cuda"))'
```

应显示 CUDA 13.0 对应计算栈、`True` 并成功分配 CUDA 张量。

## 11. 更新现有部署

### 11.1 先记录并备份

记录当前代码版本或发布包摘要、镜像 ID、Compose 项目名、环境文件、覆盖文件、卷名和网络名。先执行第 12 节备份，安排任务结束后的维护窗口。

当前部署可能来自归档而没有 `.git`，此时不能在应用目录直接 `git pull`。即使是 Git 检出，也应先检查是否有未提交修改。

### 11.2 Git 源码方式

以下假定独立源码目录已建立且工作树干净；首次获取按第 3 节执行：

```bash
export IGRAPH_SOURCE_DIR='<SOURCE_DIR>'
git -C "$IGRAPH_SOURCE_DIR" status --short
git -C "$IGRAPH_SOURCE_DIR" fetch origin
git -C "$IGRAPH_SOURCE_DIR" switch '<RELEASE_BRANCH>'
git -C "$IGRAPH_SOURCE_DIR" pull --ff-only origin '<RELEASE_BRANCH>'
git -C "$IGRAPH_SOURCE_DIR" rev-parse HEAD
```

存在未提交内容时先保存并确认如何合并；不要执行强制重置。标签发布则检出指定标签，不执行分支 `pull`。

### 11.3 导出到干净发布目录

```bash
export IGRAPH_NEW_RELEASE_DIR='<NEW_RELEASE_DIR>'
test ! -e "$IGRAPH_NEW_RELEASE_DIR"
mkdir -p "$IGRAPH_NEW_RELEASE_DIR"
git -C "$IGRAPH_SOURCE_DIR" archive --format=tar \
  -o '<RELEASE_TAR_PATH>' HEAD
tar -xf '<RELEASE_TAR_PATH>' -C "$IGRAPH_NEW_RELEASE_DIR"
cp "$IGRAPH_APP_DIR/.env" "$IGRAPH_NEW_RELEASE_DIR/.env"
```

逐条检查成功；`git archive` 导出所选提交中的源码和资源，运行数据通过第 12 节的备份方式迁移。发布包方式也应解压到新的空目录。

复制实际使用的私有 `.env.neo4j` 和部署覆盖文件，保留同一项目名、数据卷名、网络名及工作区归属 ID；确认这些配置未被新代码覆盖。新的发布目录必须通过第 3.1 节检查。

进入新目录后重新定义第 5 节的 `dc` 及所有已启用的覆盖选项。CPU 部署执行 `dc build`；GPU 部署按第 10 节分服务构建。构建成功再执行：

```bash
dc config --quiet
dc up -d --no-build --wait --wait-timeout 180
dc ps
curl -fsS http://127.0.0.1:21121/health
```

全部成功后记录新的 `DEPLOYED_COMMIT` 或发布包摘要，并更新运维记录中的当前应用目录。浏览器强制刷新，按第 8 节验收。仅修改 Compose 项目名不足以隔离显式命名的数据卷和网络。

## 12. 备份、恢复与回滚

### 12.1 必须保存哪些内容

| 内容 | 用途 |
| --- | --- |
| iGraph 数据卷 | 图、原始输入、工作区、构图状态及部分私有配置 |
| PostgreSQL 逻辑备份 | 运行时会话、消息、模型等数据库状态 |
| Neo4j 数据卷（如启用） | 原始数据库图及数据库状态 |
| 私有配置与覆盖文件 | `.env`、`.env.neo4j`、部署覆盖参数 |
| 对应源码和镜像信息 | 恢复到与备份数据兼容的版本 |

备份含凭据和业务数据，应存入受限目录；分享本文不等于可以公开备份文件。

### 12.2 一致性备份示例

以下命令在项目目录、已经正确定义 `dc` 的服务器 Bash 执行。它会短暂停止应用写入；若有 Neo4j，也在停止后复制其卷。请先等待任务结束。

```bash
export IGRAPH_BACKUP_ROOT='<BACKUP_ROOT>'
(
  set -euo pipefail
  umask 077
  backup_dir="$IGRAPH_BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup_dir"
  chmod 700 "$backup_dir"

  backend_id=$(dc ps -q backend)
  graph_id=$(dc ps -q graph-engine-api)
  test -n "$backend_id"
  test -n "$graph_id"
  data_volume=$(docker inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$backend_id")
  helper_image=$(docker inspect --format '{{.Config.Image}}' "$graph_id")
  test -n "$data_volume"

  neo_volume=''
  if dc config --services | grep -qx 'neo4j-cora'; then
    neo_id=$(dc ps -q neo4j-cora)
    test -n "$neo_id"
    neo_volume=$(docker inspect --format \
      '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$neo_id")
    test -n "$neo_volume"
  fi

  for f in .env .env.neo4j compose.yaml compose.server.yaml compose.neo4j.yaml compose.gpu.yaml DEPLOYED_COMMIT; do
    if [ -f "$f" ]; then cp "$f" "$backup_dir/"; fi
  done
  # 如还使用其他覆盖文件，请一并复制并记录参数。
  docker inspect --format '{{.Image}}' "$graph_id" > "$backup_dir/graph-image-id.txt"

  trap 'dc up -d --no-build --wait --wait-timeout 180' EXIT
  dc stop frontend backend agent-runtime graph-engine-mcp graph-engine-worker graph-engine-api
  if [ -n "$neo_volume" ]; then dc stop neo4j-cora; fi

  dc exec -T postgres sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/postgres.dump"

  docker run --rm --user root --entrypoint sh \
    -v "$data_volume:/source:ro" -v "$backup_dir:/backup" "$helper_image" \
    -c 'tar --numeric-owner -czf /backup/data.tar.gz -C /source .'

  if [ -n "$neo_volume" ]; then
    docker run --rm --user root --entrypoint sh \
      -v "$neo_volume:/source:ro" -v "$backup_dir:/backup" "$helper_image" \
      -c 'tar --numeric-owner -czf /backup/neo4j-data.tar.gz -C /source .'
    tar -tzf "$backup_dir/neo4j-data.tar.gz" > /dev/null
  fi

  test -s "$backup_dir/postgres.dump"
  tar -tzf "$backup_dir/data.tar.gz" > /dev/null
  chmod 600 "$backup_dir"/*
  dc up -d --no-build --wait --wait-timeout 180
  trap - EXIT
  printf '备份完成：%s\n' "$backup_dir"
)
```

这里的容器内 `--user root` 用于保留卷中文件权限，并未切换成宿主机 rootful Docker。命令针对命名卷；若自行改成宿主机目录绑定，应另按实际挂载备份。打包通过只证明文件可读取，仍应通过隔离恢复确认备份可用。

### 12.3 恢复到隔离环境

1. 使用与备份匹配的完整代码和镜像，在新目录恢复配置；保留原数据库凭据和工作区归属 ID。
2. 为恢复实例设置独立项目名、iGraph 数据卷、PostgreSQL 卷和网络；前端改用例如 `21122`。如有 Neo4j，数据库卷及 HTTP/Bolt 映射端口也必须独立。
3. 定义恢复实例的 `dc`，确认所有参数指向恢复环境；不要复用生产 `dc` 函数后直接执行导入。
4. 使用空数据卷，解压 iGraph 数据归档。例如：

```bash
export IGRAPH_BACKUP_DIR='<BACKUP_DIR>'
export IGRAPH_RESTORE_DATA_VOLUME='<RESTORE_DATA_VOLUME>'
export IGRAPH_HELPER_IMAGE='<GRAPH_ENGINE_IMAGE>'
docker volume create "$IGRAPH_RESTORE_DATA_VOLUME"
docker run --rm --user root --entrypoint sh \
  -v "$IGRAPH_RESTORE_DATA_VOLUME:/restore" \
  -v "$IGRAPH_BACKUP_DIR:/backup:ro" "$IGRAPH_HELPER_IMAGE" \
  -c 'tar --numeric-owner -xzf /backup/data.tar.gz -C /restore'
```

5. 只启动使用空数据库卷的 PostgreSQL，然后恢复逻辑备份：

```bash
dc up -d --no-build --wait postgres
dc exec -T postgres sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$IGRAPH_BACKUP_DIR/postgres.dump"
```

6. 若备份包含 Neo4j，使用与备份相同的 Neo4j 镜像版本，在数据库停止状态下向空 Neo4j 卷恢复 `neo4j-data.tar.gz`；不能把物理数据库文件任意复制到其他大版本。
7. 启动其余服务，检查健康状态，通过新隧道转发本机 `21131` 到恢复服务器的 `21122`。
8. 核对图、会话、模型配置、源数据和可选数据库连接后，再决定是否切换入口。

恢复前应确认目标卷确实为空，不能向正在使用的卷直接解压。不要对现用环境执行 `down -v` 或删除数据卷。

### 12.4 回滚

保留上一版干净发布目录、镜像和配置。代码回滚前核查是否发生数据库或数据格式迁移；不能默认所有版本都能仅靠回滚代码恢复。必要时使用上一版一致性备份恢复到隔离实例后切换。

## 13. 常见故障

### 13.1 网页打不开

服务器执行：

```bash
dc ps
curl -fsS http://127.0.0.1:21121/health
dc logs --tail 100 frontend backend
```

服务器正常、本机无法访问时，重新建立第 6 节隧道；确认本地端口没有冲突。前端健康接口只证明 Nginx 可访问，还需通过页面数据加载或 `/api/workspace` 检查后端链路。

### 13.2 Docker 看不到原容器或提示无权限

检查 `docker context show`、`docker info` 和 `systemctl --user status docker`。不要混用部署用户、rootful daemon 或其他 `DOCKER_HOST`。使用正确用户不代表可以修改其他部署的数据卷。

### 13.3 服务 unhealthy、构建失败或内存不足

```bash
dc logs --tail 100 backend agent-runtime graph-engine-api graph-engine-mcp
docker stats --no-stream
free -h
df -h
```

核对内部服务密钥、数据库凭据与现有卷是否匹配；检查镜像下载、锁定依赖是否可用，以及数据盘/容器存储盘空间。发布版本中某个 wheel 不可获取时应停止并由维护者修正版本清单，不应随意删除版本约束继续部署。

### 13.4 模型报错或没有流式输出

- `401/403`：核对服务商认证和权限；额度不足按服务商错误信息处理。
- `404`：核对模型 ID、API 基础路径和接口类型。
- 能聊天但工具失败：单独测试工具调用和构图所用协议。
- 一次出现大段文本：检查上游是否流式返回，以及额外反向代理是否缓冲 SSE；不要仅凭前端健康检查判断流式链路正常。

### 13.5 TXT 构图等待时间长或结果不合预期

确认选择了所需处理模式、配置已经保存且原文有效。自由文本关系抽取需要 LLM；默认逐行处理不等同于小说人物关系抽取。全文分块处理可能耗时较长，页面关闭不会主动取消后台任务；重新打开查看状态，避免反复提交相同任务。

### 13.6 Neo4j 连接失败

先检查连接地址是否从 Graph Engine 容器可达，再检查密码、数据库名及只读权限。浏览器的 `127.0.0.1` 和容器内的 `127.0.0.1` 指向不同网络空间。数据库启动成功但未导入 Cora 时，不能把空图误认为已加载示例。

### 13.7 示例不见或恢复后数据为空

确认使用了正确发布包、完整资源、原数据卷和工作区归属 ID。检查图引擎及后端日志。已删除过的内置源可能被安装标记阻止自动重装，不应通过反复重启或删除全部数据卷处理。

