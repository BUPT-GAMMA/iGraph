from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Job:
    job_id: str
    session_id: str
    tool_name: str
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

    async def submit(
        self,
        session_id: str,
        tool_name: str,
        fn: Callable[..., Awaitable[Any] | Any],
        *args,
        **kwargs,
    ) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, session_id=session_id, tool_name=tool_name)
        self._jobs[job_id] = job
        progress_kwarg = kwargs.pop("_progress_kwarg", None)

        if progress_kwarg:
            kwargs[str(progress_kwarg)] = self._make_progress_callback(job)

        async def _runner() -> None:
            job.status = "running"
            job.message = "running"
            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await fn(*args, **kwargs)
                else:
                    result = await asyncio.to_thread(fn, *args, **kwargs)
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
                job.status = "failed"
                job.message = str(exc)

        self._tasks[job_id] = asyncio.create_task(_runner())
        return job_id

    def _make_progress_callback(self, job: Job) -> Callable[[dict[str, Any]], None]:
        def _callback(update: dict[str, Any]) -> None:
            if not isinstance(update, dict):
                return
            progress = update.get("progress")
            if isinstance(progress, (int, float)):
                job.progress = max(0.0, min(1.0, float(progress)))
            message = update.get("message")
            if message is not None:
                job.message = str(message)
            job.detail = dict(update)
            with self._progress_lock:
                self._pending_progress.setdefault(job.job_id, []).append(dict(update))

        return _callback

    def drain_pending_progress(self, job_id: str) -> list[dict[str, Any]]:
        """Return and clear all queued progress updates for a job."""
        with self._progress_lock:
            return self._pending_progress.pop(job_id, [])

    def status(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def cancel_all(self) -> int:
        """取消所有未完成的 job 任务，返回取消数量。"""
        count = 0
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
                count += 1
        return count

    def list_jobs(self, session_id: str) -> list[Job]:
        return [job for job in self._jobs.values() if job.session_id == session_id]
