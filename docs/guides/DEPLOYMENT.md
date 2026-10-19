# iGraph Deployment Guide

> Deployment reference updated: October 17, 2026. English edition: October 19, 2026.
> This guide translates the supplied deployment notes, which were prepared from the inspected working copy, server configuration, and Docker/NVIDIA documentation. Server details and credentials have been redacted. Recorded versions and observations describe that deployment; this documentation update does not constitute a fresh deployment or GPU validation.
>
> This guide covers a single-user deployment on a Linux server. The application and Docker run on the server; your local computer only needs a browser and an SSH client. Paths below follow the repository layout. The original Chinese reference remains available as [DEPLOYMENT.zh-CN.md](DEPLOYMENT.zh-CN.md).

## 1. Hardware and software requirements

### 1.1 CPU, memory, storage, and GPU memory

**The three bundled examples, ordinary graph construction, and graph analysis require no GPU when using a remote model API: local GPU memory required is 0 GB.** The inspected Agent Runtime and Graph Engine used CPU PyTorch, with `torch.cuda.is_available()` returning `False`. A GPU installed in the host does not enable GPU execution automatically.

These are capacity-planning recommendations based on the service layout and dataset sizes, **not load-tested minimum requirements or performance guarantees**. vCPU means an available logical CPU; memory and storage refer to resources available to this deployment.

| Use case | Recommended CPU | Recommended RAM | Available SSD space | GPU / GPU memory |
| --- | --- | --- | --- | --- |
| Light single-user demo with the three bundled examples and few tasks | 4+ vCPU | 16+ GB | 50+ GB | Not required; 0 GB |
| Regular graph construction and analysis, example inputs, optional small Neo4j instance | 8+ vCPU | 32+ GB | 100+ GB | Not required; 0 GB |
| Loading larger graphs such as arxiv, more sessions, or heavier algorithms | 16+ vCPU | 64+ GB | 200+ GB | Ordinary graph algorithms can still use CPU |
| Larger graphs, long batch jobs, or more concurrent tasks | Start with 16–32 vCPU; scale by workload | Start with 128 GB; measure peak usage | Start with 500 GB; allow for snapshots and backups | Depends on the algorithm |
| Optional GPU graph learning, training, and inference | 8–16+ vCPU | 32–64+ GB | 100–200+ GB | Evaluate 8 GB for small tasks; 16–24+ GB for larger workloads |

- Full input text, node attributes, algorithm intermediates, and concurrent sessions increase memory use. Input file size is not a memory estimate. One snapshot of the inspected deployment showed Graph Engine API alone using about 5.7 GiB, so 4–8 GB RAM should not be treated as a complete deployment configuration.
- Initial builds need space for base images, Python compute dependencies, frontend videos, and build caches. Also budget for source data, graph snapshots, temporary files, and backups.
- Large-graph preparation uses a default disk estimate multiplier of `12`, while reserving roughly 2 GiB of disk space and some memory. This is not a universal limit for graph tasks; expensive algorithms need separate assessment.
- The Agent tool concurrency limit is `3`, separate from graph compute process concurrency. Multiple processes use more memory; smaller servers can reduce `GRAPH_COMPUTE_CONCURRENCY` to `1` or `2`.
- A GPU does not accelerate every graph tool. The current system has no session-level GPU memory quota or GPU scheduler.
- If you also host a language model on this server, plan GPU capacity separately for model size, precision, concurrency, and context length. The table excludes local language-model serving. Setting a context length to 300k does not increase the provider's actual supported context.

### 1.2 Operating system and host environment

| Component | Recorded environment / recommendation |
| --- | --- |
| CPU architecture | `x86_64 / amd64` was verified; other architectures require compute-dependency and full-build validation |
| Linux | Ubuntu Server 22.04.5 LTS with kernel 5.15 was verified; an updated Ubuntu 22.04 LTS or 24.04 LTS is an option for new deployments |
| Ubuntu 24.04 | Supported by Docker; a complete iGraph installation was not revalidated on it in the supplied notes |
| Docker Engine | The inspected instance used `29.8.1` in rootless mode; use a compatible stable version maintained by your organization |
| Docker Compose | The inspected plugin was `5.5.1`; use `docker compose` with support for `--wait`, multiple `--env-file` options, and the supplied Compose files |
| BuildKit / Buildx | Builds use `RUN --mount=type=cache`; the build environment must support it |
| Host tools | Git, SSH, curl, tar, Python 3.10+, systemd user services, and rootless prerequisites |
| Local computer | A browser and OpenSSH client; no local Docker, Python, CUDA, or Node.js installation is required |

The recorded Docker/Compose versions are not inferred minimum supported versions. Validate older versions with `docker compose config --quiet`, a build, and health checks. See [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/) for Docker's supported operating systems.

### 1.3 Container dependencies

Dockerfiles install these dependencies; normally you do not install them individually on the host.

| Component | Version / configuration in the deployment reference |
| --- | --- |
| Python base image | `python:3.12-slim`; the inspected container ran Python `3.12.15` |
| Default CPU compute stack | PyTorch `2.12.1+cpu`, torchvision `0.27.1+cpu` |
| Optional GPU compute stack | PyTorch `2.14.0+cu130`, torchvision `0.29.0+cu130` |
| Shared graph compute dependencies | torch-geometric `2.8.0`, NumPy `2.5.1`, SciPy `1.17.1`, NetworkX `3.6.1` |
| Frontend build | Node.js `22-alpine`, pnpm `10.28.1` |
| Frontend server | Nginx `1.27-alpine` |
| Session database | `pgvector/pgvector:pg17` |
| Optional Neo4j | `neo4j:5.26-community` |

Version sources are `deploy/requirements/compute-cpu.txt`, `compute-gpu.txt`, the per-service dependency files, `frontend/Dockerfile`, and the Compose files. Image tags can receive patch updates; pin image digests and retain build artifacts for strict reproducibility. Do not upgrade Torch independently without checking torchvision, PyG, and the shared Agent/Graph environment.

### 1.4 CUDA and NVIDIA requirements (GPU deployments only)

CPU deployments do not require an NVIDIA driver, CUDA Toolkit, cuDNN, or NVIDIA Container Toolkit.

For the GPU compute stack described in this guide:

1. Use a driver compatible with CUDA 13.0 and your GPU. The CUDA 13.x compatibility baseline is the **580 series or newer**; also check patch requirements for your GPU and distribution. See [NVIDIA CUDA Compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html).
2. Install NVIDIA Container Toolkit and configure CDI devices accessible to rootless Docker. Use a compatible release supporting automatic CDI generation, such as `1.18` or newer. Installing a driver alone does not expose a GPU to containers. See [NVIDIA CDI](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).
3. Docker must support CDI. Docker Engine enables Linux CDI by default from `28.3.0`. See [Docker device/CDI](https://docs.docker.com/reference/cli/docker/container/run/).
4. PyTorch CUDA wheels install the corresponding user-space runtime libraries. Standard prebuilt packages do not require a complete matching CUDA Toolkit on the host; compiling custom CUDA extensions requires an appropriate toolchain.
5. The CUDA version at the top of `nvidia-smi` describes driver support. It does not replace checking `torch.version.cuda` inside the container.

The source deployment inspection found a 580-series host driver, CPU application images, and no available `nvidia-ctk` command. **That inspection did not switch the deployment to GPU or revalidate the GPU path.**

## 2. Prepare Linux and rootless Docker

### 2.1 Network access and permissions

The server needs access to the code repository, container registries, Python/Node package sources, and model APIs. For offline deployment, prepare complete source code, resources, images, and dependencies in advance. Copying frontend files alone cannot deploy the full system.

An administrator performs the system installation below. Run subsequent Docker application commands as the regular deployment user.

On Ubuntu with Docker's official apt repository already configured:

```bash
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git openssh-client python3 uidmap \
  dbus-user-session slirp4netns fuse-overlayfs \
  docker-ce docker-ce-cli containerd.io docker-ce-rootless-extras \
  docker-buildx-plugin docker-compose-plugin
```

If that repository is not configured, first follow [Docker's Ubuntu installation instructions](https://docs.docker.com/engine/install/ubuntu/). Check existing production Docker installations before making changes; do not uninstall or stop another deployment's daemon to follow this guide.

Rootless mode requires `newuidmap` and `newgidmap`. An administrator must allocate at least 65,536 non-overlapping subordinate UIDs/GIDs to the deployment user in `/etc/subuid` and `/etc/subgid`. See [Docker rootless prerequisites](https://docs.docker.com/engine/security/rootless/).

### 2.2 Initialize as the deployment user

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

The final command should include `name=rootless`. An existing rootless installation only needs inspection, not reinstallation. If the host also runs rootful Docker, select the appropriate daemon according to Docker's documentation instead of stopping existing services.

To keep user services running after SSH disconnects, ask an administrator to run:

```bash
sudo loginctl enable-linger '<SSH_USER>'
```

Do not use `sudo docker` for the following steps: it may connect to a different daemon. The rootless data directory must use a filesystem supported by the selected storage driver and have sufficient free space.

## 3. Obtain the complete release and resources

The example project name is `igraph-demo`. **When updating an existing deployment, preserve its project name, volume names, network name, and workspace owner ID.**

Set your own paths in Bash on the server:

```bash
export IGRAPH_APP_DIR='<APP_DIR>'
export IGRAPH_PROJECT='igraph-demo'
export IGRAPH_RELEASE_REF='<RELEASE_REF>'
```

Replace every placeholder before continuing, then choose one method.

**Option A: a release branch or tag confirmed to exist on the remote.**

```bash
git clone --branch "$IGRAPH_RELEASE_REF" --single-branch \
  https://github.com/BUPT-GAMMA/iGraph.git "$IGRAPH_APP_DIR"
cd "$IGRAPH_APP_DIR"
git status --short
git rev-parse HEAD
```

If cloning fails or the version is wrong, stop. Do not silently fall back to an older branch.

**Option B: a complete release archive supplied by a maintainer.**

Verify its checksum and inspect its contents, then extract it into a new empty directory. This example assumes `compose.yaml` is at the archive root:

```bash
tar -tzf '<RELEASE_ARCHIVE>'
mkdir -p "$IGRAPH_APP_DIR"
tar -xzf '<RELEASE_ARCHIVE>' -C "$IGRAPH_APP_DIR"
cd "$IGRAPH_APP_DIR"
```

Do not extract an untrusted archive over a running installation. If the archive contains a wrapper directory, enter the actual project root.

### 3.1 Check release completeness

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

Confirm each check succeeds before continuing. Manifests only describe resources: the corresponding graph files, compressed source data, and arxiv shards must also be present. Do not copy only the JSON manifests.

The source deployment also used `compose.neo4j.yaml` and `deploy/neo4j/import_cora.py` for optional Neo4j provisioning. **Those two files are not included in the `main` snapshot used for this documentation update.** Section 9.2 applies only to a complete release package that supplies them. Otherwise, use an existing Neo4j instance as described in section 9.1.

## 4. Create the initial configuration

**Run this section only for a new installation. Preserve the original `.env` when updating; do not regenerate passwords, workspace IDs, or volume names.**

This script creates `.env` from the template and refuses to overwrite an existing file. Database account names are generic examples. Passwords and the internal service key are generated locally without printing them.

```bash
cd "$IGRAPH_APP_DIR"
umask 077
python3 - <<'PY'
from pathlib import Path
import os
import secrets

path = Path('.env')
if path.exists():
    raise SystemExit('.env already exists: preserve the current configuration and stop initialization.')

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
print('.env created. No credentials were printed.')
PY
chmod 600 .env
```

- `IGRAPH_CONTROL_SERVICE_KEY` authenticates internal services; it is not a model API key. All participating services must use the same value.
- After editing `.env`, use `docker compose up -d` to recreate affected containers. `restart` does not update their environment variables.
- Changing a password in `.env` after PostgreSQL has initialized does not change the database account's password.
- `IGRAPH_ADMIN_USER_ID` determines data ownership. Preserve it when updating or restoring existing volumes.
- Do not commit `.env`, `.env.neo4j`, private keys, data volumes, or backups containing credentials.

## 5. Use consistent Compose options, build, and start

### 5.1 Base CPU deployment

Define the command for this deployment in the project directory. Re-establish the variables and function in each new SSH shell. If a registry mirror is required, apply section 5.2 before running `dc build`.

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

Check every command succeeds before continuing. `--wait-timeout 180` limits startup waiting, not graph construction or model requests. If initial resource installation takes longer, inspect the logs before increasing the timeout.

The base deployment has seven services: `frontend`, `backend`, `agent-runtime`, `graph-engine-api`, `graph-engine-worker`, `graph-engine-mcp`, and `postgres`.

Services with health checks should report `healthy`. The separate `graph-engine-worker` has no HTTP health check and should be running; that alone does not validate all its compute tasks. `/api/workspace` should report `single_user=true` and the configured workspace owner ID.

### 5.2 Registry mirrors and other overrides

The deployment reference used **DaoCloud's public image mirror**, `docker.m.daocloud.io`. The same service also supports a prefix-based form.

| Purpose | Service / address | Usage |
| --- | --- | --- |
| Mirror used in the reference deployment | [DaoCloud Docker mirror](https://docker.m.daocloud.io) | Replace `docker.io` with `docker.m.daocloud.io` |
| Alternative address form | [DaoCloud unified mirror](https://m.daocloud.io) | Prefix the full image name with `m.daocloud.io/`, for example `m.daocloud.io/docker.io/library/python:3.12-slim` |
| Official container registry | [Docker Hub](https://hub.docker.com) | Use the project's default image references when directly accessible |
| Mirror documentation | [DaoCloud public-image-mirror](https://github.com/DaoCloud/public-image-mirror) | Supported registries, address rules, and service status |

DaoCloud's documentation recommends the prefix form and also supports the domain replacement used here. Choose one form without combining them; see [DaoCloud's instructions](https://github.com/DaoCloud/public-image-mirror#%E4%BD%BF%E7%94%A8%E6%96%B9%E6%B3%95).

These domains serve Docker pulls, not ordinary download pages. An authentication response at `/` or `/v2/` does not by itself mean the mirror is unavailable. Check image metadata, pulls, or builds instead.

**Step 1: create `compose.server.yaml` in the project root.** This is an optional operator-maintained override. If it already exists, merge the settings while preserving other configuration. The following matches the mirror override in the source deployment:

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

Official `python`, `node`, and `nginx` images use the `library/` namespace; keep the original namespace for `pgvector/pgvector`. Graph Engine API and Worker share an image, so their base-image settings must match.

**Step 2: add the override to the `dc` command from section 5.1.** Add it only once in the current shell:

```bash
IGRAPH_COMPOSE_FILES+=(-f compose.server.yaml)
dc config --quiet
```

Optionally check image metadata before building:

```bash
docker manifest inspect docker.m.daocloud.io/library/python:3.12-slim > /dev/null
docker manifest inspect docker.m.daocloud.io/library/node:22-alpine > /dev/null
docker manifest inspect docker.m.daocloud.io/library/nginx:1.27-alpine > /dev/null
docker manifest inspect docker.m.daocloud.io/pgvector/pgvector:pg17 > /dev/null
```

Successful metadata checks do not guarantee every image layer can be downloaded. For CPU deployment, continue with:

```bash
dc build
dc up -d --no-build --wait --wait-timeout 180
dc ps
```

For GPU deployment, use the per-service build order in section 10 instead of an unrestricted `dc build`.

**Step 3: optional Neo4j image.** If using the Neo4j package described in section 9.2, add or update this value in the private `.env.neo4j`, preserving the password:

```dotenv
IGRAPH_NEO4J_IMAGE=docker.m.daocloud.io/library/neo4j:5.26-community
```

The reference Neo4j deployment also used this mirror and pinned its image digest. The version tag above is convenient for initial installation; use a verified `image@sha256:digest` for strict reproducibility.

These project-level image references also work with rootless Docker and do not require a global daemon change. They only affect container image downloads, not apt, pip, pnpm, or model API endpoints.

Use the same environment files, overrides, and project name for later builds, startup, status, logs, backups, and restoration. Do not publish the complete output of `docker compose config`: it may contain expanded credentials.

### 5.3 Verify CPU execution

```bash
dc exec -T graph-engine-api python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())'
dc exec -T agent-runtime python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())'
```

The default configuration should report a CPU wheel, `None`, and `False`. This is expected, not an installation failure.

## 6. Access the frontend through SSH

Run this section on your local computer in PowerShell. Replace the placeholders first; never paste a private key into a web page or chat.

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

Keep the terminal open and visit the [local iGraph workspace](http://127.0.0.1:21130/). Verify the server's host-key fingerprint on the first connection. Do not disable host-key checking to bypass verification.

- `21121` is the server's frontend port; `21130` is the local forwarded port.
- If an existing working tunnel already occupies the local port, use it. For a second tunnel, change the leftmost port to `21131` and use that port in the browser.
- Sleep, network loss, or closing the terminal can disconnect the tunnel. Run the command again after reconnecting. Keepalive options detect a lost connection; they do not reconnect automatically.
- Single-user mode has no sign-in or registration protection. Keep the loopback binding and SSH access. Public access requires HTTPS, authentication, and appropriate access restrictions; binding to all interfaces alone does not provide them.

## 7. Configure analysis and graph-construction models

### 7.1 Graph analysis model

1. Open **Model services** at the top right of the workspace and add a provider.
2. Select the interface type that matches the provider's actual protocol. If another vendor's model is accessed through an OpenAI-compatible gateway, choose the compatible interface. The model name alone does not determine the protocol.
3. Enter your `<MODEL_API_BASE_URL>` and `<MODEL_API_KEY>`, then save. A typical base URL includes `/v1`; do not enter the complete `/chat/completions` request path.
4. Add a `<MODEL_ID>` actually supported by the provider, confirm tool-calling support, and set context/output limits within the provider's constraints.
5. Test the connection, set the default model after success, confirm the selection in the chat composer, and ask a simple graph-statistics question.

Analysis URL and API-key inputs default to empty. Reopening a saved connection does not reveal its original credentials. Leaving a field empty generally preserves the saved value; use the explicit clear action to remove a key. Display names and available/default models depend on the release and provider. This guide does not depend on a private gateway.

### 7.2 Graph-construction model

1. Open **Create your graph** and upload CSV, SQL, or TXT, or select an existing construction source.
2. Select a model in the graph-construction model settings. For another model, select the custom-model option and enter its actual ID.
3. Enter the construction API URL and key, then save the model configuration. This entry uses an OpenAI-compatible chat interface. The implementation appends `/v1` to a bare domain; still verify the correct URL with your provider.
4. For free text such as a novel, choose LLM entity/relation extraction or the novel-character-relationship preset. Automatic mode may read lines individually and does not guarantee character-relationship extraction.
5. CSV/SQL use rule-based construction by default. Agent refinement and intelligent TXT extraction require the corresponding model capabilities.
6. Start construction, inspect the resulting draft, and save it as a dataset. Only saved construction results are used for subsequent graph analysis.

Analysis and construction connections are configured separately. Construction settings are shared by the single-user workspace, not private to one dataset. Saved UI settings take precedence; an incomplete saved connection may fall back to server environment variables.

For server-level construction defaults, put these values in the private `.env`:

```dotenv
GRAPH_CONSTRUCTION_MODEL=<MODEL_ID>
GRAPH_CONSTRUCTION_BASE_URL=<MODEL_API_BASE_URL>
GRAPH_CONSTRUCTION_API_KEY=<MODEL_API_KEY>
```

Replace the placeholders and run `dc up -d --no-build --wait`. Never put real credentials in public documentation or example configuration. Selecting another model does not rebuild saved example graphs automatically.

## 8. Bundled examples, source data, and acceptance checks

### 8.1 Three examples on the home page

The **New task** page presents the following examples under the introductory analysis heading:

| Example | Saved graph size | Notes |
| --- | --- | --- |
| Financial ownership analysis | 19,436 nodes, 26,872 directed edges | A three-table PK/FK graph preserving institutions, individuals, and shareholding-record nodes |
| Molecular property analysis | 22 heavy atoms, 24 undirected chemical bonds | Stored as 48 directed edge entries; loads only the first molecule, BO-01 |
| Literary character relationships | 304 nodes, 715 directed relationships | The character graph from *Demi-Gods and Semi-Devils*, including subsequent corrections and direction fixes |

These counts come from the reference resource manifests and are not guarantees for arbitrary reconstruction. In particular, the 791 edges recorded by an earlier novel-extraction run are not the count in the corrected saved graph.

Check the following:

1. Each card displays its title, introduction, and four short question buttons. The question-mark control shows the complete question.
2. **Use example graph** opens the corresponding analysis workspace, and an example question starts a request.
3. The graph workspace displays the graph and highlights returned by tools.
4. After completing an example question, Tab inserts the next suggested question into the composer without submitting it automatically.
5. **New task** returns to the three example cards.
6. **Help → Workflow** explains the path from data sources to analysis; **Help → User cases** plays the three videos bundled in the frontend image.

### 8.2 Construction sources

The complete release includes these inputs or supporting resources:

- Three financial CSV tables: `individual_info.csv`, `institution_main.csv`, and `institution_shareholder_relationship.csv`, presented as a combined source.
- `stackexchange-graph-15000.sql` for SQL graph construction.
- `arxiv.pt`, distributed in compressed shards, containing 169,343 nodes and 2,315,598 edge entries. It is not one of the three home-page examples.
- The source TXT of *Demi-Gods and Semi-Devils* and its saved character graph.
- The first molecular structure graph and supporting background metadata.

Resources are under `graph_engine/resources/finance_risk`, `borylation`, `tianlongbabu`, and `example_inputs`. Initial installation copies relevant resources into persistent storage. Construction-source entries are typically initialized when the source list is first opened.

The corresponding entries should appear under **Create your graph → My construction sources**. A custom upload creates a separate source; a matching name does not overwrite a bundled graph. Separately uploaded TXT files, user-created projects, historical sessions, and custom graphs are runtime data. **Their presence on an existing server does not add them to the repository or a fresh deployment.** Restore a data backup to migrate them.

An older financial-example directory remaining in the source does not imply that a fourth financial card should appear. The frontend/backend example registry and release resources determine which cases are visible.

### 8.3 Language, streaming, and persistence

Switching the interface language should update system text and built-in example questions. New model responses are requested in the selected language. Historical messages are not translated, and a model may not fully follow the language instruction; verify a new conversation turn.

Analysis uses a stream of events. Model latency, tool duration, and proxy buffering affect when output becomes visible. Prompts request a brief conclusion before supporting analysis, but this is not a guaranteed response format.

After saving a graph, a conversation, and model settings, recreate the containers using the same `dc` options to check persistence:

```bash
dc up -d --no-build --force-recreate --wait --wait-timeout 180
```

Run this during a maintenance window after active tasks finish. Refresh and confirm that graphs, conversations, and model settings remain. Do not simulate a restart by deleting volumes.

## 9. Optional Neo4j and Cora sources

### 9.1 Connect an existing Neo4j database

In **Create your graph** or the dataset panel, add a Neo4j dataset:

1. Enter `<NEO4J_URI>`, `<NEO4J_USER>`, `<NEO4J_PASSWORD>`, and `<NEO4J_DATABASE>`.
2. Test connectivity, read the schema, and select node labels and relationship types.
3. Choose directed/undirected semantics and an appropriate node-name property.
4. Save the connection, select the target project, and load the graph for analysis.
5. Use the refresh-from-Neo4j action after database changes to read a new snapshot.

The server's Graph Engine container initiates the connection, so the database address must be reachable from that container. The browser's local SSH address is not the database address. Edits made during graph analysis are not automatically written back to Neo4j.

The general import path limits node count, relationship count, and property size. It reports an error on exceeding those limits instead of silently truncating; use the deployment limits shown in the interface. It also does not automatically interpret arbitrary properties as training features or data splits.

### 9.2 Provision the optional Cora Neo4j service

**Package-specific instructions:** the source deployment used an optional eighth service, with `compose.neo4j.yaml` and `deploy/neo4j/import_cora.py`. These files are absent from the `main` snapshot used for this English edition. Proceed only if your complete release package supplies both files and matches this configuration. Otherwise use section 9.1; do not run commands against missing files.

Starting an empty Neo4j container does not import Cora. Supply a trusted `cora.pt`; do not rely on a private directory on the original test machine.

Create private configuration in the project root without overwriting an existing file:

```bash
umask 077
python3 - <<'PY'
from pathlib import Path
import os
import secrets
p = Path('.env.neo4j')
if p.exists():
    raise SystemExit('.env.neo4j already exists; preserve the current configuration.')
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    f.write('IGRAPH_NEO4J_PASSWORD=' + secrets.token_hex(32) + '\n')
PY
chmod 600 .env.neo4j
```

Add each option once to `dc` in the current shell:

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

This build sequence is for CPU deployments. GPU deployments use the shared compute-stack sequence in section 10. The optional Compose file defines the built-in account; actual credentials are omitted here. Changing an environment variable does not change the password of an initialized database.

The database address inside the Docker network is `bolt://neo4j-cora:7687`. For local Neo4j Browser access, additionally forward `27474 → 27474` and `27687 → 27687`, then open the [local Neo4j Browser](http://127.0.0.1:27474/). The reference configuration binds these server ports to loopback only.

Back up the Neo4j volume separately from iGraph's `/data`. The reference configuration uses a 1 GiB maximum heap and 512 MiB page cache for a small Cora database; this is not a sizing recommendation for large databases.

## 10. Optional GPU compute stack

Use this section only when all requirements in section 1.4 are met and the workload needs a GPU. The three ordinary examples do not require it.

1. Install compatible software using the [NVIDIA Container Toolkit instructions](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).
2. Check CDI device visibility. The container user also needs permission to access the devices.

```bash
nvidia-smi
nvidia-ctk --version
nvidia-ctk cdi list
```

3. Add the GPU override to the current `dc` configuration:

```bash
IGRAPH_COMPOSE_FILES+=(-f compose.gpu.yaml)
dc config --quiet
```

4. Build in this order to avoid overwriting the shared Graph Engine image with competing CPU/GPU builds:

```bash
dc build graph-engine-api agent-runtime
dc build graph-engine-mcp backend frontend
dc up -d --no-build --wait --wait-timeout 180
```

`compose.gpu.yaml` configures GPU dependencies and CDI devices for Graph Engine API and Agent Runtime. Worker shares the API image but has no GPU device configuration. Do not run an unrestricted `dc build` with this combination: Worker's default CPU build arguments could overwrite the image with the same name. Large-graph Worker retains its CPU execution path.

5. Verify both services that actually receive GPU devices:

```bash
dc exec -T graph-engine-api python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); assert torch.cuda.is_available(); print(torch.ones(1, device="cuda"))'
dc exec -T agent-runtime python -c \
  'import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available()); assert torch.cuda.is_available(); print(torch.ones(1, device="cuda"))'
```

Both should report the CUDA 13.0 compute stack, `True`, and a successfully allocated CUDA tensor.

## 11. Update an existing deployment

### 11.1 Record the environment and back it up

Record the code revision or release-archive checksum, image IDs, Compose project name, environment files, overrides, volume names, and network name. Complete the backup in section 12 and schedule maintenance after active tasks finish.

An installation extracted from an archive may have no `.git` directory, so do not run `git pull` there. For a Git checkout, inspect uncommitted changes first.

### 11.2 Update a source checkout

The following assumes a separate source directory with a clean worktree. For a first installation, use section 3.

```bash
export IGRAPH_SOURCE_DIR='<SOURCE_DIR>'
git -C "$IGRAPH_SOURCE_DIR" status --short
git -C "$IGRAPH_SOURCE_DIR" fetch origin
git -C "$IGRAPH_SOURCE_DIR" switch '<RELEASE_BRANCH>'
git -C "$IGRAPH_SOURCE_DIR" pull --ff-only origin '<RELEASE_BRANCH>'
git -C "$IGRAPH_SOURCE_DIR" rev-parse HEAD
```

If changes are uncommitted, preserve them and decide how to merge them before continuing. Do not force-reset the checkout. For a tagged release, check out the specified tag instead of pulling a branch.

### 11.3 Export into a clean release directory

```bash
export IGRAPH_NEW_RELEASE_DIR='<NEW_RELEASE_DIR>'
test ! -e "$IGRAPH_NEW_RELEASE_DIR"
mkdir -p "$IGRAPH_NEW_RELEASE_DIR"
git -C "$IGRAPH_SOURCE_DIR" archive --format=tar \
  -o '<RELEASE_TAR_PATH>' HEAD
tar -xf '<RELEASE_TAR_PATH>' -C "$IGRAPH_NEW_RELEASE_DIR"
cp "$IGRAPH_APP_DIR/.env" "$IGRAPH_NEW_RELEASE_DIR/.env"
```

Check each command succeeds. `git archive` exports source and resources from the selected commit; migrate runtime data using section 12. Also extract supplied release archives into new empty directories.

Copy the private `.env.neo4j` and deployment overrides actually in use. Preserve the project name, volume names, network name, and workspace owner ID, and ensure new code has not overwritten them. The new directory must pass section 3.1.

Enter the new directory and redefine `dc` with all enabled overrides from section 5. CPU deployments use `dc build`; GPU deployments use the per-service build order in section 10. After a successful build:

```bash
dc config --quiet
dc up -d --no-build --wait --wait-timeout 180
dc ps
curl -fsS http://127.0.0.1:21121/health
```

After all checks succeed, record the new `DEPLOYED_COMMIT` or archive checksum and update the active application path in your operations records. Hard-refresh the browser and perform the checks in section 8. Changing only the Compose project name does not isolate explicitly named volumes and networks.

## 12. Backup, restoration, and rollback

### 12.1 What to preserve

| Item | Purpose |
| --- | --- |
| iGraph data volume | Graphs, original inputs, workspaces, construction state, and some private configuration |
| PostgreSQL logical backup | Runtime sessions, messages, model settings, and other database state |
| Neo4j volume, if enabled | Database graph and database state |
| Private configuration and overrides | `.env`, `.env.neo4j`, and deployment-specific Compose options |
| Matching source and image information | Restore a version compatible with the backed-up data |

Backups contain credentials and business data. Store them in a restricted directory; sharing this guide does not make backup files safe to publish.

### 12.2 Example consistent backup

Run these commands in the project directory in Bash on the server, with `dc` correctly defined. They briefly stop application writes. If Neo4j is enabled, its volume is copied after stopping that service. Wait for active tasks to finish first.

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
  # Also copy any other overrides in use and record their options.
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
  printf 'Backup complete: %s\n' "$backup_dir"
)
```

The container's `--user root` preserves file permissions in the archive; it does not switch the host to rootful Docker. These commands assume named volumes. For host bind mounts, adapt the backup to the actual mount layout. A readable archive alone does not establish recoverability; validate it with an isolated restoration.

### 12.3 Restore into an isolated environment

1. Use complete code and images matching the backup. Restore configuration into a new directory while preserving the original database credentials and workspace owner ID.
2. Set a distinct project name, iGraph data volume, PostgreSQL volume, and network for the restored instance. Use a different frontend port, such as `21122`. If Neo4j is enabled, its volume and HTTP/Bolt port mappings must also be separate.
3. Define `dc` for the restoration environment and verify every argument. Do not import data using the production `dc` function.
4. Extract the iGraph data archive into an empty volume, for example:

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

5. Start only PostgreSQL with an empty database volume, then restore the logical backup:

```bash
dc up -d --no-build --wait postgres
dc exec -T postgres sh -c \
  'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < "$IGRAPH_BACKUP_DIR/postgres.dump"
```

6. If the backup includes Neo4j, use the same Neo4j image version and restore `neo4j-data.tar.gz` into an empty Neo4j volume while the database is stopped. Do not copy physical database files between arbitrary major versions.
7. Start the remaining services, check their health, and create a new SSH tunnel from local port `21131` to the restored server's port `21122`.
8. Verify graphs, conversations, model configuration, source data, and optional database connections before switching the entry point.

Confirm target volumes are empty before restoring. Never extract an archive into a volume currently in use, and do not run `down -v` or delete volumes in the active environment.

### 12.4 Rollback

Keep the previous clean release directory, images, and configuration. Before reverting code, check for database or data-format migrations: rolling back code alone is not always sufficient. If needed, restore the previous consistent backup into an isolated instance before switching access.

## 13. Troubleshooting

### 13.1 The page does not open

Run on the server:

```bash
dc ps
curl -fsS http://127.0.0.1:21121/health
dc logs --tail 100 frontend backend
```

If the server works but the local browser cannot connect, recreate the tunnel from section 6 and check local port conflicts. The frontend health endpoint only confirms that Nginx is reachable. Check page data loading or `/api/workspace` to verify the backend connection as well.

### 13.2 Docker cannot find the containers or reports permission errors

Check `docker context show`, `docker info`, and `systemctl --user status docker`. Do not mix deployment users, rootful/rootless daemons, or unrelated `DOCKER_HOST` settings. Access through the correct user does not authorize changing another deployment's volumes.

### 13.3 Unhealthy services, failed builds, or insufficient memory

```bash
dc logs --tail 100 backend agent-runtime graph-engine-api graph-engine-mcp
docker stats --no-stream
free -h
df -h
```

Check internal service keys and database credentials against the existing volumes. Inspect image downloads, pinned dependency availability, and space on both the data and container-storage disks. If a pinned wheel is unavailable, stop and ask a maintainer to correct the release dependency list instead of removing version constraints arbitrarily.

### 13.4 Model errors or missing streaming output

- `401/403`: verify provider credentials and permissions; handle quota errors according to the provider's response.
- `404`: verify model ID, API base path, and interface type.
- Chat works but tools fail: test tool calling and the construction protocol separately.
- A large block of text appears at once: check whether the upstream streams responses and whether an additional reverse proxy buffers SSE. A passing frontend health check does not validate streaming.

### 13.5 TXT construction is slow or produces unexpected results

Verify the processing mode, saved configuration, and source text. Free-text relationship extraction requires an LLM; default line-by-line handling is not novel-character extraction. Processing a full text in chunks can take time. Closing the page does not cancel the background task; reopen it to inspect progress instead of submitting the same job repeatedly.

### 13.6 Neo4j connection fails

First confirm that the Graph Engine container can reach the database address, then check the password, database name, and read permissions. Browser-side `127.0.0.1` and container-side `127.0.0.1` refer to different network namespaces. A successfully started database without imported Cora data is still an empty database, not a loaded example.

### 13.7 Missing examples or empty data after restoration

Confirm the release package, complete resources, original data volumes, and workspace owner ID. Inspect Graph Engine and backend logs. Installation markers may prevent automatic reinstallation of previously deleted built-in sources; repeated restarts or deleting all volumes is not an appropriate fix.
