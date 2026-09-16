"""Session-scoped workspace tools with Uploaded/Ephemeral/Derived lifecycle rules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agno.run import RunContext
from agent_runtime.execution_scope import current_scope
from agent_runtime.graph_progress import emit_job_progress
from agent_runtime.workspace_env import build_environment, environment_python_path
from agent_runtime.workspace_process import process_group_filter
from workspace_storage import workspace_root_for


logger = logging.getLogger("igraph")

_ZONES = ("Uploaded", "Ephemeral", "Derived", "Environment")
_MANIFEST_NAME = ".workspace_manifest.json"
_ENVIRONMENT_ZONE = "Environment"
_PROTECTED_ZONES = frozenset({"Uploaded", "Derived", "Environment"})
_DEFAULT_OUTPUT_LIMIT = 100_000
_DEFAULT_TIMEOUT_SECONDS = 30.0
_SANDBOX_MODE_ENV = "IGRAPH_WORKSPACE_SANDBOX_MODE"
_ENVIRONMENT_NAMES = (
    "IGRAPH_ENV",
    "IGRAPH_ENVIRONMENT",
    "APP_ENV",
    "ENVIRONMENT",
    "ENV",
    "IGRAPH_RUNTIME_ENV",
)
_REQUIRED_SANDBOX_VALUES = {
    "prod",
    "production",
    "stage",
    "staging",
    "test",
    "testing",
    "ci",
    "qa",
}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: object, fallback: str) -> str:
    raw = str(value or "")
    cleaned = _SAFE_COMPONENT.sub("_", raw).strip(".") or fallback
    # Sanitising two different identities to the same directory would break
    # isolation, so retain a short identity suffix whenever input changed.
    if cleaned != raw:
        cleaned = f"{cleaned}_{hashlib.sha256(raw.encode()).hexdigest()[:10]}"
    return cleaned


def _ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for zone in _ZONES:
        zone_path = root / zone
        if zone_path.is_symlink():
            raise ValueError("工作区分区不能是符号链接")
        zone_path.mkdir(parents=True, exist_ok=True)



def workspace_root(run_context: RunContext) -> Path:
    return workspace_root_for(run_context.user_id, run_context.session_id)


def _relative(raw_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("路径不能为空且必须使用当前工作区内的相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError("路径必须位于当前会话工作区内")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts:
        raise ValueError("路径不能为空")
    return Path(*parts)


def _contains_symlink(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _resolve(ctx: RunContext, raw_path: str) -> tuple[Path, Path, Path]:
    root = workspace_root(ctx)
    relative = _relative(raw_path)
    target = root / relative
    if _contains_symlink(root, relative):
        raise ValueError("不允许通过符号链接访问工作区文件")
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径必须位于当前会话工作区内")
    return root, resolved, relative


def _path(ctx: RunContext, relative: str) -> Path:
    """Backward-compatible safe path helper used by older callers."""

    return _resolve(ctx, relative)[1]


def _zone(relative: Path) -> str:
    return relative.parts[0] if relative.parts and relative.parts[0] in _ZONES else "Ephemeral"


def _public_path(relative: Path) -> str:
    return relative.as_posix()


def _manifest_path(root: Path) -> Path:
    return root / _MANIFEST_NAME


def _load_manifest(root: Path) -> dict[str, dict[str, Any]]:
    path = _manifest_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    records = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    return {str(key): dict(value) for key, value in records.items() if isinstance(value, dict)}


def _save_manifest(root: Path, records: dict[str, dict[str, Any]]) -> None:
    path = _manifest_path(root)
    fd, temporary = tempfile.mkstemp(prefix=".workspace-manifest-", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "artifacts": records}, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _metadata(root: Path, path: Path, relative: Path, *, zone: str, artifact_id: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": _public_path(relative),
        "relative_name": _public_path(relative),
        "mime_type": _mime_type(path),
        "size": path.stat().st_size,
        "checksum": _checksum(path),
        "zone": zone,
        "visibility": "user" if zone in {"Uploaded", "Derived"} else "hidden",
    }
    if artifact_id is not None:
        item["artifact_id"] = artifact_id
    return item


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


def _protected_snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for key, descriptor in _iter_files(root).items():
        relative = Path(key)
        if _zone(relative) not in _PROTECTED_ZONES:
            continue
        path = root / relative
        if descriptor[0] == "file":
            try:
                snapshot[key] = path.read_bytes()
            except OSError:
                snapshot[key] = None
        else:
            snapshot[key] = None
    return snapshot


def _restore_protected(root: Path, before: dict[str, bytes | None]) -> None:
    current = _iter_files(root)
    # Only restore files that existed in the snapshot; ignore new files in protected
    # zones (they may come from concurrent workspace_publish or user uploads).
    for key in before:
        path = root / key
        expected = before[key]
        # A fallback process can replace a protected zone with a symlink (or a
        # regular file). Reset every parent component before restoring so that
        # recovery never follows an attacker-controlled path outside root.
        parent = root
        for part in Path(key).parts[:-1]:
            parent = parent / part
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                parent.unlink()
            parent.mkdir(parents=True, exist_ok=True)
        if expected is None:
            if path.is_file() or path.is_symlink():
                path.unlink()
            continue
        if path.is_symlink():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)


def _command_rejects_workspace_mutation(command: str) -> bool:
    """Catch masked shell errors before execution (bubblewrap catches the rest)."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    if any(token.startswith("/") or ".." in Path(token).parts for token in tokens if token):
        return True
    for index, token in enumerate(tokens):
        if token in {"rm", "mv", "rmdir", "unlink", "chmod", "chown", "touch", "truncate"}:
            if any(zone in value for value in tokens[index + 1 :] for zone in ("Uploaded", "Environment")):
                return True
        if token in {">", ">>", "1>", "1>>", "2>", "2>>"}:
            if any(zone in value for value in tokens[index + 1 :] for zone in ("Uploaded", "Environment")):
                return True
        if ">" in token and ("Uploaded" in token or "Environment" in token):
            return True
    return False


def _sandbox_required_environment() -> bool:
    return any(
        os.getenv(name, "").strip().lower() in _REQUIRED_SANDBOX_VALUES
        for name in _ENVIRONMENT_NAMES
    )


def _sandbox_mode() -> str:
    """Select the execution boundary without silently enabling compatibility mode."""

    configured = os.getenv(_SANDBOX_MODE_ENV, "auto").strip().lower() or "auto"
    available = shutil.which("bwrap") is not None
    if _sandbox_required_environment():
        return "bwrap" if available else "unavailable"
    if configured in {"fallback", "development", "dev"}:
        return "fallback"
    if configured in {"unavailable", "disabled", "none"}:
        return "unavailable"
    if configured == "bwrap":
        return "bwrap" if available else "unavailable"
    # Compatibility execution must be an explicit local-development choice.
    # An unset or auto mode fails closed when bubblewrap cannot run.
    if configured == "auto":
        return "bwrap" if available else "unavailable"
    return "unavailable"


def workspace_sandbox_startup_check() -> None:
    """Fail startup unless the selected non-fallback sandbox can run as this user."""

    mode = _sandbox_mode()
    if mode == "fallback":
        return
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError(
            "workspace sandbox unavailable: bubblewrap executable was not found; "
            "set IGRAPH_WORKSPACE_SANDBOX_MODE=fallback only for local development"
        )

    # Probe the same mount layout used by workspace_bash. Running this as the
    # service user catches disabled user/mount namespaces before uvicorn starts.
    with tempfile.TemporaryDirectory(prefix=".igraph-bwrap-probe-") as temporary:
        root = Path(temporary)
        _ensure_layout(root)
        sandbox = _sandbox_command(root, "exit 0")
        if sandbox is None:
            raise RuntimeError("workspace sandbox unavailable: bubblewrap disappeared during startup check")
        argv, env = sandbox
        try:
            with process_group_filter() as filter_fd:
                completed = subprocess.run(
                    [argv[0], "--seccomp", str(filter_fd), *argv[1:]],
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    check=False,
                    text=True,
                    pass_fds=(filter_fd,),
                    start_new_session=True,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"workspace sandbox unavailable: rootless bubblewrap probe failed ({exc})"
            ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip().replace("\n", " ")
        raise RuntimeError(
            "workspace sandbox unavailable: rootless bubblewrap probe failed "
            f"(exit_code={completed.returncode}, detail={detail[:300]})"
        )


def _sandbox_fields(mode: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"sandbox_mode": mode}
    if mode == "bwrap":
        fields["sandbox_security_level"] = "isolated"
    elif mode == "fallback":
        fields["sandbox_security_level"] = "development-compatibility"
        fields["sandbox_warning"] = "开发兼容模式：未使用 bubblewrap 文件系统隔离"
    return fields


def _sandbox_unavailable_result() -> dict[str, Any]:
    return {
        "success": False,
        "status": "sandbox_unavailable",
        "sandbox_mode": "unavailable",
        "sandbox_unavailable": True,
        "exit_code": None,
        "output": "",
        "output_truncated": False,
        "created_artifacts": [],
        "modified_artifacts": [],
        "artifacts": [],
        "error": {
            "code": "sandbox_unavailable",
            "message": "bubblewrap 不可用，当前环境禁止执行工作区命令",
        },
    }


def _sandbox_command(root: Path, command: str) -> tuple[list[str], dict[str, str]] | None:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    # Rootless GPU containers cannot mount proc in a nested PID namespace.
    # Keep the runtime's process group; seccomp prevents descendants detaching.
    args = [bwrap, "--unshare-user", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup", "--unshare-net",
            "--die-with-parent", "--tmpfs", "/"]
    for source, target in (("/usr", "/usr"), ("/bin", "/bin"), ("/lib", "/lib"), ("/lib64", "/lib64"), ("/usr/local", "/usr/local")):
        if Path(source).exists():
            args.extend(["--ro-bind", source, target])
    args.extend(["--proc", "/proc", "--dev", "/dev", "--bind", str(root), "/workspace"])
    # Expose only NVIDIA devices already granted to the container, retaining
    # the sandbox's private /dev and network namespace.
    for device in sorted(Path("/dev").glob("nvidia*")):
        if device.is_char_device():
            args.extend(["--dev-bind", str(device), str(device)])
    for source in ("/etc/ld.so.cache", "/proc/driver/nvidia"):
        if Path(source).exists():
            args.extend(["--ro-bind", source, source])
    args.extend(["--ro-bind", str(root / "Uploaded"), "/workspace/Uploaded"])
    args.extend(["--bind", str(root / "Ephemeral"), "/workspace/Ephemeral"])
    args.extend(["--ro-bind", str(root / "Derived"), "/workspace/Derived"])
    args.extend(["--ro-bind", str(root / "Environment"), "/workspace/Environment"])
    executable = Path(sys.executable).resolve()
    runtime_root = executable.parent.parent
    if runtime_root.exists() and runtime_root not in {Path("/usr"), Path("/usr/local")}:
        args.extend(["--ro-bind", str(runtime_root), "/opt/igraph-runtime"])
        python_path = "/opt/igraph-runtime/bin:/usr/local/bin:/usr/bin:/bin"
    else:
        python_path = "/usr/local/bin:/usr/bin:/bin"
    manifest = _manifest_path(root)
    if manifest.exists():
        args.extend(["--ro-bind", str(manifest), "/workspace/" + _MANIFEST_NAME])
    args.extend(
        [
            "--clearenv",
            "--setenv",
            "HOME",
            "/workspace",
            "--setenv",
            "PATH",
            python_path,
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "PYTHONUNBUFFERED",
            "1",
            "--chdir",
            "/workspace",
            "/bin/bash",
            "-lc",
            command,
        ]
    )
    return args, {"PATH": python_path, "HOME": "/workspace", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}


def _fallback_command(root: Path, command: str) -> tuple[list[str], dict[str, str]]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/workspace", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}
    return ["/bin/bash", "-lc", command], env


def _sanitize_output(value: str, root: Path) -> str:
    return value.replace(str(root), "/workspace")


def _changed_metadata(root: Path, before: dict[str, tuple[str, int, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    after = _iter_files(root)
    created: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    protected_changed = False
    for key in sorted(set(after) | set(before)):
        if after.get(key) == before.get(key):
            continue
        relative = Path(key)
        zone = _zone(relative)
        if zone in _PROTECTED_ZONES:
            protected_changed = True
            continue
        path = root / relative
        if key not in after or not path.is_file() or path.is_symlink():
            continue
        try:
            item = _metadata(root, path, relative, zone="Ephemeral")
        except OSError:
            continue
        (created if key not in before else modified).append(item)
    return created, modified, protected_changed


def _relocate_new_root_files(root: Path, before: dict[str, tuple[str, int, str]]) -> None:
    """Place newly-created legacy root files in the Ephemeral zone."""

    after = _iter_files(root)
    for key, descriptor in after.items():
        relative = Path(key)
        if key in before or relative.parts[0] in _ZONES or descriptor[0] != "file":
            continue
        source = root / relative
        if source.is_symlink() or not source.is_file():
            continue
        destination = root / "Ephemeral" / relative
        counter = 2
        if destination.exists() or destination.is_symlink():
            stem, suffix = destination.stem, destination.suffix
            while destination.exists() or destination.is_symlink():
                destination = destination.with_name(f"{stem}.v{counter}{suffix}")
                counter += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))


def create_workspace_tools() -> list[Any]:
    async def workspace_list(run_context: RunContext) -> dict[str, Any]:
        """List user-visible Uploaded/Derived files; inspect Ephemeral using workspace_bash."""
        try:
            root = workspace_root(run_context)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "files": []}
        manifest = _load_manifest(root)
        files: list[dict[str, Any]] = []
        for key in sorted(_iter_files(root)):
            relative = Path(key)
            zone = _zone(relative)
            path = root / relative
            if zone not in {"Uploaded", "Derived"} or not path.is_file() or path.is_symlink():
                continue
            record = manifest.get(key)
            if zone == "Derived" and record:
                files.append(dict(record))
            else:
                files.append(_metadata(root, path, relative, zone=zone))
        return {"success": True, "files": files}

    async def workspace_bash(command: str, run_context: RunContext) -> dict[str, Any]:
        """Run Bash in the current session workspace to inspect/process files or run code.

        Each call starts a fresh shell at the workspace root; use relative paths.
        Uploaded, Derived and Environment are read-only. Write intermediate files
        to Ephemeral, then use workspace_publish to deliver them as user-visible
        artifacts. The default interpreter includes the server-compatible CUDA
        numpy, scipy, networkx, torch, torchvision and torch-geometric stack,
        shared by all sessions, so run code directly with
        "python Uploaded/script.py". When uploaded code needs extra packages, call
        workspace_env_ensure first and run it with the interpreter that returns,
        e.g. "Environment/venv/bin/python Uploaded/script.py". There is no
        network here. Probe torch.cuda.is_available() before GPU execution.
        Session environments inherit the shared Torch/CUDA stack; never replace
        it or install another Torch version. If CUDA is unavailable, memory is insufficient or a
        driver mismatch, report the failure and stop; do not claim success.
        Prefer head/sed or bounded Python reads for large files.
        Results contain exit_code, bounded output, output_truncated and changed
        file metadata. Commands have a server-configured timeout and inherit no
        provider secrets.
        """
        mode = _sandbox_mode()
        empty = {
            "success": False,
            "status": "rejected",
            "exit_code": None,
            "output": "",
            "output_truncated": False,
            "created_artifacts": [],
            "modified_artifacts": [],
            "artifacts": [],
            **_sandbox_fields(mode),
        }
        if not str(command or "").strip():
            return {**empty, "error": "命令不能为空"}
        try:
            root = workspace_root(run_context)
        except ValueError as exc:
            return {**empty, "error": str(exc)}
        if mode == "unavailable":
            logger.error(
                "workspace.exec sandbox_unavailable session_id=%s",
                run_context.session_id,
            )
            return _sandbox_unavailable_result()
        if _command_rejects_workspace_mutation(command):
            return {**empty, "error": "命令包含工作区外路径或 Uploaded 写入/删除操作"}
        before = _iter_files(root)
        protected_before = _protected_snapshot(root) if mode == "fallback" else {}
        def restore_protected():
            if mode == "fallback":
                _restore_protected(root, protected_before)
        if mode == "bwrap":
            sandbox = _sandbox_command(root, command)
            if sandbox is None:
                return _sandbox_unavailable_result()
            argv, env = sandbox
        else:
            argv, env = _fallback_command(root, command)
            logger.warning(
                "workspace.exec sandbox_mode=fallback session_id=%s security_level=development-compatibility",
                run_context.session_id,
                extra={
                    "sandbox_mode": "fallback",
                    "sandbox_security_level": "development-compatibility",
                    "workspace_session_id": str(run_context.session_id),
                },
            )
        try:
            timeout = max(
                0.01,
                float(os.getenv("IGRAPH_WORKSPACE_EXEC_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))),
            )
            output_limit = max(
                1,
                int(os.getenv("IGRAPH_WORKSPACE_OUTPUT_LIMIT", str(_DEFAULT_OUTPUT_LIMIT))),
            )
        except (TypeError, ValueError):
            restore_protected()
            return {**empty, "status": "failed", "error": "命令执行参数无效"}
        logger.info("workspace.exec session_id=%s command_sha256=%s", run_context.session_id, hashlib.sha256(str(command).encode()).hexdigest())
        proc: asyncio.subprocess.Process | None = None
        try:
            async def spawn():
                options = dict(cwd=root, env=env, stdout=asyncio.subprocess.PIPE,
                               stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                if mode == "bwrap":
                    with process_group_filter() as filter_fd:
                        return await asyncio.create_subprocess_exec(
                            argv[0], "--seccomp", str(filter_fd), *argv[1:],
                            pass_fds=(filter_fd,), **options)
                return await asyncio.create_subprocess_exec(*argv, **options)

            spawning = asyncio.create_task(spawn())
            proc = await asyncio.shield(spawning)

            async def reap_group():
                # Process.wait() also waits for inherited pipes to close.
                # Kill background children as soon as the main process exits.
                while proc.returncode is None:
                    await asyncio.sleep(0.02)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            reaper = asyncio.create_task(reap_group())
            async def drain_output():
                tail = bytearray()
                total = 0
                while chunk := await proc.stdout.read(65536):
                    total += len(chunk)
                    tail.extend(chunk)
                    if len(tail) > output_limit:
                        del tail[:-output_limit]
                await reaper
                await proc.wait()
                return bytes(tail), total > output_limit
            reader = asyncio.create_task(drain_output())
            try:
                output_bytes, was_truncated = await asyncio.wait_for(asyncio.shield(reader), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    proc.kill()
                output_bytes, was_truncated = await reader
                restore_protected()
                _relocate_new_root_files(root, before)
                created, modified, _ = _changed_metadata(root, before)
                output_text = _sanitize_output(output_bytes.decode(errors="replace"), root)
                output_truncated = was_truncated or len(output_text.encode()) > output_limit
                if output_truncated:
                    output_text = output_text[-output_limit:]
                return {
                    **empty,
                    "status": "timeout",
                    "error": f"命令执行超时（{timeout:g}秒）",
                    "output": output_text,
                    "output_truncated": output_truncated,
                    "created_artifacts": created,
                    "modified_artifacts": modified,
                    "artifacts": created + modified,
                }
        except asyncio.CancelledError:
            # A child may already exist when create_subprocess_exec is cancelled.
            # Own its creation through to completion before terminating its group.
            if proc is None and "spawning" in locals():
                proc = await asyncio.shield(spawning)
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    if "reader" in locals():
                        await asyncio.shield(reader)
                    else:
                        await proc.communicate()
                except (OSError, asyncio.CancelledError):
                    pass
            restore_protected()
            _relocate_new_root_files(root, before)
            raise
        except (OSError, ValueError):
            restore_protected()
            return {**empty, "status": "failed", "error": "受控命令执行失败"}
        _relocate_new_root_files(root, before)
        created, modified, protected_changed = _changed_metadata(root, before)
        protected_changed = protected_changed and mode == "fallback"
        if protected_changed:
            restore_protected()
        output_text = _sanitize_output(output_bytes.decode(errors="replace"), root)
        output_truncated = was_truncated or len(output_text.encode()) > output_limit
        if output_truncated:
            output_text = output_text[-output_limit:]
        if protected_changed:
            output_text = (output_text + "\nUploaded/Derived 文件为只读，已拒绝修改。").strip()
        result = {
            **_sandbox_fields(mode),
            "success": proc.returncode == 0 and not protected_changed,
            "status": "completed",
            "exit_code": proc.returncode,
            "output": output_text,
            "output_truncated": output_truncated,
            "created_artifacts": created,
            "modified_artifacts": modified,
            "artifacts": created + modified,
        }
        if protected_changed:
            result["error"] = "不能修改 Uploaded 或 Derived 文件"
        logger.info("workspace.exec.complete session_id=%s exit_code=%s", run_context.session_id, proc.returncode)
        return result

    async def workspace_publish(path: str, run_context: RunContext, name: str | None = None) -> dict[str, Any]:
        """Publish an Ephemeral file as a versioned Derived artifact without overwriting prior deliveries."""
        scope = current_scope.get()
        if scope is not None:
            scope.check()
        try:
            root, source, relative = _resolve(run_context, path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if _zone(relative) != "Ephemeral":
            return {"success": False, "error": "只能发布 Ephemeral 文件"}
        if not source.is_file() or source.is_symlink():
            return {"success": False, "error": "Ephemeral 文件不存在"}
        if name is not None:
            requested = str(name).strip()
            if not requested or Path(requested).name != requested or requested in {".", ".."}:
                return {"success": False, "error": "artifact 名称必须是当前工作区内的文件名"}
            safe_name = _SAFE_FILENAME.sub("_", requested).strip("._") or "artifact.bin"
            destination_relative = Path("Derived") / safe_name
        elif relative.parts[0] == "Ephemeral":
            destination_relative = Path("Derived", *relative.parts[1:])
        else:
            destination_relative = Path("Derived") / relative
        if _contains_symlink(root, destination_relative):
            return {"success": False, "error": "发布路径不能包含符号链接"}
        destination = root / destination_relative
        counter = 2
        if destination.exists() or destination.is_symlink():
            stem, suffix = destination.stem, destination.suffix
            while destination.exists() or destination.is_symlink():
                destination = destination.with_name(f"{stem}.v{counter}{suffix}")
                counter += 1
            destination_relative = destination.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".workspace-publish-", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        checksum = _checksum(destination)
        artifact_id = "artifact_" + hashlib.sha256(
            f"{destination_relative.as_posix()}:{checksum}".encode("utf-8")
        ).hexdigest()[:24]
        artifact = _metadata(root, destination, destination_relative, zone="Derived", artifact_id=artifact_id)
        records = _load_manifest(root)
        records[_public_path(destination_relative)] = artifact
        try:
            _save_manifest(root, records)
        except OSError:
            destination.unlink(missing_ok=True)
            return {"success": False, "error": "artifact 元数据保存失败"}
        return {"success": True, "artifact": artifact, **artifact}

    async def workspace_env_ensure(packages: list[str], run_context: RunContext) -> dict[str, Any]:
        """Prepare a Python environment for uploaded code, then report its interpreter.

        Give extra PyPI packages with optional versions, e.g. ["pandas"].
        The environment inherits the preinstalled shared Torch/CUDA stack;
        requests conflicting with its fixed versions fail. Do not reinstall Torch.
        Git refs, URLs and local paths are refused. Installation runs
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

    workspace_list.__name__ = "workspace_list"
    workspace_bash.__name__ = "workspace_bash"
    workspace_publish.__name__ = "workspace_publish"
    workspace_env_ensure.__name__ = "workspace_env_ensure"
    return [workspace_list, workspace_bash, workspace_publish, workspace_env_ensure]
