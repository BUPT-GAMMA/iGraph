# 工作区 uv 虚拟环境能力 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 agent 能用 uv 为用户上传的代码准备任意版本的 Python 依赖，同时不削弱现有工作区隔离。

**Architecture:** 新增只读 `Environment` 分区存放 venv。uv 在沙箱外（服务端进程，有网络）执行安装，venv 以 `--ro-bind` 挂入沙箱供代码执行。沙箱内因此不需要网络、不需要 CA 证书、不需要放宽路径过滤。安装是一次阻塞调用，通过既有的 `emit_job_progress()` 实时报进度。

**Tech Stack:** Python 3.12、uv、bubblewrap、pytest、Docker Compose

**Spec:** `docs/archive/superpowers/specs/2026-09-15-workspace-uv-environments-design.md`

## Global Constraints

- 包来源只允许官方 PyPI，index-url 由服务端写死为 `https://pypi.org/simple`；拒绝 `git+`、`http://`、`https://`、本地路径、shell 元字符
- `Environment` 分区对 agent **只读**，内容只能经 `workspace_env_ensure` 变更
- `Environment/` 必须排除在 `_iter_files()` 之外（现状对全树每文件算 SHA-256，每次 `workspace_bash` 前后各一遍；venv 数万文件会让每条命令付两遍全量哈希）
- `UV_LINK_MODE=copy`：避免 venv 内出现 symlink
- `UV_CACHE_DIR` 指向用户级 `/data/agent_runtime/uv-cache/<user_id>`，跨会话复用
- 沙箱 namespace 参数**必须排除 `--unshare-pid`**：容器内 PID namespace 配合 `--proc` 挂载会被拒绝（spike 实测）
- 测试全部用 `PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest` 运行；仓库根目录无 `.env`，勿用 `docker compose`
- 现有 32 个 workspace 测试用例必须保持通过

## File Structure

| 文件 | 职责 | 改动性质 |
|---|---|---|
| `workspace_storage.py` | 共享分区常量与上传落盘 | `_ZONES` 加一项 |
| `agent_runtime/workspace_env.py` | **新建**：包名校验 + uv 环境构建 | 纯新增，无 agno 依赖，便于单测 |
| `agent_runtime/workspace_tools.py` | 沙箱执行与工具注册 | 分区、挂载、`_iter_files` 排除、新工具 |
| `agent_runtime/instructions.py` | LLM 系统提示中的分区说明 | 文案 |
| `deploy/docker/agent-runtime.Dockerfile` | 运行时镜像 | 装 uv |
| `compose.yaml` | 部署配置 | `seccomp:unconfined` |
| `.env.example` | 运维文档 | 说明新配置与权衡 |
| `tests/test_workspace_env.py` | **新建**：校验与构建的单测 | 纯新增 |
| `tests/test_workspace_tools.py` | 工作区行为测试 | 更新工具集断言 + 新增用例 |

把校验和 uv 调用放进独立的 `workspace_env.py`，理由：`workspace_tools.py` 已 725 行且混杂沙箱、清单、分区三类职责；新逻辑不依赖 `RunContext`，独立文件可以直接单测而无需构造 agno 上下文。

---

### Task 1: 注册 Environment 分区

先让分区存在且不被现有逻辑破坏。若不先做这步，venv 里每个文件都会被 `_relocate_new_root_files()` 搬进 `Ephemeral/`。

**Files:**
- Modify: `workspace_storage.py:9`
- Modify: `agent_runtime/workspace_tools.py:28`
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `_ZONES = ("Uploaded", "Ephemeral", "Derived", "Environment")` 于两个模块；`workspace_root_for()` 建出的根目录含 `Environment/` 子目录

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_tools.py` 末尾：

```python
def test_environment_zone_exists_and_survives_bash(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    # 分区目录随工作区初始化一并建出
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))
    assert (root / "Environment").is_dir()

    # 放在 Environment 下的文件不得被 _relocate_new_root_files 搬进 Ephemeral
    (root / "Environment" / "venv").mkdir(parents=True)
    (root / "Environment" / "venv" / "marker.txt").write_text("kept", encoding="utf-8")
    asyncio.run(tools["workspace_bash"]("printf y > Ephemeral/again.txt", current))
    assert (root / "Environment" / "venv" / "marker.txt").read_text() == "kept"
    assert not (root / "Ephemeral" / "Environment").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py::test_environment_zone_exists_and_survives_bash -v`

Expected: FAIL —— `Environment` 目录不存在（`assert (root / "Environment").is_dir()`）

- [ ] **Step 3: 两处 `_ZONES` 加上 Environment**

`workspace_storage.py:9` 改为：

```python
_ZONES = ("Uploaded", "Ephemeral", "Derived", "Environment")
```

`agent_runtime/workspace_tools.py:28` 改为：

```python
_ZONES = ("Uploaded", "Ephemeral", "Derived", "Environment")
```

两处必须一致：`workspace_storage._ensure_layout()` 负责建目录，`workspace_tools._zone()` 负责分类，不一致会导致目录建出来但被判为 `Ephemeral`。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py -v`

Expected: 33 passed（原 32 + 新增 1）

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add workspace_storage.py agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "feat(workspace): 新增 Environment 分区用于存放虚拟环境

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 将 Environment 排除在全树校验和之外

`_iter_files()` 对工作区每个文件算 SHA-256，且在每次 `workspace_bash` 前后各调用一次（`workspace_tools.py:531`、`:428`）。一个 torch venv 约 2GB／数万文件，不排除会让此后每条 shell 命令都付两遍全量哈希。这是必须处理的性能项，不是优化。

**Files:**
- Modify: `agent_runtime/workspace_tools.py:184-202`（`_iter_files`）
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: Task 1 的 `_ZONES`
- Produces: `_iter_files(root)` 返回值不含任何 `Environment/` 下的键；因此 `_protected_snapshot()`、`_changed_metadata()`、`_relocate_new_root_files()` 都不再看到 venv 文件

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_tools.py`：

```python
def test_iter_files_skips_environment_subtree(tmp_path):
    from agent_runtime.workspace_tools import _iter_files

    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))

    venv = root / "Environment" / "venv" / "lib"
    venv.mkdir(parents=True)
    for index in range(50):
        (venv / f"module{index}.py").write_text(f"# {index}", encoding="utf-8")

    keys = _iter_files(root)
    assert "Ephemeral/probe.txt" in keys
    assert not any(key.startswith("Environment/") for key in keys)


def test_environment_files_are_not_reported_as_artifacts(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Environment").mkdir(parents=True, exist_ok=True)

    # 命令期间在 Environment 下新增文件，不应出现在变更产物里
    result = asyncio.run(tools["workspace_bash"](
        "mkdir -p Environment/venv && printf pkg > Environment/venv/site.py", current))
    assert result["success"] is True
    paths = [item["path"] for item in result["artifacts"]]
    assert not any(path.startswith("Environment/") for path in paths)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py::test_iter_files_skips_environment_subtree tests/test_workspace_tools.py::test_environment_files_are_not_reported_as_artifacts -v`

Expected: 两个都 FAIL —— `Environment/` 下的键出现在 `_iter_files` 结果与 `artifacts` 中

- [ ] **Step 3: 在 `_iter_files` 中剪掉该子树**

把 `agent_runtime/workspace_tools.py:184-202` 的 `_iter_files` 替换为：

```python
def _iter_files(root: Path) -> dict[str, tuple[str, int, str]]:
    found: dict[str, tuple[str, int, str]] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != _MANIFEST_NAME]
        base = Path(directory)
        # A virtual environment holds tens of thousands of files. Checksumming
        # it on both sides of every command would make each shell call cost a
        # full-tree rehash; the zone is server-managed and read-only anyway.
        if base == root:
            dirnames[:] = [name for name in dirnames if name != _ENVIRONMENT_ZONE]
        for filename in filenames:
            path = base / filename
            relative = path.relative_to(root)
            key = relative.as_posix()
            if key == _MANIFEST_NAME:
                continue
            try:
                if path.is_symlink():
                    found[key] = ("symlink", 0, "")
                elif path.is_file():
                    found[key] = ("file", path.stat().st_size, _checksum(path))
            except OSError:
                continue
    return found
```

在 `agent_runtime/workspace_tools.py:29`（`_MANIFEST_NAME` 之后）加入常量：

```python
_ENVIRONMENT_ZONE = "Environment"
```

剪 `dirnames` 而非过滤 `key`，是为了让 `os.walk` 根本不进入该目录——这才省下遍历和哈希开销。只在 `base == root` 时剪，避免误伤深层同名目录（如 `Ephemeral/Environment/`）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py -v`

Expected: 35 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "perf(workspace): 将 Environment 分区排除在全树校验和之外

_iter_files 对每个文件算 SHA-256，且每条命令前后各跑一次。虚拟环境
动辄数万文件，不排除会让此后每条 shell 命令都付两遍全量哈希。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 包名校验

安全边界。独立成模块以便无 agno 上下文直接单测。

**Files:**
- Create: `agent_runtime/workspace_env.py`
- Test: `tests/test_workspace_env.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `PYPI_INDEX_URL: str` = `"https://pypi.org/simple"`
  - `class InvalidRequirement(ValueError)`
  - `def validated_requirements(packages: list[str]) -> list[str]` —— 返回归一化后的清单；任一项非法则抛 `InvalidRequirement`，消息为面向用户的中文

- [ ] **Step 1: 写失败测试**

新建 `tests/test_workspace_env.py`：

```python
import pytest

from agent_runtime.workspace_env import InvalidRequirement, validated_requirements


@pytest.mark.parametrize("value", [
    "requests",
    "torch==2.0.1",
    "numpy>=1.24",
    "scikit-learn",
    "pandas!=2.1.0",
    "Flask~=3.0",
    "typing_extensions",
])
def test_accepts_plain_pypi_requirements(value):
    assert validated_requirements([value]) == [value]


@pytest.mark.parametrize("value", [
    "git+https://github.com/psf/requests",
    "http://evil.example/pkg.whl",
    "https://evil.example/pkg.whl",
    "./local.whl",
    "/abs/path/pkg.whl",
    "../escape/pkg.whl",
    "requests; rm -rf /",
    "requests && curl evil.example",
    "requests`whoami`",
    "requests$(id)",
    "requests|tee",
    "--index-url=http://evil.example",
    "-e .",
    "requests\nnumpy",
    "",
    "   ",
])
def test_rejects_non_pypi_and_injection(value):
    with pytest.raises(InvalidRequirement):
        validated_requirements([value])


def test_rejects_empty_and_oversized_lists():
    with pytest.raises(InvalidRequirement):
        validated_requirements([])
    with pytest.raises(InvalidRequirement):
        validated_requirements([f"pkg{index}" for index in range(101)])


def test_strips_whitespace_and_deduplicates_preserving_order():
    assert validated_requirements(["  requests ", "numpy", "requests"]) == ["requests", "numpy"]


def test_rejects_non_string_entries():
    with pytest.raises(InvalidRequirement):
        validated_requirements(["requests", None])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_env.py -v`

Expected: FAIL —— `ModuleNotFoundError: No module named 'agent_runtime.workspace_env'`

- [ ] **Step 3: 实现校验**

新建 `agent_runtime/workspace_env.py`：

```python
"""Server-side Python environment provisioning for session workspaces.

uv runs here, outside the bubblewrap sandbox, because the sandbox has no
network namespace and no CA bundle. The resulting virtual environment is
mounted read-only into the sandbox, so package downloads never happen from
LLM-authored shell commands.
"""

from __future__ import annotations

import re

PYPI_INDEX_URL = "https://pypi.org/simple"
MAX_REQUIREMENTS = 100

# Name plus optional PEP 440 version constraints. Deliberately narrow: no
# URLs, no local paths, no extras, no environment markers, no option flags.
_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9][A-Za-z0-9._*+!-]*)*$"
)


class InvalidRequirement(ValueError):
    """A requested package is not a plain PyPI name/version specifier."""


def validated_requirements(packages: list[str]) -> list[str]:
    """Normalise a requested package list, rejecting anything but PyPI names.

    Only bare names with optional PEP 440 version constraints pass. VCS refs,
    URLs, local paths, option flags and shell metacharacters are refused so
    that a requirement string can never redirect the download or reach the
    shell.
    """
    if not isinstance(packages, list) or not packages:
        raise InvalidRequirement("请提供至少一个依赖包名")
    if len(packages) > MAX_REQUIREMENTS:
        raise InvalidRequirement(f"依赖数量不能超过 {MAX_REQUIREMENTS} 个")
    seen: list[str] = []
    for item in packages:
        if not isinstance(item, str):
            raise InvalidRequirement("依赖包名必须是字符串")
        value = item.strip()
        if not value:
            raise InvalidRequirement("依赖包名不能为空")
        if not _REQUIREMENT.match(value):
            raise InvalidRequirement(
                f"依赖 {value!r} 无效：只支持 PyPI 包名与版本约束"
                "（如 torch==2.0.1），不支持 URL、git 源、本地路径或命令"
            )
        if value not in seen:
            seen.append(value)
    return seen
```

正则要求首尾为字母数字，因此自然拒绝 `-e .`、`--index-url=...`、`./x`、`/abs`；字符类不含空格、引号、反引号、`$`、`|`、`;`、`&`、换行，因此 shell 元字符与多行注入都被拒。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_env.py -v`

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_env.py tests/test_workspace_env.py
git commit -m "feat(workspace): 新增依赖包名校验，仅允许 PyPI 名称与版本约束

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: uv 环境构建

**Files:**
- Modify: `agent_runtime/workspace_env.py`
- Test: `tests/test_workspace_env.py`

**Interfaces:**
- Consumes: Task 3 的 `validated_requirements()`、`PYPI_INDEX_URL`
- Produces:
  - `async def build_environment(root: Path, packages: list[str], *, user_id: str, on_progress=None) -> dict` ——
    成功返回 `{"success": True, "python": "Environment/venv/bin/python", "packages": [...], "cached": bool}`；
    失败返回 `{"success": False, "error": str, "output": str}`。
    `on_progress` 为可选 `async` 回调，接受 `dict`。
  - `def environment_python_path() -> str` 返回工作区相对路径 `"Environment/venv/bin/python"`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_env.py`：

```python
import asyncio
import sys
from pathlib import Path

from agent_runtime.workspace_env import build_environment, environment_python_path


def test_environment_python_path_is_workspace_relative():
    assert environment_python_path() == "Environment/venv/bin/python"


def test_build_environment_rejects_bad_requirements_before_running_uv(tmp_path, monkeypatch):
    # uv 不可用也应立即因校验失败返回，证明校验先于任何子进程
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    result = asyncio.run(build_environment(
        tmp_path, ["git+https://evil.example/x"], user_id="u1"))
    assert result["success"] is False
    assert "PyPI" in result["error"]


def test_build_environment_reports_missing_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    result = asyncio.run(build_environment(tmp_path, ["requests"], user_id="u1"))
    assert result["success"] is False
    assert "uv" in result["error"]


def test_build_environment_creates_venv_and_emits_progress(tmp_path):
    # 用标准库自身作为「已安装包」目标，避免测试依赖网络
    events = []

    async def record(payload):
        events.append(payload)

    result = asyncio.run(build_environment(
        tmp_path, ["setuptools"], user_id="u1", on_progress=record))

    if not result["success"]:
        import pytest
        pytest.skip(f"uv 不可用或无网络: {result.get('error')}")

    assert result["python"] == "Environment/venv/bin/python"
    assert (tmp_path / "Environment" / "venv" / "bin" / "python").exists()
    assert (tmp_path / "Environment" / ".packages.txt").read_text().strip() == "setuptools"
    assert [event["job"]["status"] for event in events][-1] == "done"


def test_failed_build_leaves_no_usable_venv(tmp_path):
    result = asyncio.run(build_environment(
        tmp_path, ["igraph-nonexistent-package-xyz-99999"], user_id="u1"))
    assert result["success"] is False
    # 失败必须清理，避免后续调用误用残缺环境
    assert not (tmp_path / "Environment" / "venv").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_env.py -v`

Expected: FAIL —— `ImportError: cannot import name 'build_environment'`

- [ ] **Step 3: 实现构建**

在 `agent_runtime/workspace_env.py` 的 import 段补上：

```python
import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable
```

并在文件末尾追加：

```python
_VENV_DIRNAME = "venv"
_MANIFEST_FILENAME = ".packages.txt"
_DEFAULT_INSTALL_TIMEOUT = 600.0


def environment_python_path() -> str:
    return f"Environment/{_VENV_DIRNAME}/bin/python"


def _uv_binary() -> str | None:
    configured = os.getenv("IGRAPH_UV_BINARY", "").strip()
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("uv")


def _cache_dir(user_id: str) -> Path:
    root = Path(os.getenv("IGRAPH_DATA_ROOT", "").strip() or "data")
    # A per-user cache is what makes a second session install the same
    # version in seconds instead of minutes.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(user_id or "anonymous"))
    return root / "agent_runtime" / "uv-cache" / safe


async def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, f"安装超时（{timeout:g}秒）"
    return proc.returncode or 0, output.decode(errors="replace")


async def build_environment(
    root: Path,
    packages: list[str],
    *,
    user_id: str,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Create a session virtual environment and install the requested packages.

    Runs outside the sandbox so uv has network access. Emits progress through
    `on_progress` because the call blocks for as long as the install takes.
    """
    async def report(status: str, message: str, progress: float) -> None:
        if on_progress is not None:
            await on_progress({"job": {"job_id": f"env:{user_id}", "tool_name": "workspace_env_ensure",
                                      "status": status, "progress": progress, "message": message},
                               "progress_updates": [], "progress_cursor": 0})

    try:
        requirements = validated_requirements(packages)
    except InvalidRequirement as exc:
        return {"success": False, "error": str(exc), "output": ""}

    uv = _uv_binary()
    if uv is None:
        return {"success": False, "output": "",
                "error": "uv 不可用，无法准备 Python 环境；请确认运行时镜像已安装 uv"}

    environment_root = root / "Environment"
    environment_root.mkdir(parents=True, exist_ok=True)
    venv = environment_root / _VENV_DIRNAME
    cache = _cache_dir(user_id)
    cache.mkdir(parents=True, exist_ok=True)
    budget = timeout if timeout is not None else float(
        os.getenv("IGRAPH_WORKSPACE_ENV_TIMEOUT_SECONDS", str(_DEFAULT_INSTALL_TIMEOUT)))

    env = {
        "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(environment_root),
        "UV_CACHE_DIR": str(cache),
        # Copy instead of symlink: the workspace tooling rejects symlinks.
        "UV_LINK_MODE": "copy",
        "UV_INDEX_URL": PYPI_INDEX_URL,
        "UV_NO_CONFIG": "1",
    }

    await report("running", "正在创建虚拟环境", 0.1)
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    code, output = await _run(
        [uv, "venv", str(venv), "--python", sys.executable],
        cwd=environment_root, env=env, timeout=min(budget, 120.0))
    if code != 0:
        shutil.rmtree(venv, ignore_errors=True)
        await report("failed", "虚拟环境创建失败", 1)
        return {"success": False, "error": "虚拟环境创建失败", "output": output[-4000:]}

    await report("running", f"正在安装 {len(requirements)} 个依赖", 0.3)
    code, install_output = await _run(
        [uv, "pip", "install", "--python", str(venv / "bin" / "python"),
         "--index-url", PYPI_INDEX_URL, *requirements],
        cwd=environment_root, env=env, timeout=budget)
    if code != 0:
        # A half-installed environment must not be left where a later call
        # could mistake it for a working one.
        shutil.rmtree(venv, ignore_errors=True)
        await report("failed", "依赖安装失败", 1)
        return {"success": False, "error": "依赖安装失败", "output": install_output[-4000:]}

    (environment_root / _MANIFEST_FILENAME).write_text(
        "\n".join(requirements) + "\n", encoding="utf-8")
    await report("done", "环境准备完成", 1)
    return {
        "success": True,
        "python": environment_python_path(),
        "packages": requirements,
        "cached": "Downloading" not in install_output,
        "output": install_output[-4000:],
    }
```

在 import 段确保有 `import sys`（`uv venv --python sys.executable` 用到）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_env.py -v`

Expected: 全部 passed（无 uv 或无网络时 `test_build_environment_creates_venv_and_emits_progress` 会 skip，属预期）

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_env.py tests/test_workspace_env.py
git commit -m "feat(workspace): 实现沙箱外 uv 虚拟环境构建

uv 在服务端进程执行以获得网络访问；失败时清理残缺 venv，避免后续
调用误用。缓存按用户隔离，同版本跨会话复用。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 修正沙箱 namespace 参数并挂载 Environment

spike 实测：`--unshare-all`（含 `--unshare-pid`）配合 `--proc /proc` 在容器内必然报
`Can't mount proc on /proc: Operation not permitted`；去掉 `--unshare-pid` 后可正常挂载。

**Files:**
- Modify: `agent_runtime/workspace_tools.py:370-382`（`_sandbox_command`）
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: Task 1 的 `_ZONES`
- Produces: `_sandbox_command()` 生成的 argv 不含 `--unshare-all` 与 `--unshare-pid`，含 `--unshare-user --unshare-ipc --unshare-uts --unshare-cgroup --unshare-net`，并含 `--ro-bind {root}/Environment /workspace/Environment`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_tools.py`：

```python
def test_sandbox_command_avoids_pid_namespace_and_mounts_environment(tmp_path, monkeypatch):
    from agent_runtime.workspace_tools import _sandbox_command, _ensure_layout

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    root = tmp_path / "ws"
    _ensure_layout(root)
    argv, _env = _sandbox_command(root, "echo hi")

    # 容器内 PID namespace 配合 proc 挂载会被内核拒绝
    assert "--unshare-all" not in argv
    assert "--unshare-pid" not in argv
    for flag in ("--unshare-user", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup", "--unshare-net"):
        assert flag in argv, flag
    assert "--proc" in argv

    joined = " ".join(argv)
    assert f"--ro-bind {root / 'Environment'} /workspace/Environment" in joined
    # 网络仍然隔离：不得出现 share-net
    assert "--share-net" not in argv
```

在 `tests/test_workspace_tools.py` 顶部确认已 `import shutil`（第 4 行已有）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py::test_sandbox_command_avoids_pid_namespace_and_mounts_environment -v`

Expected: FAIL —— `assert "--unshare-all" not in argv`

- [ ] **Step 3: 改沙箱参数**

把 `agent_runtime/workspace_tools.py:374` 那行：

```python
    args = [bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--tmpfs", "/"]
```

替换为：

```python
    # Namespaces are listed explicitly rather than via --unshare-all: a PID
    # namespace combined with mounting proc is refused inside a container
    # ("Can't mount proc on /proc"), while every other namespace works. The
    # network namespace stays unshared, so sandboxed code has no network.
    args = [bwrap, "--unshare-user", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup", "--unshare-net",
            "--die-with-parent", "--new-session", "--tmpfs", "/"]
```

在 `agent_runtime/workspace_tools.py:381`（`--ro-bind Derived` 之后）追加一行：

```python
    args.extend(["--ro-bind", str(root / "Environment"), "/workspace/Environment"])
```

只读挂载即 agent 无法用 bash 篡改已装环境的保证。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py -v`

Expected: 36 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "fix(workspace): 显式列举 namespace 并挂载 Environment 分区

--unshare-all 含 PID namespace，与 proc 挂载组合在容器内被内核拒绝，
导致 bwrap 完全不可用、部署被迫退回无隔离的 fallback。改为逐项列举
并排除 pid；网络 namespace 仍隔离。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: workspace_env_ensure 工具

**Files:**
- Modify: `agent_runtime/workspace_tools.py`（`create_workspace_tools()` 内新增函数，并加入 `:723` 的返回列表）
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: Task 4 的 `build_environment()`、`environment_python_path()`；`agent_runtime/graph_progress.emit_job_progress`
- Produces: `workspace_env_ensure(packages: list[str], run_context: RunContext) -> dict`，注册进工具集；工具集变为四项

- [ ] **Step 1: 写失败测试**

先把 `tests/test_workspace_tools.py:35` 的断言改为：

```python
    assert set(tools) == {"workspace_list", "workspace_bash", "workspace_publish", "workspace_env_ensure"}
```

再追加：

```python
def test_env_ensure_rejects_non_pypi_sources(tmp_path):
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["git+https://evil.example/x"], current))
    assert result["success"] is False
    assert "PyPI" in result["error"]


def test_env_ensure_reports_fallback_security_level(tmp_path, monkeypatch):
    # fallback 下 Environment 的只读保证不成立，必须在返回值里说明
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], current))
    assert result["sandbox_mode"] == "fallback"
    assert result["sandbox_security_level"] == "development-compatibility"


def test_env_ensure_surfaces_missing_uv(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_UV_BINARY", "/nonexistent/uv")
    current = ctx(tmp_path)
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], current))
    assert result["success"] is False
    assert "uv" in result["error"]


def test_env_ensure_requires_session_identity(tmp_path):
    os.environ["IGRAPH_WORKSPACE_ROOT"] = str(tmp_path)
    broken = SimpleNamespace(user_id="u1", session_id="")
    result = asyncio.run(toolset()["workspace_env_ensure"](["requests"], broken))
    assert result["success"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py -v -k "env_ensure or session_scoped"`

Expected: FAIL —— `KeyError: 'workspace_env_ensure'`

- [ ] **Step 3: 实现工具**

在 `agent_runtime/workspace_tools.py` import 段追加：

```python
from agent_runtime.graph_progress import emit_job_progress
from agent_runtime.workspace_env import build_environment, environment_python_path
```

在 `create_workspace_tools()` 内、`workspace_publish` 定义之后插入：

```python
    async def workspace_env_ensure(packages: list[str], run_context: RunContext) -> dict[str, Any]:
        """Prepare a Python environment for uploaded code, then report its interpreter.

        Give plain PyPI names with optional versions, e.g. ["torch==2.0.1",
        "numpy"]. Git refs, URLs and local paths are refused. Installation runs
        on the server with network access and replaces any previous environment
        for this session; the result is read-only inside workspace_bash.
        Run the code with the returned relative interpreter path, for example
        "Environment/venv/bin/python Uploaded/script.py". Large packages can
        take minutes on first use and are cached for later sessions.
        """
        scope = current_scope.get()
        if scope is not None:
            scope.check()
        mode = _sandbox_mode()
        try:
            root = workspace_root(run_context)
        except ValueError as exc:
            return {"success": False, "error": str(exc), **_sandbox_fields(mode)}
        result = await build_environment(
            root, packages, user_id=str(run_context.user_id),
            on_progress=emit_job_progress,
        )
        return {**result, **_sandbox_fields(mode)}
```

把 `agent_runtime/workspace_tools.py:722-726` 的收尾段替换为：

```python
    workspace_list.__name__ = "workspace_list"
    workspace_bash.__name__ = "workspace_bash"
    workspace_publish.__name__ = "workspace_publish"
    workspace_env_ensure.__name__ = "workspace_env_ensure"
    return [workspace_list, workspace_bash, workspace_publish, workspace_env_ensure]
```

`_sandbox_fields(mode)` 复用现有函数（`:341`），fallback 下自动带上
`sandbox_security_level` 与 `sandbox_warning`，满足 spec 中「降级需可见」的要求。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py tests/test_workspace_env.py -v`

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "feat(workspace): 新增 workspace_env_ensure 工具

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Environment 对 agent 只读且不对用户展示

**Files:**
- Modify: `agent_runtime/workspace_tools.py:485-490`（`workspace_list` 的分区过滤）
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: Task 1、Task 6
- Produces: `workspace_list()` 与 `workspace_storage.visible_files()` 结果均不含 `Environment/`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_tools.py`：

```python
def test_environment_is_hidden_from_listings(tmp_path):
    from workspace_storage import visible_files

    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    asyncio.run(tools["workspace_bash"]("printf x > Ephemeral/probe.txt", current))
    (root / "Environment" / "venv" / "bin").mkdir(parents=True)
    (root / "Environment" / "venv" / "bin" / "python").write_text("#!", encoding="utf-8")
    (root / "Uploaded").mkdir(exist_ok=True)
    (root / "Uploaded" / "data.csv").write_text("a,b", encoding="utf-8")

    listed = asyncio.run(tools["workspace_list"](current))["files"]
    assert [item["path"] for item in listed] == ["Uploaded/data.csv"]
    assert not any(item["path"].startswith("Environment/") for item in visible_files("u1", "s1"))


def test_environment_cannot_be_modified_by_bash_in_fallback(tmp_path):
    current = ctx(tmp_path)
    tools = toolset()
    root = tmp_path / "u1" / "s1"
    (root / "Environment" / "venv").mkdir(parents=True)
    target = root / "Environment" / "venv" / "sitecustomize.py"
    target.write_text("original", encoding="utf-8")

    asyncio.run(tools["workspace_bash"]("printf tampered > Environment/venv/sitecustomize.py", current))
    # bwrap 下由 --ro-bind 保证；fallback 下至少不得静默成功地留下篡改
    assert target.read_text(encoding="utf-8") == "original"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py::test_environment_is_hidden_from_listings tests/test_workspace_tools.py::test_environment_cannot_be_modified_by_bash_in_fallback -v`

Expected: 第一个可能已通过（`workspace_list` 已只列 `Uploaded`/`Derived`），第二个 FAIL —— fallback 下文件被改写

- [ ] **Step 3: 把 Environment 纳入受保护分区**

`workspace_list` 的过滤已限定 `{Uploaded, Derived}`（`:486`），无需改动；确认即可。

把 `Environment` 纳入 fallback 的保护语义。修改三处硬编码集合：

`agent_runtime/workspace_tools.py:29` 之后加常量：

```python
_PROTECTED_ZONES = frozenset({"Uploaded", "Derived", "Environment"})
```

`_protected_snapshot()`（`:209`）的判断改为：

```python
        if _zone(relative) not in _PROTECTED_ZONES:
```

`_changed_metadata()`（`:436`）的判断改为：

```python
        if zone in _PROTECTED_ZONES:
```

注意：由于 Task 2 已把 `Environment/` 排除在 `_iter_files()` 之外，
`_protected_snapshot()` 不会真的把 venv 读进内存——这正是我们要的，
既纳入保护语义又不付快照开销。fallback 下的保护由
`_command_rejects_workspace_mutation()` 承担，因此还需在
`:257`、`:261`、`:263` 三处把 `"Uploaded"` 的检查扩展为同时检查
`"Environment"`：

```python
    for index, token in enumerate(tokens):
        if token in {"rm", "mv", "rmdir", "unlink", "chmod", "chown", "touch", "truncate"}:
            if any(zone in value for value in tokens[index + 1 :] for zone in ("Uploaded", "Environment")):
                return True
        if token in {">", ">>", "1>", "1>>", "2>", "2>>"}:
            if any(zone in value for value in tokens[index + 1 :] for zone in ("Uploaded", "Environment")):
                return True
        if ">" in token and ("Uploaded" in token or "Environment" in token):
            return True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py tests/test_workspace_env.py -v`

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "feat(workspace): Environment 分区对 agent 只读且不对用户展示

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 更新 LLM 提示与 workspace_bash 描述

docstring 与 instructions 是 LLM 唯一的行为依据。前面所有能力若不写进这两处，
agent 不会知道可以用。

**Files:**
- Modify: `agent_runtime/instructions.py:57-63`
- Modify: `agent_runtime/workspace_tools.py:495-504`（`workspace_bash` docstring）
- Test: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: Task 6 的工具名与返回字段
- Produces: 提示文本含 `Environment` 与 `workspace_env_ensure`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_workspace_tools.py`：

```python
def test_tool_descriptions_mention_environment_workflow():
    from agent_runtime.instructions import instructions_for

    tools = toolset()
    bash_doc = tools["workspace_bash"].__doc__ or ""
    assert "Environment" in bash_doc
    assert "workspace_env_ensure" in bash_doc

    text = " ".join(instructions_for(reasoning_mode="direct", planning_mode="auto"))
    assert "Environment" in text
    assert "workspace_env_ensure" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/test_workspace_tools.py::test_tool_descriptions_mention_environment_workflow -v`

Expected: FAIL —— `assert "Environment" in bash_doc`

- [ ] **Step 3: 更新两处文案**

`agent_runtime/workspace_tools.py:495-504` 的 docstring 替换为：

```python
        """Run Bash in the current session workspace to inspect/process files or run code.

        Each call starts a fresh shell at the workspace root; use relative paths.
        Uploaded, Derived and Environment are read-only. Write intermediate files
        to Ephemeral, then use workspace_publish to deliver them as user-visible
        artifacts. Only the standard library is importable by default; when
        uploaded code needs third-party packages, call workspace_env_ensure first
        and then run it with the interpreter it returns, e.g.
        "Environment/venv/bin/python Uploaded/script.py". There is no network
        here. Prefer head/sed or bounded Python reads for large files. Results
        contain exit_code, bounded output, output_truncated and changed file
        metadata. Commands have a server-configured timeout and inherit no
        provider secrets.
        """
```

`agent_runtime/instructions.py:58-62` 的段落替换为：

```python
        "会话文件工作区分为 Uploaded（只读上传）、Ephemeral（中间文件）、Derived（已发布产物）"
        "和 Environment（只读 Python 环境）。"
        "workspace_list 查看文件；workspace_bash 使用相对路径执行受限命令，代码和中间输出写入 Ephemeral；"
        "workspace_publish 将中间文件发布为可下载的版本化产物。"
        "沙箱内只有标准库且无网络：用户代码需要第三方依赖时，先用 workspace_env_ensure "
        "声明 PyPI 包名与版本（如 torch==2.0.1），再用它返回的解释器路径运行，"
        "例如 Environment/venv/bin/python Uploaded/script.py；首次安装大型依赖可能需要几分钟。"
        "工作区工具不替代图引擎和构图审批。"
        "沙箱不可用时说明限制并继续使用图工具，不绕过沙箱。避免反复执行相同参数或连续重试失败工具。"
        "需要记录工作进度时使用 update_agent_notes，仅更新工作笔记；项目、数据集和执行状态由服务管理。"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/<user>/iGraph && PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/ -v -k "workspace or instruction"`

Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add agent_runtime/instructions.py agent_runtime/workspace_tools.py tests/test_workspace_tools.py
git commit -m "docs(workspace): 在工具描述与系统提示中说明 Environment 工作流

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: 镜像装 uv 与部署配置

代码就位后打通运行环境。此任务无单测，验证靠实际构建与启动。

**Files:**
- Modify: `deploy/docker/agent-runtime.Dockerfile:8-12`
- Modify: `compose.yaml`（`agent-runtime` 服务，约 `:75-105`）
- Modify: `.env.example`

**Interfaces:**
- Consumes: 前序全部任务
- Produces: 镜像内 `/usr/local/bin/uv` 可用；容器可运行 bwrap

- [ ] **Step 1: Dockerfile 装 uv**

把 `deploy/docker/agent-runtime.Dockerfile:12` 那行：

```dockerfile
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
```

替换为：

```dockerfile
# uv provisions per-session virtual environments for uploaded code. It runs
# outside the bubblewrap sandbox, where network access is available.
RUN python -m pip install --no-cache-dir uv && python -m pip install --no-cache-dir -r /tmp/requirements.txt
```

在 `:20` 的 `mkdir -p` 中追加 uv 缓存目录，使其归属非 root 用户：

```dockerfile
RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data/agent_runtime/skills/users /data/agent_runtime/skills/candidates \
       /data/agent_runtime/uv-cache \
    && chown -R igraph:igraph /app /data
```

- [ ] **Step 2: compose 加 seccomp 配置**

在 `compose.yaml` 的 `agent-runtime` 服务中，`restart: unless-stopped` 之前插入：

```yaml
    # bubblewrap needs to create namespaces; Docker's default seccomp profile
    # refuses that, which forces the workspace into unisolated fallback mode.
    security_opt:
      - seccomp:unconfined
```

- [ ] **Step 3: `.env.example` 记录权衡**

在 `.env.example` 中 `IGRAPH_WORKSPACE_SANDBOX_MODE` 相关段落（约 `:143-151`）之后追加：

```
# 工作区依赖安装超时（秒）。uv 首次安装大型包（如 torch）可能需要数分钟；
# 命中用户级缓存后通常只需数秒。
IGRAPH_WORKSPACE_ENV_TIMEOUT_SECONDS=600

# 说明：agent-runtime 在 compose 中配置了 seccomp:unconfined。
# 这是 bubblewrap 创建 namespace 的必要条件——Docker 默认 seccomp 会拒绝，
# 导致工作区退回无文件系统隔离的 fallback 模式。
# 权衡：容器自身少一层 syscall 过滤，换取工作区的真实隔离。
# 用户上传代码的执行边界由 bubblewrap 承担，因此这个交换是值得的。
```

- [ ] **Step 4: 构建并验证**

```bash
cd /home/<user>/iGraph
docker build -f deploy/docker/agent-runtime.Dockerfile -t igraph-agent-runtime:uv-check .
# uv 就位
docker run --rm igraph-agent-runtime:uv-check uv --version
# 在放开 seccomp 后 bwrap 可创建 namespace（沿用 spike 验证过的参数）
docker run --rm --user 10001 --security-opt seccomp=unconfined igraph-agent-runtime:uv-check \
  bwrap --unshare-user --unshare-ipc --unshare-uts --unshare-cgroup --unshare-net \
        --tmpfs / --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib \
        --ro-bind /lib64 /lib64 --proc /proc --dev /dev /bin/sh -c 'echo SANDBOX_OK'
```

Expected: 打印 uv 版本号；打印 `SANDBOX_OK`

- [ ] **Step 5: 提交**

```bash
cd /home/<user>/iGraph
git add deploy/docker/agent-runtime.Dockerfile compose.yaml .env.example
git commit -m "build(agent-runtime): 安装 uv 并放开 seccomp 以启用 bubblewrap

Docker 默认 seccomp 拒绝 namespace 创建，使工作区隔离无法生效。放开该
限制换取真实的文件系统隔离；用户上传代码的执行边界由 bubblewrap 承担。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: 端到端验证

**Files:**
- 无代码改动；产出为验证结论

**Interfaces:**
- Consumes: Task 1-9 全部
- Produces: 确认真实隔离生效、依赖可用、网络仍被隔离

- [ ] **Step 1: 全量测试**

```bash
cd /home/<user>/iGraph
PYTHONPATH=/home/<user>/iGraph .venv/bin/pytest tests/ -q
```

Expected: 全部通过，无回归

- [ ] **Step 2: 重建并重启 agent-runtime**

由于仓库根目录无 `.env`，不要用 `docker compose`。按现有容器的启动方式重建镜像并重启：

```bash
cd /home/<user>/iGraph
docker build -f deploy/docker/agent-runtime.Dockerfile -t igraph-agent-runtime:latest-dev .
docker stop igraph-agent-runtime-1 && docker rm igraph-agent-runtime-1
# 用与原容器相同的网络、卷、环境变量重新启动，并加上 --security-opt seccomp=unconfined
# 原配置可由 `docker inspect igraph-agent-runtime-1` 在删除前导出留存
```

- [ ] **Step 3: 确认沙箱模式不再是 fallback**

```bash
docker logs igraph-agent-runtime-1 2>&1 | grep -i "sandbox" | tail -20
```

Expected: 启动无 `workspace sandbox unavailable`；执行命令时日志**不再**出现
`sandbox_mode=fallback security_level=development-compatibility`

- [ ] **Step 4: 走一遍真实用户路径**

在前端新建会话，上传一个需要第三方依赖的脚本：

```python
# needs_requests.py
import requests
print("requests", requests.__version__)
```

让 agent 准备环境并执行。确认：

1. `workspace_env_ensure` 被调用，前端能看到安装进度
2. 脚本用 `Environment/venv/bin/python` 成功执行并打印版本
3. `Environment/` 不出现在前端会话文件面板
4. 让 agent 尝试 `printf x > Environment/venv/sitecustomize.py`，应失败
5. 让脚本内发起 HTTP 请求（如 `requests.get("https://pypi.org")`），
   应因沙箱无网络而失败——证明 `--unshare-net` 仍生效

- [ ] **Step 5: 确认缓存复用**

新建第二个会话，装相同版本的同一个包。对比两次
`workspace_env_ensure` 的耗时与返回的 `cached` 字段。

Expected: 第二次显著更快，`cached` 为 `True`

---

## Self-Review

**1. Spec coverage**

| spec 要求 | 对应任务 |
|---|---|
| 新增 `Environment` 分区 | Task 1 |
| `Environment` 排除在 `_iter_files` 之外 | Task 2 |
| 仅 PyPI，拒绝 git/URL/本地路径 | Task 3 |
| uv 沙箱外执行、`UV_LINK_MODE=copy`、用户级缓存 | Task 4 |
| 长时执行 + `emit_job_progress` 进度 | Task 4（回调）、Task 6（接线） |
| namespace 参数排除 `--unshare-pid` | Task 5 |
| `--ro-bind` 挂载 `Environment` | Task 5 |
| `workspace_env_ensure` 工具与工具集断言更新 | Task 6 |
| `Environment` 对 agent 只读、不对用户展示 | Task 7 |
| 安装失败不留残缺 venv | Task 4（实现与测试） |
| fallback 下携带降级警示 | Task 6 |
| docstring 与 instructions 更新 | Task 8 |
| Dockerfile 装 uv、compose 加 seccomp、`.env.example` | Task 9 |
| 端到端验证含沙箱无网络、缓存复用 | Task 10 |

spec 中「`bin/python` 是 symlink」这一边界情况：由 Task 2 将 `Environment/`
排除出 `_iter_files()` 后自然不再触发 symlink 检查，Task 10 Step 4 实际验证。
spec 中「相对路径要求」由 Task 8 的 docstring 明确承担。无遗漏项。

**2. Placeholder scan**

无 TBD／TODO；每个代码步骤都给出完整可粘贴的代码；每个测试步骤都给出完整测试体；
Task 9、10 无单测但给出了确切的构建与验证命令及预期输出。

**3. Type consistency**

- `validated_requirements(packages: list[str]) -> list[str]`（Task 3 定义，Task 4 调用）一致
- `build_environment(root, packages, *, user_id, on_progress=None, timeout=None) -> dict`（Task 4 定义，Task 6 调用）一致；Task 6 只传 `on_progress`，其余用默认值
- `environment_python_path() -> str` 返回 `"Environment/venv/bin/python"`，与 Task 4 测试、Task 8 文案、Task 10 验证三处引用一致
- `_PROTECTED_ZONES`（Task 7 新增）与 `_ENVIRONMENT_ZONE`（Task 2 新增）命名无冲突
- 工具名 `workspace_env_ensure` 在 Task 6、7、8、10 中拼写一致

**4. 任务顺序依赖**

Task 1 必须最先（否则 venv 文件被搬走）；Task 2 必须在 Task 4 之前（否则装包后每条命令都全量哈希）；Task 3 在 Task 4 之前（校验被构建调用）；Task 5 可与 3、4 并行但需在 Task 10 之前；Task 6 依赖 4；Task 7 依赖 1 与 6；Task 8 依赖 6；Task 9 依赖全部代码任务；Task 10 最后。
