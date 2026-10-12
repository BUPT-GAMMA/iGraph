"""Durable FIFO worker that executes one heavy job in a killable process."""

from __future__ import annotations

import multiprocessing
import ctypes
import math
import os
import queue
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .catalog import LargeGraphCatalog
from .contracts import DataQualityPolicy
from .durability import fsync_directory, remove_tree_and_fsync
from .engine_dispatcher import execute_compute_request
from .router import ResourceManager
from .processes import process_context
from .snapshot import PartitionedSnapshotBuilder


_DEFAULT_MAX_JOB_SECONDS = 24 * 60 * 60


def _configured_max_job_seconds() -> float | None:
    """Return the process wall-time limit; non-positive values disable it."""

    raw = os.getenv("LARGE_GRAPH_MAX_JOB_SECONDS", str(_DEFAULT_MAX_JOB_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = float(_DEFAULT_MAX_JOB_SECONDS)
    if not math.isfinite(value):
        value = float(_DEFAULT_MAX_JOB_SECONDS)
    return value if value > 0 else None


def _await_worker_registration(start_gate: Any) -> None:
    """Keep a newly spawned worker inert until its OS identity is durable.

    There is an unavoidable interval between ``Process.start()`` and the
    parent committing the PID/start-time token to SQLite.  If the parent dies
    in that interval, an ungated child could keep writing while a replacement
    runtime sees ``worker_pid IS NULL`` and safely requeues the lease.  The
    child therefore performs no graph I/O until the parent opens this gate.
    A lost parent leaves the gate closed and the child exits on its own.
    """

    try:
        timeout = float(os.getenv("LARGE_GRAPH_START_GATE_SECONDS", "30"))
    except (TypeError, ValueError, OverflowError):
        timeout = 30.0
    timeout = max(1.0, min(timeout, 300.0))
    if not start_gate.wait(timeout=timeout):
        raise RuntimeError("Worker 启动身份未在时限内持久化，已拒绝开始图计算")


def _job_process_entry(
    request: dict[str, Any], roots: dict[str, str], messages: Any, start_gate: Any
) -> None:
    """Top-level spawn target; all heavy I/O and CPU work stays outside the API."""

    def progress(stage: str, value: float, message: str, detail: dict[str, Any]) -> None:
        messages.put(
            {
                "type": "progress",
                "stage": stage,
                "progress": value,
                "message": message,
                "detail": detail,
            }
        )

    try:
        # Every worker owns a POSIX session/process group.  Pools and helper
        # processes created by snapshot/algorithm code inherit this group, so
        # cancellation can terminate the whole execution tree atomically.
        if os.name == "posix":
            os.setsid()
        _await_worker_registration(start_gate)
        if request["kind"] == "prepare":
            policy = DataQualityPolicy.from_dict(request["quality_policy"])
            result = PartitionedSnapshotBuilder(roots["snapshots"]).build(
                source_path=request["source_path"],
                user_scope=request["user_scope"],
                graph_id=request["graph_id"],
                graph_version=int(request["graph_version"]),
                directed=bool(request["directed"]),
                partitions=int(request["partitions"]),
                workers=int(request["workers"]),
                delimiter=str(request["delimiter"]),
                input_format=str(request["input_format"]),
                policy=policy,
                expected_source=dict(request["source"]),
                progress=progress,
            )
        elif request["kind"] == "compute":
            result = execute_compute_request(
                request,
                snapshot_root=roots["snapshots"],
                artifact_root=roots["artifacts"],
                progress=progress,
            )
        else:
            raise ValueError(f"未知大图任务类型: {request.get('kind')!r}")
        messages.put({"type": "result", "result": result})
    except BaseException as exc:
        # Traceback is useful operationally but capped so job status stays a
        # bounded control-plane record.
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:]
        messages.put({"type": "error", "error": str(exc), "traceback": detail})


class DurableLargeGraphRuntime:
    """Persistent job scheduler with process-level cancellation.

    A single worker thread only orchestrates.  The actual preparation and
    algorithm stages run in a child process, so cancellation terminates the
    underlying execution rather than merely cancelling an asyncio wrapper.
    """

    def __init__(
        self,
        root: str | Path,
        catalog: LargeGraphCatalog,
        resource_manager: ResourceManager | None = None,
    ) -> None:
        self.root = Path(root)
        self.catalog = catalog
        self.resources = resource_manager or ResourceManager(self.root)
        self.snapshot_root = self.root / "snapshots"
        self.artifact_root = self.root / "artifacts"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._current_process: multiprocessing.Process | None = None
        self._current_job_id: str | None = None
        self._current_lease_id: str | None = None
        self._current_job: dict[str, Any] | None = None
        # The deadline starts at Process.start(), not when a durable queued job
        # is claimed.  Operators can explicitly disable the watchdog with a
        # non-positive LARGE_GRAPH_MAX_JOB_SECONDS value.
        self.max_job_seconds = _configured_max_job_seconds()

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._stop.clear()
            self._recover_interrupted_jobs()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="igraph-large-graph-worker",
                daemon=True,
            )
            self._thread.start()

    def notify(self) -> None:
        self.start()
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        # The worker normally observes _stop within 200ms and performs this
        # handoff itself.  This fallback covers a stuck queue/OS primitive.
        if thread is not None and thread.is_alive():
            with self._state_lock:
                process = self._current_process
                job_id = self._current_job_id
                lease_id = self._current_lease_id
                job = self._current_job
            if process is not None and process.pid is not None:
                self._terminate_process_tree(process)
            remote_quiesced = job is None or self._cancel_remote_attempt(job)
            if job_id and remote_quiesced:
                handed_off = self.catalog.requeue_running_job(
                    job_id, "服务停止，任务已持久化并等待重试",
                    lease_id=lease_id,
                )
                # Cleanup is deliberately gated by the lease-fenced catalog
                # transition. If the worker managed to publish while close()
                # was taking the lock, requeue returns False and the now
                # catalog-visible output must be preserved.
                if handed_off and job is not None:
                    self._cleanup_recorded_attempt(job)

    def cancel(self, job_id: str, *, user_id: str, session_id: str) -> bool:
        changed = self.catalog.request_cancel(
            job_id, user_id=user_id, session_id=session_id
        )
        if changed:
            self._wake.set()
        return changed

    def _worker_loop(self) -> None:
        next_recovery = 0.0
        while not self._stop.is_set():
            job = self.catalog.claim_next_job()
            if job is None:
                if time.monotonic() >= next_recovery:
                    self._recover_interrupted_jobs()
                    next_recovery = time.monotonic() + 5.0
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                self._execute(job)
            except BaseException as exc:
                # A catalog/filesystem/process-launch failure must not kill the
                # durable scheduler thread or leave the job forever "running".
                self._cleanup_staging(job)
                self.catalog.finish_failed(
                    job["job_id"], str(exc), lease_id=job.get("lease_id")
                )
                self._drain_attempt_cleanups()
                with self._state_lock:
                    self._current_process = None
                    self._current_job_id = None
                    self._current_lease_id = None
                    self._current_job = None
            self._drain_attempt_cleanups()

    def _execute(self, job: dict[str, Any]) -> None:
        # A recovered attempt may have left private sort/map directories. They
        # are never catalog-visible and are safe to remove before retrying.
        self._cleanup_staging(job)
        request = dict(job["request"])
        # Each claimed attempt writes into its own physical scope. A stale
        # process can therefore neither delete nor rename the retry's staging
        # files even if both briefly overlap around lease recovery.
        base_scope = str(request.get("user_scope") or "_")
        lease_component = str(job.get("lease_id") or "unleased").replace("/", "_")
        request["user_scope"] = f"{base_scope}__{lease_component}"
        request.update(
            {
                "kind": job["kind"],
                "user_id": job["user_id"],
                "session_id": job["session_id"],
                "graph_id": job["graph_id"],
                "graph_version": job["graph_version"],
                "job_id": job["job_id"],
                "lease_id": job.get("lease_id"),
            }
        )
        if self.catalog.cancellation_requested(job["job_id"]):
            self.catalog.finish_cancelled(
                job["job_id"], lease_id=job.get("lease_id")
            )
            self._cleanup_recorded_attempt(job)
            return
        if self._stop.is_set():
            handed_off = self.catalog.requeue_running_job(
                job["job_id"], "服务停止，任务已持久化并等待重试",
                lease_id=job.get("lease_id"),
            )
            if handed_off:
                self._cleanup_recorded_attempt(job)
            return

        admitted, reason, observed = self._admit_claimed_job(job, request)
        # The resource probe can take long enough for a concurrent cancellation
        # or shutdown request.  Check both again immediately before launch.
        if self.catalog.cancellation_requested(job["job_id"]):
            self.catalog.finish_cancelled(
                job["job_id"], lease_id=job.get("lease_id")
            )
            self._cleanup_recorded_attempt(job)
            return
        if self._stop.is_set():
            handed_off = self.catalog.requeue_running_job(
                job["job_id"], "服务停止，任务已持久化并等待重试",
                lease_id=job.get("lease_id"),
            )
            if handed_off:
                self._cleanup_recorded_attempt(job)
            return
        if not admitted:
            self.catalog.finish_resource_rejected(
                job["job_id"], reason, observed, lease_id=job.get("lease_id")
            )
            return

        context = process_context()
        messages = context.Queue()
        start_gate = context.Event()
        process = context.Process(
            target=_job_process_entry,
            args=(
                request,
                {"snapshots": str(self.snapshot_root), "artifacts": str(self.artifact_root)},
                messages,
                start_gate,
            ),
            name=f"igraph-large-job-{job['job_id']}",
        )
        with self._state_lock:
            self._current_process = process
            self._current_job_id = job["job_id"]
            self._current_lease_id = job.get("lease_id")
            self._current_job = dict(job)
        result: dict[str, Any] | None = None
        error: str | None = None
        process_started_at: float | None = None
        try:
            # There is intentionally no I/O or cleanup between this final
            # cancellation check and process.start().  A later request is
            # handled by the process-tree termination path below.
            if self.catalog.cancellation_requested(job["job_id"]):
                self.catalog.finish_cancelled(
                    job["job_id"], lease_id=job.get("lease_id")
                )
                self._cleanup_recorded_attempt(job)
                return
            process.start()
            process_started_at = time.monotonic()
            worker_identity = self._capture_worker_identity(process.pid)
            if sys.platform.startswith("linux") and worker_identity is None:
                raise RuntimeError("无法验证大图 Worker 的 Linux 启动身份，已拒绝执行")
            registered = self.catalog.set_worker_pid(
                job["job_id"], process.pid,
                lease_id=job.get("lease_id"),
                identity=worker_identity,
            )
            if not registered:
                raise RuntimeError("大图 Worker 租约已失效，未授权子进程开始执行")
            if self.catalog.cancellation_requested(job["job_id"]):
                self._terminate_process_tree(process)
                self.catalog.finish_cancelled(
                    job["job_id"], lease_id=job.get("lease_id")
                )
                self._cleanup_recorded_attempt(job)
                return
            registration_completed_at = time.monotonic()
            if (
                self.max_job_seconds is not None
                and process_started_at is not None
                and registration_completed_at - process_started_at
                >= self.max_job_seconds
            ):
                self._finish_timed_out_process(
                    job,
                    process,
                    elapsed_seconds=registration_completed_at - process_started_at,
                )
                return
            # This is intentionally the last operation after durable PID and
            # birth-token registration.  Before it, the child cannot create
            # staging files or nested worker pools.
            start_gate.set()
            next_heartbeat = registration_completed_at + 5.0
            while process.is_alive():
                if self._stop.is_set():
                    self._terminate_process_tree(process)
                    if not self._cancel_remote_attempt(job):
                        # Keep the current lease and attempt files bound until
                        # recovery can prove the remote execution is terminal.
                        return
                    handed_off = self.catalog.requeue_running_job(
                        job["job_id"], "服务停止，任务已持久化并等待重试",
                        lease_id=job.get("lease_id"),
                    )
                    if handed_off:
                        self._cleanup_recorded_attempt(job)
                    return
                if self.catalog.cancellation_requested(job["job_id"]):
                    self._terminate_process_tree(process)
                    if result is not None:
                        self._discard_unpublished_result(result)
                    elif not self._cancel_remote_attempt(job):
                        # The adapter lease will reap an orphaned run. Do not
                        # delete its shared output or release this attempt yet.
                        return
                    self.catalog.finish_cancelled(
                        job["job_id"], lease_id=job.get("lease_id")
                    )
                    self._cleanup_recorded_attempt(job)
                    return
                now = time.monotonic()
                if (
                    self.max_job_seconds is not None
                    and process_started_at is not None
                    and now - process_started_at >= self.max_job_seconds
                ):
                    # Termination can take up to the TERM/KILL grace period.
                    # finish_timeout re-checks cancellation under the catalog
                    # write lock, so a cancellation persisted during that
                    # interval wins over the timeout terminal state.
                    self._finish_timed_out_process(
                        job,
                        process,
                        elapsed_seconds=max(0.0, now - process_started_at),
                    )
                    return
                if now >= next_heartbeat:
                    self.catalog.heartbeat(
                        job["job_id"], lease_id=job.get("lease_id")
                    )
                    next_heartbeat = now + 5.0
                try:
                    poll_seconds = 0.2
                    if (
                        self.max_job_seconds is not None
                        and process_started_at is not None
                    ):
                        poll_seconds = min(
                            poll_seconds,
                            max(
                                0.001,
                                self.max_job_seconds
                                - (now - process_started_at),
                            ),
                        )
                    item = messages.get(timeout=poll_seconds)
                except queue.Empty:
                    continue
                result, error = self._consume_message(
                    job["job_id"], item, result, error,
                    lease_id=job.get("lease_id"),
                )
            process_finished_at = time.monotonic()
            process.join(timeout=5)
            # Drain messages written just before process exit.
            while True:
                try:
                    item = messages.get_nowait()
                except queue.Empty:
                    break
                result, error = self._consume_message(
                    job["job_id"], item, result, error,
                    lease_id=job.get("lease_id"),
                )
            # A crashed/direct child can exit before one of its pool workers.
            # Quiesce the whole group before any staging cleanup or publication.
            self._terminate_process_tree(process)
            finished_elapsed = (
                max(0.0, process_finished_at - process_started_at)
                if process_started_at is not None
                else 0.0
            )

            if self.catalog.cancellation_requested(job["job_id"]):
                if result is not None:
                    self._discard_unpublished_result(result)
                elif not self._cancel_remote_attempt(job):
                    return
                self.catalog.finish_cancelled(
                    job["job_id"], lease_id=job.get("lease_id")
                )
                self._cleanup_recorded_attempt(job)
            elif (
                self.max_job_seconds is not None
                and process_started_at is not None
                and finished_elapsed >= self.max_job_seconds
            ):
                self._finish_timed_out_process(
                    job,
                    process,
                    elapsed_seconds=finished_elapsed,
                )
            elif error:
                if not self._cancel_remote_attempt(job):
                    return
                self._cleanup_staging(job)
                self.catalog.finish_failed(
                    job["job_id"], error, lease_id=job.get("lease_id")
                )
            elif process.exitcode != 0 or result is None:
                if not self._cancel_remote_attempt(job):
                    return
                self._cleanup_staging(job)
                self.catalog.finish_failed(
                    job["job_id"], f"底层执行进程异常退出(exitcode={process.exitcode})",
                    lease_id=job.get("lease_id"),
                )
            else:
                try:
                    published = (
                        self.catalog.publish_snapshot(
                            job["job_id"], result, lease_id=job.get("lease_id")
                        )
                        if job["kind"] == "prepare"
                        else self.catalog.publish_artifact(
                            job["job_id"], result, lease_id=job.get("lease_id")
                        )
                    )
                except BaseException:
                    # Physical rename happens before catalog publication. If
                    # the catalog transaction fails, remove that orphan so a
                    # retry cannot mistake it for a visible version/result.
                    self._discard_unpublished_result(result)
                    raise
                if not published:
                    self._discard_unpublished_result(result)
                    self._cleanup_recorded_attempt(job)
        finally:
            # A launch/catalog/queue failure must never leave descendants
            # writing into staging while the scheduler cleans or retries it.
            if result is None:
                self._cancel_remote_attempt(job)
            if process.pid is not None:
                self._terminate_process_tree(process)
            messages.close()
            with self._state_lock:
                self._current_process = None
                self._current_job_id = None
                self._current_lease_id = None
                self._current_job = None

    def _finish_timed_out_process(
        self,
        job: dict[str, Any],
        process: multiprocessing.Process,
        *,
        elapsed_seconds: float,
    ) -> str | None:
        """Quiesce one attempt before its lease-fenced timeout transition."""

        self._terminate_process_tree(process)
        if not self._cancel_remote_attempt(job):
            # Leave the durable attempt running. The recovery loop retries the
            # cancellation after the adapter's own control lease has expired.
            return None
        terminal_status = self.catalog.finish_timeout(
            job["job_id"],
            max_job_seconds=float(self.max_job_seconds or 0.0),
            elapsed_seconds=max(0.0, float(elapsed_seconds)),
            lease_id=job.get("lease_id"),
        )
        if terminal_status is not None:
            self._cleanup_recorded_attempt(job)
        return terminal_status

    @staticmethod
    def _cancel_remote_attempt(job: dict[str, Any]) -> bool:
        """Return whether a remote attempt is absent or durably terminal."""

        request = job.get("request") or {}
        raw_plan = request.get("backend_plan")
        if (
            not isinstance(raw_plan, dict)
            or raw_plan.get("selected_backend") != "graphscope"
        ):
            return True
        adapter_url = str(raw_plan.get("adapter_url") or "").strip()
        if not adapter_url:
            return False
        try:
            from .graphscope_backend import GraphScopeAdapterClient, graphscope_run_id

            return GraphScopeAdapterClient(adapter_url).cancel(
                graphscope_run_id(str(job["job_id"]), job.get("lease_id"))
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            # Remote leases expire automatically if the worker disappears.
            return False

    def _admit_claimed_job(
        self, job: dict[str, Any], request: dict[str, Any]
    ) -> tuple[bool, str, dict[str, int]]:
        """Re-evaluate resources after dequeue using the current machine state."""
        try:
            if job["kind"] == "prepare":
                source_path = Path(str(request["source_path"]))
                source_bytes = source_path.stat().st_size
                estimate = self.resources.estimate_preparation(
                    source_bytes,
                    int(request["partitions"]),
                    int(request["workers"]),
                )
            elif job["kind"] == "compute":
                snapshot = dict(request["snapshot"])
                estimate = self.resources.estimate_algorithm(
                    int(snapshot.get("node_count", 0)),
                    int(snapshot.get("edge_count", 0)),
                    max(1, int(snapshot.get("partitions", 1))),
                    str(request.get("algorithm") or ""),
                    dict(request.get("parameters") or {}),
                    int(request.get("workers") or 1),
                    snapshot_storage_bytes=snapshot.get("storage_bytes"),
                )
            else:
                return False, f"未知大图任务类型: {job.get('kind')!r}", {}
            return self.resources.admit(estimate)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return False, f"执行前资源二次准入失败: {exc}", {}

    @staticmethod
    def _posix_process_group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_process_tree(
        self, process: multiprocessing.Process, *, grace_seconds: float = 2.0
    ) -> None:
        """Terminate a job and all descendants, escalating TERM to KILL."""
        pid = process.pid
        if pid is None:
            return

        process_group: int | None = None
        if os.name == "posix":
            # The child calls setsid() before doing any work.  Allow a short
            # scheduling window for that first instruction; never signal a
            # group unless the child is confirmed as its leader.
            group_deadline = time.monotonic() + 0.5
            while process.is_alive() and time.monotonic() < group_deadline:
                try:
                    candidate = os.getpgid(pid)
                except ProcessLookupError:
                    break
                if candidate == pid:
                    process_group = candidate
                    break
                time.sleep(0.01)
            # The group can outlive its leader.  This check is safe here
            # because the job process contract reserves its own PID as PGID
            # before it is allowed to create descendants.
            if (
                process_group is None
                and self._posix_process_group_alive(pid)
            ):
                process_group = pid

        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.is_alive():
            process.terminate()

        deadline = time.monotonic() + max(0.0, float(grace_seconds))
        while time.monotonic() < deadline:
            process.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            if process_group is not None:
                if not self._posix_process_group_alive(process_group):
                    break
            elif not process.is_alive():
                break

        if process_group is not None and self._posix_process_group_alive(process_group):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.is_alive():
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
            else:  # pragma: no cover - compatibility with older Python/Windows
                process.terminate()
        process.join(timeout=1.0)

    def _consume_message(
        self,
        job_id: str,
        item: dict[str, Any],
        result: dict[str, Any] | None,
        error: str | None,
        *,
        lease_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        kind = item.get("type")
        if kind == "progress":
            self.catalog.update_progress(
                job_id,
                stage=str(item.get("stage") or "running"),
                progress=float(item.get("progress") or 0.0),
                message=str(item.get("message") or "running"),
                detail=dict(item.get("detail") or {}),
                lease_id=lease_id,
            )
        elif kind == "result":
            result = dict(item.get("result") or {})
        elif kind == "error":
            error = str(item.get("error") or item.get("traceback") or "unknown error")
        return result, error

    def _attempt_version_roots(self, job: dict[str, Any]) -> tuple[Path, ...]:
        request = job.get("request") or {}
        base_scope = str(request.get("user_scope") or "_")
        lease_component = str(job.get("lease_id") or "unleased").replace("/", "_")
        user_scope = f"{base_scope}__{lease_component}"
        source_version_name = f"v{int(job['graph_version']):08d}"
        candidates = [
            (
                self.snapshot_root / user_scope / job["graph_id"] / source_version_name,
                self.snapshot_root.resolve(),
            ),
            (
                self.artifact_root / user_scope / job["graph_id"] / source_version_name,
                self.artifact_root.resolve(),
            ),
        ]
        derived_target = request.get("derived_target")
        if isinstance(derived_target, dict):
            try:
                target_version_name = (
                    f"v{int(derived_target['graph_version']):08d}"
                )
            except (KeyError, TypeError, ValueError):
                target_version_name = ""
            if target_version_name:
                candidates.append(
                    (
                        self.snapshot_root
                        / user_scope
                        / job["graph_id"]
                        / target_version_name,
                        self.snapshot_root.resolve(),
                    )
                )
        safe: list[Path] = []
        for candidate, root in candidates:
            base = candidate.resolve()
            if base.is_relative_to(root):
                safe.append(base)
        return tuple(safe)

    def _cleanup_staging(self, job: dict[str, Any]) -> None:
        for base in self._attempt_version_roots(job):
            if not base.is_dir():
                continue
            for child in base.iterdir():
                # Exact naming guards prevent cleanup from touching published
                # snapshots/artifacts or anything outside this graph version.
                if (
                    (child.is_symlink() or child.is_dir())
                    and child.name.startswith(".")
                    and child.name.endswith(".staging")
                ):
                    try:
                        remove_tree_and_fsync(child)
                    except (OSError, ValueError):
                        pass

    def _cleanup_attempt_outputs(self, job: dict[str, Any]) -> bool:
        """Remove every non-catalogued file owned by one attempt lease.

        A child atomically renames its staging directory before sending its
        result message.  Therefore cleanup cannot be limited to hidden staging
        names: cancellation or shutdown may win after that rename but before
        catalog publication.  The lease-specific scope makes these generated
        directories safe to remove without touching a retry or another worker.
        """
        clean = True
        for base in self._attempt_version_roots(job):
            if not base.is_dir():
                continue
            try:
                children = list(base.iterdir())
            except FileNotFoundError:
                continue
            except OSError:
                clean = False
                continue
            for child in children:
                if not (child.is_symlink() or child.is_dir()):
                    continue
                is_staging = child.name.startswith(".") and child.name.endswith(
                    ".staging"
                )
                is_generated = child.name.startswith(("snap_", "artifact_"))
                if is_staging or is_generated:
                    try:
                        remove_tree_and_fsync(child)
                    except (OSError, ValueError):
                        clean = False
            # Retrying a cleanup after rmtree succeeded but its directory fsync
            # failed must not acknowledge the durable queue merely because the
            # child is now absent from the page cache view.
            try:
                fsync_directory(base)
            except (OSError, ValueError):
                clean = False
        return clean

    def _cleanup_recorded_attempt(self, job: dict[str, Any]) -> None:
        if not self._cleanup_attempt_outputs(job):
            return
        lease_id = job.get("lease_id")
        if isinstance(lease_id, str) and lease_id:
            self.catalog.acknowledge_attempt_cleanup(job["job_id"], lease_id)

    def _drain_attempt_cleanups(self) -> None:
        for attempt in self.catalog.pending_attempt_cleanups():
            self._cleanup_recorded_attempt(attempt)

    def _recover_interrupted_jobs(self, stale_after_seconds: float = 30.0) -> int:
        recoverable_leases: set[tuple[str, str]] = set()
        for job in self.catalog.stale_interrupted_jobs(stale_after_seconds):
            lease_id = job.get("lease_id")
            safe, reason = self._quiesce_stale_worker(job)
            if safe and isinstance(lease_id, str) and lease_id:
                if self._cancel_remote_attempt(job):
                    recoverable_leases.add((job["job_id"], lease_id))
                else:
                    self.catalog.mark_recovery_blocked(
                        job["job_id"],
                        lease_id,
                        "恢复受阻：无法确认 GraphScope 远端运行已终止",
                    )
            elif isinstance(lease_id, str) and lease_id:
                self.catalog.mark_recovery_blocked(
                    job["job_id"], lease_id, reason
                )
        recovered = self.catalog.recover_interrupted_jobs(
            stale_after_seconds,
            recoverable_leases=recoverable_leases,
        )
        self._drain_attempt_cleanups()
        return recovered

    @staticmethod
    def _read_linux_process_identity(pid: int) -> dict[str, Any]:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = stat_text.rfind(")")
        if closing < 0:
            raise ValueError("/proc stat 格式无效")
        fields = stat_text[closing + 2 :].split()
        if len(fields) <= 19:
            raise ValueError("/proc stat 字段不足")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
        return {
            "scheme": "linux-proc-v1",
            "pid": int(pid),
            "state": fields[0],
            "pgid": int(fields[2]),
            "session_id": int(fields[3]),
            "start_time_ticks": int(fields[19]),
            "boot_id": boot_id,
        }

    def _observe_worker_process(
        self, pid: int
    ) -> tuple[str, dict[str, Any] | None]:
        if sys.platform.startswith("linux"):
            try:
                identity = self._read_linux_process_identity(pid)
            except FileNotFoundError:
                return "absent", None
            except (OSError, UnicodeError, ValueError):
                return "unverifiable", None
            if identity.get("state") == "Z":
                return "absent", identity
            return "alive", identity
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "absent", None
        except (OSError, PermissionError):
            return "unverifiable", None
        return "unverifiable", None

    def _capture_worker_identity(self, pid: int | None) -> dict[str, Any] | None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            return None
        # ``Process.start()`` only proves that the child exists. On a busy
        # host it may need noticeably longer than 500 ms to run ``setsid()``.
        # The start gate still prevents graph I/O during this wait, so allow a
        # bounded, configurable registration window instead of rejecting a
        # healthy worker because it was scheduled late.
        try:
            timeout = float(
                os.getenv("LARGE_GRAPH_WORKER_IDENTITY_TIMEOUT_SECONDS", "5")
            )
        except (TypeError, ValueError, OverflowError):
            timeout = 5.0
        if not math.isfinite(timeout):
            timeout = 5.0
        timeout = max(0.1, min(timeout, 30.0))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, identity = self._observe_worker_process(pid)
            if state == "absent":
                return None
            if state == "alive" and identity is not None:
                if identity.get("pgid") == pid and identity.get("session_id") == pid:
                    return identity
            if state == "unverifiable":
                return None
            time.sleep(0.01)
        return None

    @staticmethod
    def _same_process_identity(
        expected: dict[str, Any], observed: dict[str, Any]
    ) -> bool:
        return all(
            expected.get(field) == observed.get(field)
            for field in ("scheme", "pid", "start_time_ticks", "boot_id")
        )

    def _quiesce_stale_worker(
        self, job: dict[str, Any]
    ) -> tuple[bool, str]:
        raw_pid = job.get("worker_pid")
        if isinstance(raw_pid, bool):
            return False, "恢复受阻：Worker PID 无效，未删除旧租约文件"
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return False, "恢复受阻：Worker PID 无效，未删除旧租约文件"
        if pid <= 1:
            return False, "恢复受阻：Worker PID 无效，未删除旧租约文件"

        expected = job.get("worker_identity")
        if isinstance(expected, dict):
            try:
                identity_valid = (
                    expected.get("scheme") == "linux-proc-v1"
                    and int(expected.get("pid")) == pid
                    and int(expected.get("pgid")) == pid
                    and int(expected.get("session_id")) == pid
                    and int(expected.get("start_time_ticks")) >= 0
                    and bool(str(expected.get("boot_id") or ""))
                )
            except (TypeError, ValueError, OverflowError):
                identity_valid = False
            if not identity_valid:
                return False, "恢复受阻：持久化的 Worker 启动身份无效"
        state, observed = self._observe_worker_process(pid)
        if state == "absent" and not isinstance(expected, dict):
            # A dead session leader can leave live pool descendants behind.
            # Without a persisted birth token we cannot safely signal a
            # numeric process group, but we can prove it no longer exists.
            if os.name == "posix" and self._posix_process_group_alive(pid):
                return False, "恢复受阻：原 Worker 已退出但进程组仍存活且缺少启动身份"
            return True, "原 Worker 及其进程组已退出"
        if state != "alive" or not isinstance(observed, dict):
            if not isinstance(expected, dict):
                return False, "恢复受阻：无法验证 Worker 的启动身份"
            members = self._verified_group_members(expected)
            if members is None:
                return False, "恢复受阻：无法验证原 Worker 进程组成员"
            if not members:
                return True, "原 Worker 及其进程组已退出"
            if not self._terminate_verified_process_group(expected):
                return False, "恢复受阻：已验证 Worker 后代进程未能完全终止"
            return True, "已终止原 Worker 遗留的后代进程"
        if not isinstance(expected, dict):
            return False, "恢复受阻：旧任务缺少可验证的 Worker 启动身份"
        if not self._same_process_identity(expected, observed):
            # Never signal a reused numeric PID.  If it also became leader of
            # a group/session with the old numeric identity, there is no safe
            # way to distinguish that new group from old descendants.
            if (
                observed.get("pgid") == expected.get("pgid")
                or observed.get("session_id") == expected.get("session_id")
            ):
                return False, "恢复受阻：Worker PID 已复用且进程组身份存在歧义"
            members = self._verified_group_members(expected)
            if members is None:
                return False, "恢复受阻：Worker PID 已复用且无法核验旧进程组"
            if members and not self._terminate_verified_process_group(expected):
                return False, "恢复受阻：PID 复用后旧 Worker 后代未能完全终止"
            return True, "原 Worker 已退出；检测到 PID 已复用"
        if (
            expected.get("pgid") != pid
            or expected.get("session_id") != pid
            or observed.get("pgid") != pid
            or observed.get("session_id") != pid
        ):
            return False, "恢复受阻：Worker 不再属于已验证的独立进程组"
        if not self._terminate_verified_process_group(expected):
            return False, "恢复受阻：已验证 Worker 进程组未能完全终止"
        return True, "已终止原 Worker 进程组"

    def _verified_group_members(
        self, expected: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        group_id = int(expected["pgid"])
        session_id = int(expected["session_id"])
        boot_id = expected.get("boot_id")
        members: list[dict[str, Any]] = []
        try:
            proc_entries = list(Path("/proc").iterdir())
        except OSError:
            return None
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                identity = self._read_linux_process_identity(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                # An entry may disappear before open (ENOENT) or after open
                # but before read (ESRCH). Neither denotes an unreadable live
                # process; other inspection failures still fail closed below.
                continue
            except (OSError, UnicodeError, ValueError):
                # Skipping an unreadable live process could let cleanup race
                # one of the old job's descendants. Recovery therefore fails
                # closed when the process table cannot be fully inspected.
                return None
            if (
                identity.get("boot_id") == boot_id
                and identity.get("pgid") == group_id
                and identity.get("session_id") == session_id
                and identity.get("state") != "Z"
            ):
                members.append(identity)
        return members

    def _pidfd_signal_verified(
        self,
        identity: dict[str, Any],
        expected: dict[str, Any],
        signum: int,
    ) -> bool:
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_send_signal is None:
            return False
        pid = int(identity["pid"])
        try:
            pidfd = self._open_pidfd(pid)
        except (OSError, ValueError):
            return False
        try:
            try:
                observed = self._read_linux_process_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                return True
            except (OSError, UnicodeError, ValueError):
                return False
            if (
                not self._same_process_identity(identity, observed)
                or observed.get("pgid") != expected.get("pgid")
                or observed.get("session_id") != expected.get("session_id")
            ):
                return False
            try:
                pidfd_send_signal(pidfd, signum, None, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            return True
        finally:
            os.close(pidfd)

    @staticmethod
    def _open_pidfd(pid: int) -> int:
        native = getattr(os, "pidfd_open", None)
        if native is not None:
            return int(native(pid, 0))
        if not sys.platform.startswith("linux"):
            raise OSError("pidfd is unavailable")
        libc = ctypes.CDLL(None, use_errno=True)
        pidfd_open = getattr(libc, "pidfd_open", None)
        if pidfd_open is None:
            raise OSError("pidfd_open is unavailable")
        pidfd_open.argtypes = (ctypes.c_int, ctypes.c_uint)
        pidfd_open.restype = ctypes.c_int
        descriptor = int(pidfd_open(int(pid), 0))
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return descriptor

    def _terminate_verified_process_group(
        self,
        expected: dict[str, Any],
        *,
        grace_seconds: float = 2.0,
    ) -> bool:
        """TERM then KILL only members revalidated through Linux pidfds."""
        if not sys.platform.startswith("linux"):
            return False
        if getattr(signal, "pidfd_send_signal", None) is None:
            return False
        phases = (
            (signal.SIGTERM, max(0.0, float(grace_seconds))),
            (signal.SIGKILL, 1.0),
        )
        for signum, duration in phases:
            deadline = time.monotonic() + duration
            while True:
                members = self._verified_group_members(expected)
                if members is None:
                    return False
                if not members:
                    return True
                if not all(
                    self._pidfd_signal_verified(member, expected, signum)
                    for member in members
                ):
                    return False
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        remaining = self._verified_group_members(expected)
        return remaining == []

    def _discard_unpublished_result(self, result: dict[str, Any]) -> None:
        derived = result.get("derived_snapshot")
        if isinstance(derived, dict):
            self._discard_unpublished_result(derived)
        manifest_raw = result.get("manifest_path") or result.get("path")
        if not manifest_raw:
            return
        manifest = Path(str(manifest_raw)).resolve()
        allowed_roots = (self.snapshot_root.resolve(), self.artifact_root.resolve())
        # Cancellation cleanup is restricted to the exact generated snapshot
        # or artifact directory and can never remove a graph/version root.
        if any(manifest.is_relative_to(root) for root in allowed_roots):
            generated_dir = manifest.parent
            if generated_dir.name.startswith(("snap_", "artifact_")):
                try:
                    remove_tree_and_fsync(generated_dir)
                except (OSError, ValueError):
                    pass
