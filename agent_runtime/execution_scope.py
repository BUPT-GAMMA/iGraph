"""Run-owned cancellation of asynchronous leaves; observing a run never cancels it.

Agno owns the producer and its terminal save. We interrupt the provider/tool
await and raise its native cancellation exception back into that producer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, TypeVar

from agno.exceptions import RunCancelledException
from agno.run.cancellation_management.in_memory_cancellation_manager import InMemoryRunCancellationManager

T = TypeVar("T")
current_scope: ContextVar["RunScope | None"] = ContextVar("igraph_run_scope", default=None)
operation_id: ContextVar[str | None] = ContextVar("igraph_operation_id", default=None)


@dataclass
class RunScope:
    run_id: str
    user_id: str
    session_id: str
    generation: int
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    executors: set[asyncio.Task] = field(default_factory=set)
    leaves: set[asyncio.Task] = field(default_factory=set)
    preflight_error: str | None = None
    native_run_created: bool = False
    terminal_write_pending: bool = False
    cancel_requested_at: float | None = None
    context_window_usage: dict | None = None

    def check(self) -> None:
        if self.stopped.is_set():
            raise RunCancelledException("用户终止了当前任务")
        if self.preflight_error:
            raise RuntimeError(self.preflight_error)

    def cancel(self) -> None:
        # Broadcast only. Each leaf unwinds and closes its own resources.
        if self.cancel_requested_at is None:
            self.cancel_requested_at = time.monotonic()
            logging.getLogger(__name__).info('run_cancel_dispatched run_id=%s generation=%s', self.run_id, self.generation)
        self.stopped.set()

    @property
    def execution_stopped(self) -> bool:
        return all(task.done() for task in self.executors | self.leaves)

    async def wait(self, operation: Awaitable[T]) -> T:
        task = asyncio.ensure_future(operation)
        self.leaves.add(task)
        signal = asyncio.create_task(self.stopped.wait())
        try:
            if self.stopped.is_set() or self.preflight_error:
                task.cancel()
            else:
                await asyncio.wait((task, signal), return_when=asyncio.FIRST_COMPLETED)
                if signal.done():
                    task.cancel()
            try:
                result = await task
            except (asyncio.CancelledError, Exception):
                # A provider may surface a network error while its response is
                # being closed. Durable user cancellation wins before retries.
                self.check()
                raise
            self.check()
            return result
        finally:
            signal.cancel()
            if not task.done():
                task.cancel()
            # Never report exit while a cancelled leaf is still unwinding.
            import anyio
            cleanup = asyncio.gather(task, signal, return_exceptions=True)
            with anyio.CancelScope(shield=True):
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
            self.leaves.discard(task)


class RunCancellationManager(InMemoryRunCancellationManager):
    """Public Agno extension; durable intent stays in the control store."""
    def __init__(self) -> None:
        super().__init__()
        self.scopes: dict[str, RunScope] = {}

    async def aregister_run(self, run_id: str) -> None:
        await super().aregister_run(run_id)
        scope = current_scope.get()
        if scope and scope.run_id == run_id:
            scope.native_run_created = True
            self.scopes[run_id] = scope
            task = asyncio.current_task()
            if task:
                scope.executors.add(task)

    def is_cancelled(self, run_id: str) -> bool:
        scope = self.scopes.get(run_id)
        return bool(scope and scope.stopped.is_set()) or super().is_cancelled(run_id)

    async def ais_cancelled(self, run_id: str) -> bool:
        return self.is_cancelled(run_id)

    async def acancel_run(self, run_id: str) -> bool:
        scope = self.scopes.get(run_id)
        if scope:
            scope.cancel()
        return await super().acancel_run(run_id)

    def cancel_run(self, run_id: str) -> bool:
        scope = self.scopes.get(run_id)
        if scope:
            # Synchronous native entry points may run in a worker thread.
            for task in scope.executors:
                if not task.done():
                    task.get_loop().call_soon_threadsafe(scope.cancel)
                    break
            else:
                scope.cancel()
        return super().cancel_run(run_id)
