#!/usr/bin/env python3
"""Bridge one benchmark task envelope to the server's Codex Docker harness."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Sequence
import uuid

_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))

from benchmark_platform.agent_protocol import normalize_agent_payload

try:
    from .zhizengzeng_config import load_codex_zhizengzeng_config
except ImportError:  # Direct ``python scripts/...py`` execution.
    from scripts.zhizengzeng_config import load_codex_zhizengzeng_config


_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_BROKER_ENVIRONMENT_NAMES = (
    "IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT",
    "IGRAPH_VIRTUAL_ML_BROKER_TOKEN",
)
_CPU_VALUE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,3})?\Z")
_CPUSET_VALUE = re.compile(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*\Z")
_MEMORY_VALUE = re.compile(r"[1-9][0-9]*[bkmg]\Z", re.IGNORECASE)
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_CONTAINER_OWNER = re.compile(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)\Z")
_WATCHDOG_MODE = "__lease_watchdog__"
_DOCKER_SHIM_MODE = "__docker_exec_taskset_shim__"
_CODEX_WIRE_API = "responses"
_MANAGED_LABEL = "com.igraph.benchmark.managed"
_LEASE_LABEL = "com.igraph.benchmark.lease"
_TASK_LABEL = "com.igraph.benchmark.task"
_REDACTED = "[REDACTED]"
_ACTIVE_REDACTION_SECRETS: set[str] = set()

_DOCKER_EXEC_OPTIONS_WITH_VALUE = frozenset(
    {
        "--detach-keys",
        "--env",
        "--env-file",
        "--user",
        "--workdir",
        "-e",
        "-u",
        "-w",
    }
)
_DOCKER_EXEC_FLAG_OPTIONS = frozenset(
    {
        "--detach",
        "--interactive",
        "--privileged",
        "--tty",
        "-d",
        "-i",
        "-t",
    }
)
_DOCKER_EXEC_SHORT_FLAGS = frozenset("dit")
_DOCKER_EXEC_SHORT_VALUE_OPTIONS = frozenset("euw")


def _responses_wire_api_for_model(model: str) -> str:
    """Select the Codex Responses transport for every requested model."""

    _ = model
    return _CODEX_WIRE_API


def _tool_output_requires_terminal_failure(tool_text: Any) -> bool:
    """Keep recoverable intermediate tool failures out of the terminal verdict.

    The upstream harness accumulates every output accepted by its marker hook and
    rejects the whole task at the end if that history is non-empty.  A later tool
    call can legitimately recover from an earlier non-zero exit or parse error,
    so no individual historical output is sufficient to decide terminal failure.
    Per-call structured success is retained by ``normalize_agent_payload``.
    """

    _ = tool_text
    return False


class AdapterCancelled(RuntimeError):
    """Catchable cancellation used to unwind into the cleanup ``finally``."""


def _redact_secrets(value: Any, secrets: Sequence[str]) -> Any:
    exact = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))

    def redact(current: Any) -> Any:
        if isinstance(current, str):
            for secret in exact:
                current = current.replace(secret, _REDACTED)
            return current
        if isinstance(current, dict):
            return {
                redact(key) if isinstance(key, str) else key: redact(nested)
                for key, nested in current.items()
            }
        if isinstance(current, (list, tuple)):
            return [redact(nested) for nested in current]
        return current

    return redact(value)


def _container_cpus(value: str) -> str:
    candidate = str(value)
    if not _CPU_VALUE.fullmatch(candidate):
        raise argparse.ArgumentTypeError(
            "container-cpus must be a plain positive decimal with at most 3 decimals"
        )
    try:
        parsed = Decimal(candidate)
    except InvalidOperation as exc:  # Defensive; the regexp is stricter.
        raise argparse.ArgumentTypeError("container-cpus is invalid") from exc
    if parsed <= 0 or parsed > Decimal("1024"):
        raise argparse.ArgumentTypeError("container-cpus must be within (0, 1024]")
    return candidate


def _container_cpuset_cpus(value: str) -> str:
    candidate = str(value)
    if not _CPUSET_VALUE.fullmatch(candidate):
        raise argparse.ArgumentTypeError(
            "container-cpuset-cpus must look like 0-3,8,10-11 without spaces"
        )
    intervals: list[tuple[int, int]] = []
    for component in candidate.split(","):
        if "-" in component:
            start_text, end_text = component.split("-", 1)
        else:
            start_text = end_text = component
        start, end = int(start_text), int(end_text)
        if end < start:
            raise argparse.ArgumentTypeError(
                "container-cpuset-cpus ranges must be increasing"
            )
        if end > 1_048_575:
            raise argparse.ArgumentTypeError(
                "container-cpuset-cpus contains an unreasonable CPU index"
            )
        intervals.append((start, end))
    intervals.sort()
    if any(
        current[0] <= previous[1]
        for previous, current in zip(intervals, intervals[1:])
    ):
        raise argparse.ArgumentTypeError(
            "container-cpuset-cpus must not contain duplicate or overlapping CPUs"
        )
    return candidate


def _container_memory(value: str) -> str:
    candidate = str(value)
    if not _MEMORY_VALUE.fullmatch(candidate):
        raise argparse.ArgumentTypeError(
            "container-memory must be an integer followed by b, k, m, or g"
        )
    normalized = candidate.lower()
    multiplier = {
        "b": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
    }[normalized[-1]]
    byte_limit = int(normalized[:-1]) * multiplier
    if byte_limit < 6 * 1024**2 or byte_limit > 1024**4:
        raise argparse.ArgumentTypeError(
            "container-memory must be between 6m and 1024g"
        )
    return normalized


def _container_pids_limit(value: str) -> int:
    candidate = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", candidate):
        raise argparse.ArgumentTypeError(
            "container-pids-limit must be a positive integer"
        )
    parsed = int(candidate, 10)
    if parsed > 1_000_000:
        raise argparse.ArgumentTypeError(
            "container-pids-limit must be within [1, 1000000]"
        )
    return parsed


def _positive_docker_timeout(value: str) -> float:
    candidate = str(value)
    if not re.fullmatch(r"(?:[1-9][0-9]*|0\.[0-9]*[1-9][0-9]*)", candidate):
        raise argparse.ArgumentTypeError("Docker timeout must be a positive decimal")
    parsed_decimal = Decimal(candidate)
    if parsed_decimal > Decimal("600"):
        raise argparse.ArgumentTypeError("Docker timeout must not exceed 600 seconds")
    parsed = float(parsed_decimal)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Docker timeout is too small")
    return parsed


def _rewrite_docker_cli_for_taskset(
    arguments: Sequence[str],
    cpuset_cpus: str,
) -> list[str]:
    """Pin every PATH-dispatched ``docker exec`` workload to one CPU set.

    Docker exec options must stay before the container identifier, while
    ``taskset`` must be inserted immediately after it. Unknown exec options are
    rejected instead of risking an unpinned workload because it is ambiguous
    whether the following token is an option value or the container name.
    """

    cpuset = _container_cpuset_cpus(cpuset_cpus)
    rewritten = list(arguments)
    if not rewritten or rewritten[0] != "exec":
        return rewritten

    prefix = ["exec"]
    index = 1
    while index < len(rewritten):
        option = rewritten[index]
        if option == "--":
            prefix.append(option)
            index += 1
            break
        if option == "-" or not option.startswith("-"):
            break

        if option in _DOCKER_EXEC_OPTIONS_WITH_VALUE:
            if index + 1 >= len(rewritten):
                raise ValueError(f"docker exec option requires a value: {option}")
            prefix.extend((option, rewritten[index + 1]))
            index += 2
            continue
        if option in _DOCKER_EXEC_FLAG_OPTIONS:
            prefix.append(option)
            index += 1
            continue
        if option.startswith("--") and "=" in option:
            long_name = option.split("=", 1)[0]
            if long_name in (
                _DOCKER_EXEC_OPTIONS_WITH_VALUE | _DOCKER_EXEC_FLAG_OPTIONS
            ):
                prefix.append(option)
                index += 1
                continue
        if option.startswith("-") and not option.startswith("--"):
            short_body = option[1:]
            if short_body and all(
                character in _DOCKER_EXEC_SHORT_FLAGS for character in short_body
            ):
                prefix.append(option)
                index += 1
                continue
            if (
                len(short_body) >= 2
                and short_body[0] in _DOCKER_EXEC_SHORT_VALUE_OPTIONS
            ):
                prefix.append(option)
                index += 1
                continue
            if (
                len(short_body) > 2
                and short_body[0] in _DOCKER_EXEC_SHORT_FLAGS
                and short_body[1] == "="
            ):
                prefix.append(option)
                index += 1
                continue
        raise ValueError(f"unsupported docker exec option: {option}")

    if index >= len(rewritten):
        raise ValueError("docker exec is missing a container identifier")
    container = rewritten[index]
    command = rewritten[index + 1 :]
    if not command:
        raise ValueError("docker exec is missing its workload command")
    return [
        *prefix,
        container,
        "taskset",
        "-c",
        cpuset,
        *command,
    ]


def _docker_shim_main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("docker affinity shim requires Docker and cpuset arguments\n")
        return 125
    try:
        docker = Path(argv[0]).resolve(strict=True)
        if not docker.is_file() or not os.access(docker, os.X_OK):
            raise ValueError("real Docker executable is unavailable")
        rewritten = _rewrite_docker_cli_for_taskset(argv[2:], argv[1])
        os.execv(str(docker), [str(docker), *rewritten])
    except (
        argparse.ArgumentError,
        argparse.ArgumentTypeError,
        OSError,
        ValueError,
    ) as exc:
        sys.stderr.write(f"docker affinity shim error: {exc}\n")
        return 125
    return 125  # ``execv`` never returns after a successful handoff.


def _write_docker_exec_taskset_shim(
    directory: Path,
    *,
    docker: str,
    cpuset_cpus: str,
) -> Path:
    """Create a private, argv-preserving Docker launcher without ``eval``."""

    cpuset = _container_cpuset_cpus(cpuset_cpus)
    real_docker = Path(docker).resolve(strict=True)
    if not real_docker.is_file() or not os.access(real_docker, os.X_OK):
        raise ValueError("real Docker executable is unavailable")
    python = Path(sys.executable).resolve(strict=True)
    adapter = Path(__file__).resolve(strict=True)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(directory, 0o700)
    shim = directory / "docker"
    fixed_arguments = (
        str(python),
        "-I",
        str(adapter),
        _DOCKER_SHIM_MODE,
        str(real_docker),
        cpuset,
    )
    launcher = (
        "#!/bin/sh\nexec "
        + " ".join(shlex.quote(value) for value in fixed_arguments)
        + ' "$@"\n'
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(shim, flags, 0o700)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(launcher)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return shim


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-root", type=Path, required=True)
    parser.add_argument("--skills-dir", type=Path, required=True)
    parser.add_argument("--provider-profile", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--image", default="sandbox_codex:latest")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--container-cpus",
        type=_container_cpus,
        default=None,
        help=(
            "optional Docker CFS CPU quota; omit it for rootless daemons that "
            "do not support NanoCPUs"
        ),
    )
    parser.add_argument(
        "--container-cpuset-cpus",
        type=_container_cpuset_cpus,
        required=True,
        help="required hard CPU-affinity limit for every benchmark container",
    )
    parser.add_argument("--container-memory", type=_container_memory, default="8g")
    parser.add_argument(
        "--container-pids-limit",
        type=_container_pids_limit,
        default=512,
    )
    parser.add_argument(
        "--docker-command-timeout-seconds",
        type=_positive_docker_timeout,
        default=60.0,
        help="timeout for short docker run/exec/rm control commands",
    )
    return parser.parse_args(argv)


def _watchdog_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--read-fd", type=int, required=True)
    parser.add_argument("--attempt-root", type=Path, required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--cidfile", type=Path, required=True)
    parser.add_argument("--audit-file", type=Path, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--adapter-pid", type=int, required=True)
    parser.add_argument("--docker", required=True)
    parser.add_argument(
        "--docker-timeout-seconds",
        type=_positive_docker_timeout,
        required=True,
    )
    return parser.parse_args(argv)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _lease_audit(
    path: Path,
    *,
    state: str,
    lease_id: str,
    container_name: str,
    cidfile: Path,
    detail: str | None = None,
    adapter_pid: int | None = None,
) -> None:
    _atomic_json(
        path,
        {
            "schema": "igraph.benchmark.container_lease.v1",
            "state": state,
            "lease_id": lease_id,
            "container_name": container_name,
            "cidfile": str(cidfile),
            "adapter_pid": os.getpid() if adapter_pid is None else adapter_pid,
            "updated_unix_seconds": time.time(),
            "detail": detail,
        },
    )


def _best_effort_lease_audit(path: Path, **fields: Any) -> None:
    # An unwritable audit file must never prevent the watchdog from removing a
    # live managed container or its secret-bearing environment file.
    try:
        _lease_audit(path, **fields)
    except Exception:
        pass


def _cid_from_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii", errors="strict").strip().lower()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    return value if _CONTAINER_ID.fullmatch(value) else None


def _docker_lease_targets(
    docker: str,
    *,
    lease_id: str,
    timeout_seconds: float,
) -> list[str]:
    try:
        completed = subprocess.run(
            [
                docker,
                "ps",
                "-aq",
                "--filter",
                f"label={_MANAGED_LABEL}=true",
                "--filter",
                f"label={_LEASE_LABEL}={lease_id}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [
        value
        for value in (line.strip().lower() for line in completed.stdout.splitlines())
        if _CONTAINER_ID.fullmatch(value)
    ]


def _force_remove_container(
    docker: str,
    target: str,
    *,
    timeout_seconds: float,
) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [docker, "rm", "-f", target],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "docker rm timed out"
    except OSError as exc:
        return False, f"docker rm failed: {type(exc).__name__}"
    diagnostic = (completed.stderr or completed.stdout).strip()[-2000:]
    if completed.returncode == 0 or "no such container" in diagnostic.casefold():
        return True, diagnostic
    return False, diagnostic or f"docker rm exited {completed.returncode}"


def _restore_workspace_ownership(
    docker: str,
    container: str,
    workspace_exec: Path,
    dataset_target: Path,
    *,
    attempt_root: Path,
    container_owner: str,
    timeout_seconds: float,
) -> tuple[bool, str]:
    resolved_root = attempt_root.resolve()
    resolved_exec = workspace_exec.resolve()
    if resolved_exec == resolved_root or not resolved_exec.is_relative_to(resolved_root):
        return False, "refused to chown a path outside the attempt workspace"
    resolved_dataset = dataset_target.resolve()
    if resolved_dataset.parent != resolved_exec:
        return False, "refused to chown with a dataset outside the task exec directory"
    if not _CONTAINER_OWNER.fullmatch(container_owner):
        return False, "refused to chown with an invalid container-visible owner"
    # The dataset is a second, read-only bind mounted over a child of the RW
    # workspace bind.  Explicitly prune it, stay on the workspace filesystem,
    # and never follow task-created symlinks.  Restore owner permissions on
    # directories as well: an agent may have made one non-writable even after
    # ownership itself has been repaired.
    ownership_script = (
        'set -eu\n'
        '/usr/bin/find "$1" -xdev -path "$2" -prune -o '
        '-exec /bin/chown -h "$3" -- {} +\n'
        '/usr/bin/find "$1" -xdev -path "$2" -prune -o -type d '
        '-exec /bin/chmod u+rwx -- {} +'
    )
    try:
        completed = subprocess.run(
            [
                docker,
                "exec",
                container,
                "/bin/bash",
                "-c",
                ownership_script,
                "igraph-restore-ownership",
                str(resolved_exec),
                str(resolved_dataset),
                container_owner,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "workspace ownership restore timed out"
    except OSError as exc:
        return False, f"workspace ownership restore failed: {type(exc).__name__}"
    diagnostic = (completed.stderr or completed.stdout).strip()[-2000:]
    if completed.returncode == 0:
        return True, diagnostic
    return False, diagnostic or f"workspace chown exited {completed.returncode}"


def _capture_workspace_owner(
    docker: str,
    container: str,
    workspace_exec: Path,
    *,
    attempt_root: Path,
    timeout_seconds: float,
) -> tuple[str | None, str]:
    """Capture the bind mount owner as seen inside this container's userns.

    Host UIDs must not be passed directly to a container-side ``chown``.  In a
    rootless or remapped user namespace the same numeric UID denotes a different
    host account.  The host creates ``workspace_exec`` before container startup,
    so its container-visible owner is the exact identity that cleanup must use.
    Capture it before the agent can change the mount root itself.
    """

    resolved_root = attempt_root.resolve()
    resolved_exec = workspace_exec.resolve()
    if resolved_exec == resolved_root or not resolved_exec.is_relative_to(resolved_root):
        return None, "refused to stat a path outside the attempt workspace"
    try:
        completed = subprocess.run(
            [
                docker,
                "exec",
                container,
                "/usr/bin/stat",
                "-c",
                "%u:%g",
                "--",
                str(resolved_exec),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, "workspace owner capture timed out"
    except OSError as exc:
        return None, f"workspace owner capture failed: {type(exc).__name__}"
    diagnostic = (completed.stderr or completed.stdout).strip()[-2000:]
    if completed.returncode != 0:
        return None, diagnostic or f"workspace stat exited {completed.returncode}"
    owner = completed.stdout.strip()
    if not _CONTAINER_OWNER.fullmatch(owner):
        return None, "workspace stat returned an invalid owner"
    return owner, ""


def _remove_environment_file(
    path: Path,
    *,
    attempt_root: Path,
) -> tuple[bool, str]:
    """Remove only this adapter's exact secret-bearing temporary env file."""

    target = path.absolute()
    expected_parent = (attempt_root.absolute() / "codex-workspace")
    if (
        target.parent != expected_parent
        or not target.name.startswith(".igraph-container-env-")
    ):
        return False, f"refused to remove unsafe environment file: {target}"
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        return False, (
            f"cannot remove container environment file: {type(exc).__name__}: {exc}"
        )
    if target.exists() or target.is_symlink():
        return False, f"container environment file still exists: {target}"
    return True, ""


def _remove_attempt_tree(path: Path, *, attempt_root: Path) -> tuple[bool, str]:
    resolved_root = attempt_root.resolve()
    target = path.absolute()
    if target == resolved_root or not target.is_relative_to(resolved_root):
        return False, f"refused to remove unsafe cleanup path: {target}"
    try:
        if target.is_symlink():
            target.unlink()
            return False, f"unexpected cleanup symlink was unlinked: {target}"
        if target.exists():
            shutil.rmtree(target)
    except OSError as exc:
        return False, f"cannot remove {target}: {type(exc).__name__}: {exc}"
    if target.exists() or target.is_symlink():
        return False, f"cleanup path still exists: {target}"
    return True, ""


def _watchdog_main(argv: Sequence[str]) -> int:
    args = _watchdog_arguments(argv)
    clean_shutdown = False
    try:
        while True:
            chunk = os.read(args.read_fd, 64)
            if not chunk:
                break
            clean_shutdown = True
    except OSError:
        clean_shutdown = False
    finally:
        try:
            os.close(args.read_fd)
        except OSError:
            pass
    if clean_shutdown:
        environment_removed, _ = _remove_environment_file(
            args.environment_file,
            attempt_root=args.attempt_root,
        )
        return 0 if environment_removed else 1

    _best_effort_lease_audit(
        args.audit_file,
        state="adapter_disappeared",
        lease_id=args.lease_id,
        container_name=args.container_name,
        cidfile=args.cidfile,
        adapter_pid=args.adapter_pid,
    )
    # The adapter may have died anywhere between creating this exact file and
    # returning from ``docker run``.  It is no longer needed by the dead
    # process, so remove it immediately and retry again on every terminal path.
    _remove_environment_file(
        args.environment_file,
        attempt_root=args.attempt_root,
    )
    deadline = time.monotonic() + 30.0
    last_detail = "container CID/label was not observed"
    observed_container = False
    while time.monotonic() < deadline:
        targets: list[str] = []
        cid = _cid_from_file(args.cidfile)
        if cid is not None:
            targets.append(cid)
        targets.extend(
            target
            for target in _docker_lease_targets(
                args.docker,
                lease_id=args.lease_id,
                timeout_seconds=args.docker_timeout_seconds,
            )
            if target not in targets
        )
        if targets:
            observed_container = True
            outcomes = [
                _force_remove_container(
                    args.docker,
                    target,
                    timeout_seconds=args.docker_timeout_seconds,
                )
                for target in targets
            ]
            succeeded = all(outcome[0] for outcome in outcomes)
            last_detail = "; ".join(outcome[1] for outcome in outcomes if outcome[1])
            if succeeded:
                environment_removed, environment_detail = _remove_environment_file(
                    args.environment_file,
                    attempt_root=args.attempt_root,
                )
                if environment_detail:
                    last_detail = "; ".join(
                        part for part in (last_detail, environment_detail) if part
                    )
                _best_effort_lease_audit(
                    args.audit_file,
                    state=(
                        "orphan_cleanup_succeeded"
                        if environment_removed
                        else "orphan_cleanup_failed"
                    ),
                    lease_id=args.lease_id,
                    container_name=args.container_name,
                    cidfile=args.cidfile,
                    detail=last_detail or None,
                    adapter_pid=args.adapter_pid,
                )
                return 0 if environment_removed else 1
        time.sleep(0.5)
    environment_removed, environment_detail = _remove_environment_file(
        args.environment_file,
        attempt_root=args.attempt_root,
    )
    if environment_detail:
        last_detail = "; ".join(
            part for part in (last_detail, environment_detail) if part
        )
    _best_effort_lease_audit(
        args.audit_file,
        state=(
            "orphan_cleanup_failed"
            if observed_container or not environment_removed
            else "orphan_not_found"
        ),
        lease_id=args.lease_id,
        container_name=args.container_name,
        cidfile=args.cidfile,
        detail=last_detail,
        adapter_pid=args.adapter_pid,
    )
    return 1 if observed_container or not environment_removed else 0


@dataclass(slots=True)
class _ContainerLease:
    task_id: str
    lease_id: str
    attempt_root: Path
    environment_file: Path
    cidfile: Path
    audit_file: Path
    docker: str
    docker_timeout_seconds: float
    notify_fd: int
    watchdog: subprocess.Popen[Any]

    def finish(self, *, cleanup_succeeded: bool, detail: str | None = None) -> None:
        try:
            _best_effort_lease_audit(
                self.audit_file,
                state=(
                    "container_cleanup_succeeded"
                    if cleanup_succeeded
                    else "container_cleanup_delegated"
                ),
                lease_id=self.lease_id,
                container_name=self.task_id,
                cidfile=self.cidfile,
                detail=detail,
            )
            try:
                if cleanup_succeeded:
                    os.write(self.notify_fd, b"clean")
            except OSError:
                pass
        finally:
            try:
                os.close(self.notify_fd)
            except OSError:
                pass
        if cleanup_succeeded:
            try:
                self.watchdog.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                # The clean marker is authoritative. The watchdog owns no
                # container resources and will exit after consuming the pipe.
                pass


def _start_container_lease(
    *,
    attempt_root: Path,
    environment_file: Path,
    task_id: str,
    docker: str,
    docker_timeout_seconds: float,
) -> _ContainerLease:
    if os.name != "posix":
        raise RuntimeError("container lease watchdog requires a POSIX host")
    lease_id = uuid.uuid4().hex
    cidfile = attempt_root / "codex-container.cid"
    audit_file = attempt_root / "container-lease.json"
    cidfile.unlink(missing_ok=True)
    read_fd, notify_fd = os.pipe()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        _WATCHDOG_MODE,
        "--read-fd",
        str(read_fd),
        "--attempt-root",
        str(attempt_root),
        "--environment-file",
        str(environment_file),
        "--cidfile",
        str(cidfile),
        "--audit-file",
        str(audit_file),
        "--container-name",
        task_id,
        "--lease-id",
        lease_id,
        "--adapter-pid",
        str(os.getpid()),
        "--docker",
        docker,
        "--docker-timeout-seconds",
        str(docker_timeout_seconds),
    ]
    try:
        watchdog = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(read_fd,),
            start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        os.close(notify_fd)
        raise
    os.close(read_fd)
    try:
        _lease_audit(
            audit_file,
            state="watchdog_started",
            lease_id=lease_id,
            container_name=task_id,
            cidfile=cidfile,
        )
    except BaseException:
        try:
            os.write(notify_fd, b"clean")
        except OSError:
            pass
        os.close(notify_fd)
        try:
            watchdog.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    return _ContainerLease(
        task_id=task_id,
        lease_id=lease_id,
        attempt_root=attempt_root,
        environment_file=environment_file,
        cidfile=cidfile,
        audit_file=audit_file,
        docker=docker,
        docker_timeout_seconds=docker_timeout_seconds,
        notify_fd=notify_fd,
        watchdog=watchdog,
    )


def _raise_cancellation(signum: int, _frame: Any) -> None:
    # Ignore a second soft signal so it cannot interrupt the cleanup finally.
    for candidate in (signal.SIGTERM, signal.SIGINT):
        signal.signal(candidate, signal.SIG_IGN)
    try:
        signal_name = signal.Signals(signum).name
    except ValueError:
        signal_name = str(signum)
    raise AdapterCancelled(f"adapter cancelled by {signal_name}")


def _install_cancellation_handlers() -> None:
    signal.signal(signal.SIGTERM, _raise_cancellation)
    signal.signal(signal.SIGINT, _raise_cancellation)


def _strict_envelope() -> dict[str, Any]:
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        raise ValueError("benchmark task envelope must be a JSON object")
    if payload.get("schema") != "igraph.benchmark.agent_task.v1":
        raise ValueError("unsupported benchmark task envelope schema")
    if not isinstance(payload.get("question"), str) or not payload["question"].strip():
        raise ValueError("benchmark task envelope has no question")
    return payload


def _jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw.strip():
            continue
        value = json.loads(raw)
        if isinstance(value, dict):
            events.append(value)
    if not events:
        raise ValueError("Codex harness produced no transcript events")
    return events


def _safe_task_id(envelope: dict[str, Any]) -> str:
    identity = f"{envelope.get('question_id')}#{envelope.get('attempt')}#{os.getpid()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"igraph-{digest}"


def _container_graph_ml_tool(skills_dir: Path) -> str:
    skill_name = skills_dir.name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", skill_name):
        raise ValueError(f"unsafe graph analysis skill directory name: {skill_name!r}")
    return f"/root/.codex/skills/{skill_name}/graph_ml_tools.py"


def _link_dataset(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _copy_runtime_package(release_root: Path, workspace_exec: Path) -> None:
    source = release_root / "benchmark_platform"
    if not source.is_dir():
        raise FileNotFoundError(f"benchmark runtime package is missing: {source}")
    shutil.copytree(
        source,
        workspace_exec / "benchmark_platform",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _prompt(envelope: dict[str, Any], dataset_name: str) -> str:
    return (
        "Solve the graph benchmark question below. The dataset file is "
        f"{dataset_name!r} in the current working directory. Use the supplied "
        "graph_analysis_skills command-line tools for graph inspection and "
        "computation so every tool action is recorded. For graph-ML questions, "
        "run the matching virtual train action before the infer action. Do not "
        "inspect benchmark metadata, answer files, or parent directories. End "
        "with a clear final answer to the original question.\n\n"
        f"Original question:\n{envelope['question']}"
    )


def _emit(payload: dict[str, Any]) -> None:
    redacted = _redact_secrets(payload, tuple(_ACTIVE_REDACTION_SECRETS))
    sys.stdout.write(
        json.dumps(
            redacted,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _docker_container_command(
    args: argparse.Namespace,
    *,
    docker: str,
    task_id: str,
    lease: _ContainerLease,
    environment_file: Path,
    exec_path: Path,
    dataset_target: Path,
    config_path: Path | None,
) -> list[str]:
    command = [
        docker,
        "run",
        "-d",
        "--rm",
        "--name",
        task_id,
        "--cidfile",
        str(lease.cidfile),
        "--label",
        f"{_MANAGED_LABEL}=true",
        "--label",
        f"{_LEASE_LABEL}={lease.lease_id}",
        "--label",
        f"{_TASK_LABEL}={task_id}",
        "--memory",
        args.container_memory,
        "--memory-swap",
        args.container_memory,
        "--pids-limit",
        str(args.container_pids_limit),
        "--network",
        "host",
        "--shm-size",
        "4g",
        "--add-host",
        "host.docker.internal:host-gateway",
        # No GPU device is requested. These values also disable exposure when
        # the daemon has the NVIDIA runtime configured as its default runtime.
        "--env",
        "NVIDIA_VISIBLE_DEVICES=void",
        "--env",
        "CUDA_VISIBLE_DEVICES=-1",
        "--env-file",
        str(environment_file),
        "-v",
        f"{exec_path}:{exec_path}:rw",
        "-v",
        f"{dataset_target}:{dataset_target}:ro",
    ]
    command.extend(["--cpuset-cpus", args.container_cpuset_cpus])
    if args.container_cpus is not None:
        command.extend(["--cpus", args.container_cpus])
    if config_path is not None and config_path.is_file():
        command.extend(["-v", f"{config_path.resolve()}:/root/.codex/config.toml:ro"])
    command.extend([args.image, "/bin/bash", "-c", "tail -f /dev/null"])
    return command


def _result_from_transcript(
    transcript: list[dict[str, Any]],
    *,
    execution_error: str | None,
    cleanup_errors: Sequence[str],
    usage: dict[str, Any],
    model: str,
    cleanup_warnings: Sequence[str] = (),
) -> dict[str, Any]:
    normalized = normalize_agent_payload(transcript) if transcript else None
    common = {
        "final_answer": normalized.final_answer if normalized is not None else None,
        "tool_calls": normalized.tool_calls if normalized is not None else [],
        "transcript": transcript,
        "warnings": (
            [
                {
                    "type": "ContainerWorkspaceCleanupWarning",
                    "message": "; ".join(cleanup_warnings),
                }
            ]
            if cleanup_warnings
            else []
        ),
    }
    if cleanup_errors:
        return {
            "status": "failed",
            "error": {
                "type": "ContainerCleanupError",
                "message": "; ".join(cleanup_errors),
            },
            "execution_error": execution_error,
            **common,
        }
    if execution_error is not None:
        return {
            "status": "failed",
            "error": {"type": "CodexHarnessError", "message": execution_error},
            **common,
        }
    if normalized is None:
        raise ValueError("Codex harness completed without a transcript")
    if normalized.status != "success" or normalized.final_answer is None:
        return {
            "status": "failed",
            "error": {
                "type": "InvalidCodexTranscript",
                "message": "Codex transcript contains no valid final answer",
            },
            **common,
        }
    return {
        "status": "success",
        **common,
        "reasoning": {
            "provider": "zhizengzeng",
            "model": model,
            "usage": usage,
        },
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    envelope = _strict_envelope()
    harness_root = args.harness_root.expanduser().resolve()
    skills_dir = args.skills_dir.expanduser().resolve()
    release_root = _RELEASE_ROOT
    if not (harness_root / "src" / "agents" / "codex" / "runner.py").is_file():
        raise FileNotFoundError("Codex harness root is invalid")
    if not (skills_dir / "SKILL.md").is_file():
        raise FileNotFoundError("graph analysis skill directory is invalid")
    if not args.model.strip():
        raise ValueError("model must be non-empty")
    if (
        not isinstance(args.image, str)
        or not args.image.strip()
        or args.image.startswith("-")
        or any(character in args.image for character in "\r\n\x00")
    ):
        raise ValueError("image must be a safe non-empty Docker image reference")
    if args.timeout_seconds < 1:
        raise ValueError("timeout-seconds must be positive")

    provider = load_codex_zhizengzeng_config(args.provider_profile)
    _ACTIVE_REDACTION_SECRETS.add(provider.api_key)
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError("docker executable is unavailable")
    sys.path.insert(0, str(harness_root))
    from src.agents.base import AgentTaskSpec  # type: ignore[import-not-found]
    from src.agents.codex.runner import CodexAgent  # type: ignore[import-not-found]

    attempt_root = Path.cwd().resolve()
    workspace_root = attempt_root / "codex-workspace"
    workspace_exec = workspace_root / "exec"
    harness_output = attempt_root / "codex-harness"
    workspace_exec.mkdir(parents=True, exist_ok=False)
    harness_output.mkdir(parents=True, exist_ok=False)

    try:
        dataset_source = Path(str(envelope.get("dataset_path") or "")).resolve()
        if not dataset_source.is_file():
            raise FileNotFoundError("benchmark dataset path is unavailable")
        dataset_target = workspace_exec / dataset_source.name
        _link_dataset(dataset_source, dataset_target)
        _copy_runtime_package(release_root, workspace_exec)
        docker_shim = _write_docker_exec_taskset_shim(
            workspace_root / ".host-docker-shim",
            docker=docker,
            cpuset_cpus=args.container_cpuset_cpus,
        )
    except BaseException as exc:
        cleanup_failures = [
            detail
            for path in (workspace_root, harness_output)
            for removed, detail in (
                _remove_attempt_tree(path, attempt_root=attempt_root),
            )
            if not removed
        ]
        if cleanup_failures:
            raise RuntimeError(
                "adapter setup failed and temporary cleanup was incomplete: "
                + "; ".join(cleanup_failures)
            ) from exc
        raise

    container_started = False
    workspace_container_owner: str | None = None
    environment_file: Path | None = None
    lease: _ContainerLease | None = None
    cleanup_errors: list[str] = []
    cleanup_warnings: list[str] = []

    class SharedWorkspaceCodexAgent(CodexAgent):
        @staticmethod
        def _text_contains_failed_tool_marker(tool_text: str) -> bool:
            return _tool_output_requires_terminal_failure(tool_text)

        def _default_wire_api_for_model(self, model: str) -> str:
            # The server harness otherwise selects the legacy Chat Completions
            # transport for this model.  zhizengzeng exposes the OpenAI
            # Responses-compatible endpoint, so make the rendered Codex
            # provider config use that transport explicitly.
            return _responses_wire_api_for_model(model)

        def _start_container(
            self,
            task_id: str,
            workspace_path: str,
            task: dict[str, Any],
            lobster: dict[str, Any] | None,
            config_path: Path | None = None,
        ) -> None:
            nonlocal container_started, environment_file, lease, workspace_container_owner
            _ = lobster
            exec_path = (Path(workspace_path).expanduser().resolve() / "exec")
            if not exec_path.is_dir():
                raise RuntimeError("shared Codex workspace is unavailable")
            env_map = {
                "OPENROUTER_API_KEY": self.openrouter_api_key,
                "OPENROUTER_BASE_URL": self.openrouter_base_url,
                "TMPDIR": "/tmp",
            }
            for name in str(task.get("env") or "").splitlines():
                name = name.strip()
                if not name:
                    continue
                if not _ENVIRONMENT_NAME.fullmatch(name):
                    raise ValueError(f"invalid container environment name: {name!r}")
                value = os.environ.get(name, "")
                if value:
                    env_map[name] = value
            for name, value in env_map.items():
                if "\n" in value or "\r" in value:
                    raise ValueError(f"container environment value is invalid: {name}")
            if lease is not None:
                raise RuntimeError("Codex container lease was already initialized")
            environment_file = (
                exec_path.parent / f".igraph-container-env-{uuid.uuid4().hex}"
            )
            lease = _start_container_lease(
                attempt_root=attempt_root,
                environment_file=environment_file,
                task_id=task_id,
                docker=docker,
                docker_timeout_seconds=args.docker_command_timeout_seconds,
            )
            environment_fd: int | None = None
            try:
                open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    open_flags |= os.O_NOFOLLOW
                environment_fd = os.open(
                    environment_file,
                    open_flags,
                    0o600,
                )
                os.chmod(environment_file, 0o600)
                with os.fdopen(environment_fd, "w", encoding="utf-8") as stream:
                    environment_fd = None
                    for name, value in env_map.items():
                        if value:
                            stream.write(f"{name}={value}\n")
                command = _docker_container_command(
                    args,
                    docker=docker,
                    task_id=task_id,
                    lease=lease,
                    environment_file=environment_file,
                    exec_path=exec_path,
                    dataset_target=dataset_target,
                    config_path=config_path,
                )
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=args.docker_command_timeout_seconds,
                )
                if completed.returncode == 0:
                    container_started = True
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Codex container startup timed out") from exc
            finally:
                if environment_fd is not None:
                    try:
                        os.close(environment_fd)
                    except OSError:
                        pass
                if environment_file is not None:
                    _remove_environment_file(
                        environment_file,
                        attempt_root=attempt_root,
                    )
            if completed.returncode != 0:
                raise RuntimeError(
                    "Codex container startup failed: "
                    + completed.stderr.strip()[-2000:]
                )
            cid = _cid_from_file(lease.cidfile)
            if cid is None:
                raise RuntimeError("Codex container started without a valid CID file")
            workspace_container_owner, owner_detail = _capture_workspace_owner(
                str(docker_shim),
                task_id,
                exec_path,
                attempt_root=attempt_root,
                timeout_seconds=args.docker_command_timeout_seconds,
            )
            if workspace_container_owner is None:
                raise RuntimeError(
                    "Codex workspace owner capture failed: " + owner_detail
                )
            _lease_audit(
                lease.audit_file,
                state="container_started",
                lease_id=lease.lease_id,
                container_name=task_id,
                cidfile=lease.cidfile,
                detail=f"cid={cid}",
            )

        def _prepare_workspace(self, task_id: str, workspace_path: str) -> None:
            exec_path = (Path(workspace_path).expanduser().resolve() / "exec")
            quoted = shlex.quote(str(exec_path))
            command = (
                "chmod 1777 /tmp && "
                "rm -rf /tmp_workspace && "
                f"ln -s {quoted} /tmp_workspace && "
                "mkdir -p /root/.codex /root/.codex/sessions"
            )
            try:
                completed = subprocess.run(
                    [
                        str(docker_shim),
                        "exec",
                        task_id,
                        "/bin/bash",
                        "-c",
                        command,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=args.docker_command_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Codex shared workspace setup timed out") from exc
            if completed.returncode != 0:
                raise RuntimeError(
                    "Codex shared workspace setup failed: "
                    + completed.stderr.strip()[-2000:]
                )

    task_id = _safe_task_id(envelope)
    os.environ["PYTHONPATH"] = "/tmp_workspace"
    os.environ["IGRAPH_GRAPH_ML_TOOL"] = _container_graph_ml_tool(skills_dir)
    forwarded_names = ["PYTHONPATH", "IGRAPH_GRAPH_ML_TOOL"]
    forwarded_names.extend(
        name for name in _BROKER_ENVIRONMENT_NAMES if os.environ.get(name)
    )
    task = {
        "env": "\n".join(forwarded_names),
        "skills_path": str(skills_dir.parent),
        "skills": skills_dir.name,
        "warmup": "",
    }
    spec = AgentTaskSpec(
        task_id=task_id,
        task=task,
        workspace_path=str(workspace_root),
        prompt=_prompt(envelope, dataset_target.name),
        timeout_seconds=args.timeout_seconds,
        output_dir=harness_output,
        model=args.model.strip(),
    )
    transcript: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    execution_error: str | None = None
    original_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join(
        (str(docker_shim.parent), original_path or os.defpath)
    )
    try:
        agent = SharedWorkspaceCodexAgent(
            image=args.image,
            openrouter_api_key=provider.api_key,
            openrouter_base_url=provider.base_url,
        )
        execution = agent.run_task(spec)
        execution_error = execution.error
        try:
            usage = agent.collect_usage(task_id, harness_output, execution.elapsed_time)
        except Exception as exc:
            usage = {"collection_error": type(exc).__name__}
        chat_path = harness_output / "chat.jsonl"
        if chat_path.is_file():
            transcript = _jsonl(chat_path)
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path
        container_cleanup_succeeded = True
        cleanup_detail: str | None = None
        try:
            if lease is not None:
                if container_started and workspace_container_owner is not None:
                    ownership_succeeded, ownership_detail = _restore_workspace_ownership(
                        str(docker_shim),
                        task_id,
                        workspace_exec,
                        dataset_target,
                        attempt_root=attempt_root,
                        container_owner=workspace_container_owner,
                        timeout_seconds=args.docker_command_timeout_seconds,
                    )
                    if not ownership_succeeded:
                        cleanup_warnings.append(ownership_detail)
                target = _cid_from_file(lease.cidfile) or task_id
                container_cleanup_succeeded, cleanup_detail = _force_remove_container(
                    docker,
                    target,
                    timeout_seconds=args.docker_command_timeout_seconds,
                )
                if not container_cleanup_succeeded:
                    cleanup_errors.append(cleanup_detail)
        finally:
            environment_cleanup_succeeded = True
            environment_cleanup_detail = ""
            if environment_file is not None:
                (
                    environment_cleanup_succeeded,
                    environment_cleanup_detail,
                ) = _remove_environment_file(
                    environment_file,
                    attempt_root=attempt_root,
                )
            if lease is not None:
                lease.finish(
                    cleanup_succeeded=(
                        container_cleanup_succeeded
                        and environment_cleanup_succeeded
                    ),
                    detail="; ".join(
                        detail
                        for detail in (cleanup_detail, environment_cleanup_detail)
                        if detail
                    )
                    or None,
                )
            for cleanup_path in (workspace_root, harness_output):
                removed, removal_detail = _remove_attempt_tree(
                    cleanup_path,
                    attempt_root=attempt_root,
                )
                if not removed:
                    cleanup_warnings.append(removal_detail)
            if environment_file is not None and (
                environment_file.exists() or environment_file.is_symlink()
            ):
                cleanup_errors.append(
                    environment_cleanup_detail
                    or f"container environment file still exists: {environment_file}"
                )
            if lease is not None:
                _best_effort_lease_audit(
                    lease.audit_file,
                    state=(
                        "cleanup_succeeded"
                        if not cleanup_errors and not cleanup_warnings
                        else "cleanup_failed"
                    ),
                    lease_id=lease.lease_id,
                    container_name=task_id,
                    cidfile=lease.cidfile,
                    detail="; ".join((*cleanup_errors, *cleanup_warnings))
                    or cleanup_detail,
                )

    return _result_from_transcript(
        transcript,
        execution_error=execution_error,
        cleanup_errors=cleanup_errors,
        usage=usage,
        model=args.model.strip(),
        cleanup_warnings=cleanup_warnings,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == _WATCHDOG_MODE:
        return _watchdog_main(arguments[1:])
    if arguments and arguments[0] == _DOCKER_SHIM_MODE:
        return _docker_shim_main(arguments[1:])
    broker_token = os.environ.get("IGRAPH_VIRTUAL_ML_BROKER_TOKEN", "")
    if broker_token:
        _ACTIVE_REDACTION_SECRETS.add(broker_token)
    _install_cancellation_handlers()
    try:
        _emit(_run(_arguments(arguments)))
    except Exception as exc:
        _emit(
            {
                "status": "failed",
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
