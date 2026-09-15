"""Server-side Python environment provisioning for session workspaces.

uv runs here, outside the bubblewrap sandbox, because the sandbox has no
network namespace and no CA bundle. The resulting virtual environment is
mounted read-only into the sandbox, so package downloads never happen from
LLM-authored shell commands.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

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
