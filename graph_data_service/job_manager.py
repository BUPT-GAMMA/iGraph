from __future__ import annotations

import asyncio
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
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending_progress: dict[str, list[dict[str, Any]]] = {}
        self._progress_lock = threading.Lock()
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
            self._cancel_signals.pop(oldest, None)
            with self._progress_lock:
                self._pending_progress.pop(oldest, None)

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
                job.status = "cancelled"
                job.message = "cancelled"
                raise
            except Exception as exc:
                job.status = "cancelled" if signal.is_set() else "failed"
                job.message = "cancelled" if signal.is_set() else str(exc)
            finally:
                job_context.reset(token)

        self._tasks[job_id] = asyncio.create_task(_runner())
        if context and self.execution_host:
            self.execution_host.watch(context)
        return job_id

    def _make_progress_callback(self, job: Job) -> Callable[[dict[str, Any]], None]:
        def _callback(update: dict[str, Any]) -> None:
            if not isinstance(update, dict) or job.status != "running":
                return
            progress = update.get("progress")
            if isinstance(progress, (int, float)):
                job.progress = max(0.0, min(1.0, float(progress)))
            message = update.get("message")
            if message is not None:
                job.message = str(message)
            job.detail = dict(update)
            with self._progress_lock:
                updates = self._pending_progress.setdefault(job.job_id, [])
                updates.append(dict(update))
                del updates[:-100]

        return _callback

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
