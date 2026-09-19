from __future__ import annotations

import asyncio
import copy
import inspect
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable
from graph_data_service.execution_host import job_context
from graph_data_service.async_work import run_sync


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Job:
    job_id: str
    session_id: str
    tool_name: str
    user_id: str = ""
    run_id: str = ""
    execution_generation: int = 0
    operation_id: str = ""
    parent_job_id: str | None = None
    status: str = "queued"
    progress: float = 0.0
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self, *, progress_root=None) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._submission_ready: dict[str, asyncio.Event] = {}
        self._pending_progress: dict[str, list[dict[str, Any]]] = {}
        self._deleted_progress_sessions: set[tuple[str, str]] = set()
        self._progress_history: dict[str, list[dict[str, Any]]] = {}
        from graph_data_service.job_progress import JobProgressStore
        self._progress_store = JobProgressStore(progress_root) if progress_root is not None else None
        self._progress_lock = threading.RLock()
        self._cancel_signals: dict[str, asyncio.Event] = {}
        self.execution_host = None
        self.load_saved = None
        self._saved_jobs: dict[str, None] = {}

    def saved(self, job_id, limit=256):
        """Evict only persisted terminal work; durable lookup retains replay."""
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            return
        self._saved_jobs[job_id] = None
        while len(self._saved_jobs) > limit:
            oldest = next(iter(self._saved_jobs))
            self._saved_jobs.pop(oldest, None)
            self._jobs.pop(oldest, None)
            self._tasks.pop(oldest, None)
            self._submission_ready.pop(oldest, None)
            self._cancel_signals.pop(oldest, None)
            with self._progress_lock:
                self._pending_progress.pop(oldest, None)
                if self._progress_store is not None:
                    self._progress_history.pop(oldest, None)

    async def submit(
        self,
        session_id: str,
        tool_name: str,
        fn: Callable[..., Awaitable[Any] | Any],
        *args,
        **kwargs,
    ) -> str:
        identity = kwargs.pop("_identity", {})
        job_id = kwargs.pop("_job_id", None) or uuid.uuid4().hex
        context = kwargs.pop('_context', None) or job_context.get()
        registered = kwargs.pop('_registered', False)
        if context and self.execution_host and not registered:
            return await self.execution_host.launch(context, tool_name, {'args': args, 'kwargs': kwargs},
                                                    fn, args, kwargs, child=True)
        existing = self._jobs.get(job_id)
        if existing is not None:
            if existing.session_id != session_id or existing.tool_name != tool_name or any(getattr(existing, k) != v for k, v in identity.items()):
                raise ValueError("JOB_IDEMPOTENCY_CONFLICT")
            return job_id
        job = Job(job_id=job_id, session_id=session_id, tool_name=tool_name, **identity)
        self._jobs[job_id] = job
        signal = self._cancel_signals[job_id] = asyncio.Event()
        submission_ready = self._submission_ready[job_id] = asyncio.Event()
        progress_kwarg = kwargs.pop("_progress_kwarg", None)
        cancel_kwarg = kwargs.pop("_cancel_kwarg", None)
        if cancel_kwarg:
            kwargs[str(cancel_kwarg)] = signal

        if progress_kwarg:
            kwargs[str(progress_kwarg)] = self._make_progress_callback(job)

        async def _runner() -> None:
            token = job_context.set(context)
            try:
                if signal.is_set():
                    raise asyncio.CancelledError()
                if context and self.execution_host:
                    await self.execution_host.before_start(context)
                if signal.is_set():
                    raise asyncio.CancelledError()
                job.status = "running"
                job.message = "running"
                if inspect.iscoroutinefunction(fn):
                    result = await fn(*args, **kwargs)
                else:
                    # A Python thread cannot be forcibly stopped. Keep execution
                    # and its capacity alive until it really exits. Heavy work
                    # uses the isolated worker executor instead of this fallback.
                    result = await run_sync(fn, *args, **kwargs)
                if (
                    context and self.execution_host
                    and isinstance(result, dict) and result.get('async')
                    and str(result.get('job_id', '')).startswith('lg_')
                ):
                    # Expose the durable handle promptly, but retain ownership
                    # until the independently running worker has really exited.
                    job.result = result
                    submission_ready.set()
                    await self.execution_host.wait_large_graph(context, result['job_id'])
                if signal.is_set():
                    job.status = "cancelled"
                    job.message = "cancelled"
                    return
                job.result = result
                if isinstance(result, dict) and result.get("success") is False:
                    job.status = "failed"
                    job.message = str(result.get("error") or "job failed")
                else:
                    job.progress = 1.0
                    job.status = "done"
                    job.message = "completed"
            except asyncio.CancelledError:
                job.status = "cancelling"
                if context and self.execution_host:
                    await self.execution_host.cancel_large_graph_work(context)
                job.status = "cancelled"
                job.message = "cancelled"
                raise
            except Exception as exc:
                if context and self.execution_host:
                    await self.execution_host.cancel_large_graph_work(context)
                job.status = "cancelled" if signal.is_set() else "failed"
                job.message = "cancelled" if signal.is_set() else str(exc)
            finally:
                submission_ready.set()
                job_context.reset(token)

        self._tasks[job_id] = asyncio.create_task(_runner())
        if context and self.execution_host:
            self.execution_host.watch(context)
        return job_id

    def _make_progress_callback(self, job: Job) -> Callable[[dict[str, Any]], None]:
        def _callback(update: dict[str, Any]) -> None:
            if not isinstance(update, dict):
                return
            row = copy.deepcopy(update)
            with self._progress_lock:
                if job.status != "running" or (job.user_id, job.session_id) in self._deleted_progress_sessions:
                    return
                if self._progress_store is not None:
                    sequence = self._progress_store.append(job.job_id, row, user_id=job.user_id, session_id=job.session_id)
                else:
                    history = self._progress_history.setdefault(job.job_id, [])
                    sequence = len(history) + 1
                    history.append({**row, "progress_seq": sequence})
                row["progress_seq"] = sequence
                progress = row.get("progress")
                if isinstance(progress, (int, float)):
                    job.progress = max(0.0, min(1.0, float(progress)))
                message = row.get("message")
                if message is not None:
                    job.message = str(message)
                job.detail = row
                updates = self._pending_progress.setdefault(job.job_id, [])
                updates.append(copy.deepcopy(row))
                del updates[:-100]  # Compatibility queue; cursor readers use the full journal.
        return _callback

    def read_progress(self, job_id: str, *, after_seq: int = 0, limit: int = 200) -> dict:
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or not 0 <= after_seq <= 2**63 - 1:
            raise ValueError("after_seq must be a nonnegative signed 64-bit integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("progress_limit must be an integer between 1 and 1000")
        with self._progress_lock:
            if self._progress_store is not None:
                return self._progress_store.read(job_id, after_seq, limit)
            rows = self._progress_history.get(job_id, [])[after_seq:after_seq + limit + 1]
            page = copy.deepcopy(rows[:limit])
            return {"progress_updates": page,
                    "progress_cursor": page[-1]["progress_seq"] if page else after_seq,
                    "progress_has_more": len(rows) > limit}

    def delete_progress(self, session_id: str, user_id: str) -> None:
        with self._progress_lock:
            self._deleted_progress_sessions.add((user_id, session_id))
            if self._progress_store is not None:
                self._progress_store.delete_session(session_id, user_id)
            for job in self._jobs.values():
                if (job.user_id, job.session_id) == (user_id, session_id):
                    self._pending_progress.pop(job.job_id, None)
                    self._progress_history.pop(job.job_id, None)

    def progress_status(self, job: Job, *, after_seq=None, limit=200) -> dict:
        # The terminal state and last page must be read together: otherwise an
        # epoch arriving between the two reads could be skipped on completion.
        with self._progress_lock:
            page = (self.read_progress(job.job_id, after_seq=after_seq, limit=limit) if after_seq is not None
                    else {"progress_updates": self.drain_pending_progress(job.job_id)})
            return {"success": True, "job": job.to_dict(), **page}

    def drain_pending_progress(self, job_id: str) -> list[dict[str, Any]]:
        """Return and clear all queued progress updates for a job."""
        with self._progress_lock:
            return self._pending_progress.pop(job_id, [])

    def status(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None and self.load_saved:
            job = self.load_saved(job_id)
            if job:
                self._jobs[job_id] = job
                self.saved(job_id)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        task = self._tasks.get(job_id)
        if job is None or task is None:
            return False
        if task.done() or self._cancel_signals[job_id].is_set():
            return True
        self._cancel_signals[job_id].set()
        previous = job.status
        job.status = "cancelling"
        job.message = "cancelling"
        # A queued runner must execute its small no-work cancellation branch.
        if previous != "queued":
            task.cancel()
        return True

    async def wait(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def wait_submission(self, job_id: str) -> None:
        ready = self._submission_ready.get(job_id)
        if ready is not None:
            await ready.wait()

    def owned(self, job_id: str, session_id: str, user_id: str) -> Job | None:
        job = self.status(job_id)
        return job if job and job.session_id == session_id and job.user_id == user_id else None

    def cancel_run(self, user_id: str, session_id: str, run_id: str) -> list[str]:
        targets = [j.job_id for j in self._jobs.values() if (j.user_id, j.session_id, j.run_id) == (user_id, session_id, run_id)]
        for job_id in targets:
            self.cancel(job_id)
        return targets

    def cancel_all(self) -> int:
        """取消所有未完成的 job 任务，返回取消数量。"""
        count = 0
        for job_id, task in self._tasks.items():
            if not task.done():
                self.cancel(job_id)
                count += 1
        return count

    def list_jobs(self, session_id: str) -> list[Job]:
        return [job for job in self._jobs.values() if job.session_id == session_id]
