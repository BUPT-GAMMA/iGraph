# 工作区 Python 环境能力设计

## Context

iGraph 的 agent 通过 `workspace_list` / `workspace_bash` / `workspace_publish`
三个工具操作会话工作区。用户上传数据文件和分析脚本，agent 用 bash 执行。

当前局限：沙箱内只有镜像自带的 Python 标准库，没有第三方包，也无法安装。
用户上传一个 `import pandas` 的脚本就跑不了；上传需要 `torch==2.0.1` 的代码
更无从谈起。这限制了 agent 处理用户自带代码的能力。

目标：让 agent 能为用户上传的代码准备所需的 Python 依赖（含精确版本），
且不削弱现有的工作区隔离设计。

预期结果：agent 声明依赖清单 → 服务端用 uv 建虚拟环境 → venv 以只读方式
暴露给沙箱内的代码执行。

## 已验证的前提（spike 结论）

在 `igraph-agent-runtime:latest-dev` 镜像上实测：

1. **Docker 默认 seccomp 阻止 bwrap 创建 namespace**。这是 bwrap 当前完全
   不可用的首要原因，`workspace_sandbox_startup_check()` 在容器内必然失败，
   因此部署被迫配置 `IGRAPH_WORKSPACE_SANDBOX_MODE=fallback`（无文件系统隔离）。
2. `--security-opt seccomp=unconfined` 解除上述限制。`CAP_SYS_ADMIN` 与
   AppArmor 均非原因，加了也无效。
3. **PID namespace 与 proc 挂载的组合仍被拒绝**。现有代码的 `--unshare-all`
   （含 `--unshare-pid`）配合 `--proc /proc` 必然报
   `Can't mount proc on /proc: Operation not permitted`。
   去掉 `--unshare-pid` 后 `--proc` 可正常挂载，其余 namespace 隔离保留。
4. **沙箱外创建的 venv 以 `--ro-bind` 挂入后可正常使用**：`import requests`
   成功，`sys.prefix` 正确指向挂载点，写入被拒绝（符合只读预期），
   `pyvenv.cfg` 的 `home = /usr/local/bin` 通过已有的 `--ro-bind /usr/local`
   正确解析到基础解释器。uv 装 5 个包耗时约 2 秒。

结论：uv 在**沙箱外**执行（服务端进程有网络），venv 只读挂入沙箱。
沙箱内因此不需要网络、不需要挂 `/etc/ssl`、不需要放宽路径过滤。

## 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| uv 执行位置 | 沙箱外（服务端进程） | 避免为联网而削弱沙箱边界 |
| 依赖声明方式 | 包名列表 | 简单、可审计、便于限制来源 |
| 包来源 | 仅官方 PyPI，服务端写死 index-url | 拒绝 git/URL/本地路径，防止内网探测与供应链注入 |
| 执行模型 | 长时执行 + 进度事件（复用 graph 工具模式） | 见下方说明 |

### 关于「异步」的更正

原设想为「立即返回 job_id，装完后通知」。实际架构不支持：
`progress_sink` 是 `ContextVar`，在工具调用返回时即 `reset`
（`agent_runtime/tool_dag.py:147,171`），工具返回后事件通道就关闭了。

graph 工具的现有模式是**长时执行**：MCP 返回 `job_id` 后，
`completed_graph_result()` 在同一次工具调用内循环轮询至完成
（`agent_runtime/graph_jobs.py:51`），期间持续通过 `emit_job_progress()`
发进度事件。前端因此能看到实时进度。

本设计沿用该模式：`workspace_env_ensure` 是一次阻塞调用，但通过
`emit_job_progress()` 报告安装进度，用户可见。无需新建 job 存储或轮询工具。

## 架构

### 新增 `Environment` 分区

工作区分区从三个扩展为四个：

| 分区 | agent 权限 | 用户可见 | 用途 |
|---|---|---|---|
| `Uploaded` | 只读 | 是 | 用户上传 |
| `Ephemeral` | 读写 | 否 | 中间产物 |
| `Derived` | 只读 | 是 | 已发布交付物 |
| `Environment` | **只读** | 否 | venv，由服务端管理 |

`Environment` 对 agent 只读：venv 内容只能经 `workspace_env_ensure` 变更，
agent 无法用 bash 篡改已装环境。

### 数据流

```
agent: workspace_env_ensure(packages=["torch==2.0.1"])
  ↓
服务端校验包名格式（拒绝 git+/http/本地路径）
  ↓
写入 Environment/.packages.txt（依赖清单，可审计）
  ↓
uv venv + uv pip install --index-url https://pypi.org/simple
  （沙箱外，UV_LINK_MODE=copy，UV_CACHE_DIR 指向用户级缓存）
  ↓ 期间 emit_job_progress() 报进度
返回 {"success": True, "python": "Environment/venv/bin/python", ...}
  ↓
agent: workspace_bash("Environment/venv/bin/python Uploaded/script.py")
  ↓
沙箱内 --ro-bind Environment，可 import 已装包，不可写
```

### 缓存复用

`UV_CACHE_DIR` 指向 `/data/agent_runtime/uv-cache/<user_id>`
（与现有 `AGENT_RUNTIME_SKILLS_ROOT: /data/agent_runtime/skills` 同构，
`igraph-data` 是命名卷，重启存活）。

同一用户跨会话安装相同版本的包时从缓存复用，实测差异显著：
首次下载数分钟，缓存命中约数秒。这是选 uv 而非预装固定版本的核心收益——
预装无法覆盖任意版本需求，而换版本和首次安装一样慢，缓存才是真正的加速来源。

## 需要修改的文件

### `agent_runtime/workspace_tools.py`（主要改动）

1. `_ZONES` 增加 `"Environment"`（`:28`）
2. `_sandbox_command()`（`:370`）：
   - `--unshare-all` 改为显式列举，**排除 `--unshare-pid`**（spike 结论 3）
   - 新增 `--ro-bind {root}/Environment /workspace/Environment`
3. `_iter_files()`（`:184`）**排除 `Environment/` 整个子树**。
   现状是对全树每个文件算 SHA-256，且每次 `workspace_bash` 前后各跑一次
   （`:531`、`:428`）。一个 torch venv 约 2GB／数万文件，不排除会让此后每条
   shell 命令都付两遍全量哈希。按 `.workspace_manifest.json` 的现有排除方式处理。
4. 新增 `workspace_env_ensure` 工具，加入 `create_workspace_tools()` 返回列表（`:723`）
5. 更新 `workspace_bash` docstring（`:495`）说明 venv 用法——docstring 即
   LLM 可见的工具描述

### `workspace_storage.py`

`_ZONES` 同步增加 `"Environment"`（`:9`）。
`visible_files()`（`:65`）保持只走 `Uploaded`/`Derived`——venv 不对用户展示。

### `agent_runtime/instructions.py`

`:58-60` 的分区说明段落增加 `Environment`，并说明依赖准备流程。
该文本经 `instructions_for()` 注入每个 agent（`agno_factory.py:73`）。

### `deploy/docker/agent-runtime.Dockerfile`

安装 uv。注意当前镜像**未装 uv**（`igraph-agent-runtime:local` 标签虽有 uv
但反而缺 bwrap，是早期实验的陈旧产物，不可作为依据）。

### `compose.yaml`

`agent-runtime` 服务增加：

```yaml
security_opt:
  - seccomp:unconfined
```

`.env.example` 说明此项的作用与权衡：换取工作区真实隔离，代价是容器自身
少一层 syscall 过滤。

### 复用的现有工具

- `emit_job_progress()`（`agent_runtime/graph_progress.py:7`）——进度事件
- `_resolve()` / `_relative()`（`workspace_tools.py:101` / `:79`）——路径校验
- `workspace_root_for()`（`workspace_storage.py:35`）——会话隔离根

## 已知的边界情况

1. **`bin/python` 是 symlink**。`uv venv` 默认建符号链接，而本模块多处拒绝
   symlink（`_contains_symlink:92`、`_iter_files:197`）。spike 确认 bwrap 能
   正常跟随，且 venv 在只读挂载下可用。但需确认 `Environment` 被 `_iter_files`
   排除后不再触发这些检查。
2. **安装中途被打断留下半个 venv**。需要在失败时清理或标记 venv 不可用，
   避免后续调用误用残缺环境。
3. **`_command_rejects_workspace_mutation()`（`:247`）拒绝含绝对路径的命令**。
   实测 `Environment/venv/bin/python script.py`（相对路径）可通过，
   但 `/workspace/Environment/...` 会被拒。工具描述需明确要求相对路径。
4. **fallback 模式下无隔离**。若部署未开启 seccomp 配置，bwrap 不可用，
   此时 `Environment` 的只读保证不成立。`workspace_env_ensure` 在
   `mode == "fallback"` 时应在返回值中携带与现有 `_sandbox_fields()` 一致的
   降级警示。

## 验证方式

### 单元测试（`tests/test_workspace_tools.py`）

现有 32 个用例须全部通过。注意 `:35` 断言工具集合恰为
`{"workspace_list", "workspace_bash", "workspace_publish"}`，新增工具须更新该断言。

新增覆盖：

- `workspace_env_ensure` 拒绝 `git+https://`、`http://`、`./local.whl`、
  含 shell 元字符的包名
- venv 建好后 `workspace_bash` 内可 import 已装包
- agent 无法通过 bash 写入 `Environment/`
- `Environment/` 不出现在 `workspace_list` 与 `visible_files()` 结果中
- `_iter_files()` 跳过 `Environment/`（放入上千个文件后确认耗时不随之增长）
- 安装失败后不留下可被误用的残缺 venv

### 端到端

1. 给 `compose.yaml` 加 `seccomp:unconfined` 并重启 agent-runtime
2. 确认 `workspace_sandbox_startup_check()` 通过、日志显示
   `sandbox_mode=bwrap` 而非 `fallback`
3. 上传一个 `import requests` 的脚本，让 agent 准备环境并执行
4. 确认沙箱内无网络（`--unshare-net` 仍在）：脚本内发起 HTTP 请求应失败
5. 同一用户新建会话装相同版本包，确认缓存命中（耗时显著下降）
6. 确认 `Environment/` 不出现在前端会话文件面板

### 回归重点

- 现有三个工具的行为不变
- fallback 模式下的保护语义不变（`tests/test_workspace_tools.py` 中
  `test_fallback_*` 系列）
- 去掉 `--unshare-pid` 后其余隔离仍生效：沙箱内不可访问工作区外路径
  （`test_uploaded_python_cannot_access_external_path_in_bwrap`）
