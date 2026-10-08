"""Bounded, service-owned construction; no long work in the default executor."""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field

import anyio

from execution_lifecycle import ProcessLease, atomic_json
from graph_data_service.async_work import run_sync
from graph_data_service.construction import ConstructionError, _json, _json_keys
from graph_data_service.worker_process import WorkerProcess, recover_worker

logger = logging.getLogger(__name__)


class BuildFailed(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class BuildAttempt:
    coordinator: object
    user_id: str
    dataset_id: str
    fingerprint: str
    state: dict
    build_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task: asyncio.Task | None = None

    @property
    def instance_id(self):
        return self.coordinator.lease.identity

    def snapshot(self):
        state = copy.deepcopy(self.state)
        try:
            directory = self.coordinator.store._directory(self.user_id, self.dataset_id)
            record = self.coordinator.store._read(directory, self.dataset_id)
            if record.get("build_id") == self.build_id:
                state.update(self.coordinator.store._status(record, directory))
        except (OSError, ValueError, TypeError):
            # Status must stay available when an observational sidecar is absent
            # or temporarily unreadable; the durable construction record wins.
            pass
        progress = state.get("progress", {})
        message = progress.get("current_message") if isinstance(progress, dict) else None
        return {**state, "status": "building", "build_id": self.build_id,
                "failure_code": None, "message": message or "构图正在服务器上运行，请稍后查询状态。"}

    def invoke(self, algorithm, source, options, lease_fds, *, isolated):
        owner = self.coordinator
        if owner.closed:
            raise BuildFailed("construction_interrupted")
        if not isolated:
            # Preserve the public Path/options callable extension point.
            return algorithm(source, options)
        from graph_engine.construction.agent import EvolutionConfig
        duration = EvolutionConfig.from_options(options).max_seconds
        staging = source.parent / ".builds" / self.build_id
        staging.mkdir(parents=True, exist_ok=False)
        report_path = source.parent / "evolution-report.json"
        try:
            atomic_json(staging / "request.json", {"source": str(source), "options": options,
                        "data_root": str(owner.store.root), "build_id": self.build_id})
            atomic_json(report_path, {"status": "running", "build_id": self.build_id, "stage": "starting_worker"})
            atomic_json(source.parent / "construction-progress.json", {
                "version": "construction_progress.v1", "build_id": self.build_id,
                "status": "running", "stage": "starting",
                "current_message": "构图 Agent 已启动，正在读取数据源。", "elapsed_seconds": 0,
                "metrics": {}, "events": [{"id": "stage:starting", "kind": "stage", "stage": "starting",
                    "message": "构图 Agent 已启动，正在读取数据源。"}],
            })
            # Only this dedicated coordination thread waits for the async
            # supervisor. It retains the dataset lock through publication.
            future = asyncio.run_coroutine_threadsafe(owner.supervise(self, staging, lease_fds, duration), owner.loop)
            try:
                result = future.result()
            except concurrent.futures.CancelledError:
                raise BuildFailed("construction_interrupted") from None
            if result.get("build_id") != self.build_id:
                raise BuildFailed("construction_worker_result_invalid")
            if result.get("failure_code"):
                raise BuildFailed(result["failure_code"])
            if result.get("filename") != "graph.json":
                raise BuildFailed("construction_worker_result_invalid")
            artifact = staging / "graph.json"
            if artifact.is_symlink() or artifact.resolve().parent != staging.resolve():
                raise BuildFailed("construction_worker_result_invalid")
            with artifact.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != result.get("sha256"):
                    raise BuildFailed("construction_worker_result_invalid")
                stream.seek(0)
                graph = json.load(stream)
            return graph
        except Exception as exc:
            try:
                report = json.loads(report_path.read_text())
                report.update(status="failed", build_id=self.build_id,
                              failure_code=getattr(exc, "code", "construction_failed"))
                atomic_json(report_path, report)
            except (OSError, ValueError):
                logger.warning("construction_report_write_failed build_id=%s", self.build_id)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


class ConstructionCoordinator:
    def __init__(self, store, *, concurrency=None):
        raw = os.getenv("GRAPH_CONSTRUCTION_CONCURRENCY", "1") if concurrency is None else str(concurrency)
        if not raw.isascii() or not raw.isdecimal() or int(raw) < 1:
            raise ValueError("GRAPH_CONSTRUCTION_CONCURRENCY must be a positive integer")
        self.limit = int(raw)
        self.store = store
        self.root = store.root / ".construction_runtime"
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="construction")
        self.active = {}
        self.workers = {}
        self.recoveries = {}
        self.pending_recovery = {}
        self.inventory_uncertain = False
        self.lease = self.loop = self.scanner = None
        self.closed = False
        self._start_lock = asyncio.Lock()

    async def restore(self):
        async with self._start_lock:
            if self.lease is not None:
                return
            self.loop = asyncio.get_running_loop()
            self.lease = await run_sync(ProcessLease, self.root / "services")
            await self.scan()
            self.scanner = asyncio.create_task(self._scan_loop())

    def _inventory(self):
        rows = []
        self.inventory_uncertain = False
        try:
            paths = list((self.root / "attempts").glob("*.json"))
        except OSError:
            self.inventory_uncertain = True
            logger.exception("construction_recovery_inventory_pending")
            return rows
        for path in paths:
            try:
                row = json.loads(path.read_text())
                if str(uuid.UUID(row["build_id"])) != path.stem:
                    raise ValueError("Invalid construction attempt identity")
                uuid.UUID(row["executor_instance"])
                self.store._directory(row["user_id"], row["dataset_id"])
                rows.append(row)
            except FileNotFoundError:
                continue  # Another recovering service already removed this row.
            except (OSError, ValueError, KeyError, TypeError):
                self.inventory_uncertain = True
                logger.exception("construction_recovery_record_invalid record=%s", path.name)
        return rows

    async def scan(self):
        rows = {**self.pending_recovery, **{row["build_id"]: row for row in await run_sync(self._inventory)}}
        for row in rows.values():
            build_id = row["build_id"]
            local_done = row["executor_instance"] == self.lease.identity and all(
                attempt.build_id != build_id for attempt in self.active.values()) and build_id not in self.workers
            if row["executor_instance"] == self.lease.identity and not local_done:
                continue
            owner = ProcessLease.inspect(self.root / "services", row["executor_instance"])
            if owner == "alive" and not local_done:
                continue
            self.pending_recovery[build_id] = row
            if (owner == "exited" or local_done) and build_id not in self.recoveries:
                task = asyncio.create_task(self._recover(row))
                self.recoveries[build_id] = task
                task.add_done_callback(lambda done, bid=build_id: self.recoveries.pop(bid, None))

    async def _recover(self, row):
        build_id = row["build_id"]
        try:
            local_done = row["executor_instance"] == self.lease.identity and all(
                attempt.build_id != build_id for attempt in self.active.values()) and build_id not in self.workers
            if not local_done and ProcessLease.inspect(self.root / "services", row["executor_instance"]) != "exited":
                return
            if not await recover_worker(self.root / "workers", build_id):
                logger.warning("construction_exit_unconfirmed build_id=%s", build_id)
                return
            await run_sync(self.store.finish_interrupted, row["user_id"], row["dataset_id"], build_id)
            staging = self.store._directory(row["user_id"], row["dataset_id"]) / ".builds" / build_id
            await run_sync(shutil.rmtree, staging, ignore_errors=True)
            await run_sync((self.root / "attempts" / f"{build_id}.json").unlink, missing_ok=True)
            self.pending_recovery.pop(build_id, None)
            logger.info("construction_recovered build_id=%s", build_id)
        except Exception:
            logger.exception("construction_recovery_pending build_id=%s", build_id)

    async def _scan_loop(self):
        while not self.closed:
            await asyncio.sleep(5)
            try:
                await self.scan()
            except Exception:
                logger.exception("construction_recovery_scan_failed")

    def ensure_writable(self, user_id, dataset_id):
        if (user_id, dataset_id) in self.active or any(
                (row["user_id"], row["dataset_id"]) == (user_id, dataset_id) for row in self.pending_recovery.values()):
            raise ConstructionError("construction_busy", "数据集正在构图或确认退出，请稍后重试。", 409, retryable=True)

    def _preflight(self, user_id, dataset_id, source_path, source_filename):
        reused = self.store.reuse_ready(user_id, dataset_id, source_path, source_filename)
        record = self.store._read(self.store._directory(user_id, dataset_id), dataset_id)
        return reused, record

    def _duplicate(self, attempt, fingerprint):
        if attempt.fingerprint != fingerprint:
            raise ConstructionError("construction_build_conflict", "该数据集正在使用其他来源或参数构图，请等待当前任务完成。", 409)
        logger.info("construction_deduplicated build_id=%s", attempt.build_id)
        return attempt.snapshot()

    async def build(self, user_id, dataset_id, source_path=None, source_filename=None, options=None):
        if self.closed:
            raise ConstructionError("construction_service_stopping", "服务正在关闭，请稍后重试。", 503)
        options = {} if options is None else copy.deepcopy(options)
        if not isinstance(options, dict):
            raise ConstructionError("invalid_operation", "options must be a JSON object.")
        _json_keys(options)
        if "construction_model" in options:
            from graph_engine.construction.llm import selected_model
            try:
                selected_model(options["construction_model"])
            except ValueError:
                raise ConstructionError("invalid_construction_model", "请选择服务器配置的构图模型。", 422) from None
        fingerprint = hashlib.sha256(_json([str(source_path) if source_path is not None else None,
                                           source_filename, options]).encode()).hexdigest()
        key = (user_id, dataset_id)
        await self.restore()
        if self.closed:
            raise ConstructionError("construction_service_stopping", "服务正在关闭，请稍后重试。", 503)
        if key in self.active:
            return self._duplicate(self.active[key], fingerprint)
        reused, record = await run_sync(self._preflight, user_id, dataset_id, source_path, source_filename)
        # The preflight yields; another request may already have claimed this key.
        if key in self.active:
            return self._duplicate(self.active[key], fingerprint)
        if reused is not None:
            return reused
        if record["status"] == "building":
            if record.get("request_fingerprint") == fingerprint:
                return self.store._status(record)
            raise ConstructionError("construction_build_conflict" if record.get("request_fingerprint") else "construction_busy",
                                    "该数据集已有构图任务，请稍后重试。", 409, retryable=True)
        recovering = list(self.pending_recovery.values())
        if self.closed:
            raise ConstructionError("construction_service_stopping", "服务正在关闭，请稍后重试。", 503)
        if (self.inventory_uncertain or len(self.active) + len(recovering) >= self.limit or
                any(uid == user_id for uid, _ in self.active) or any(row["user_id"] == user_id for row in recovering)):
            logger.info("construction_capacity_rejected active=%s limit=%s", len(self.active), self.limit)
            raise ConstructionError("construction_capacity_exhausted", "构图资源繁忙，本次未启动，请稍后重试。", 429, retryable=True)
        attempt = BuildAttempt(self, user_id, dataset_id, fingerprint, self.store._status(record))
        if source_filename:
            attempt.state.update(source_filename=source_filename, source_type=source_filename.rsplit(".", 1)[-1].lower())
            attempt.state["algorithm_available"] = attempt.state["source_type"] in self.store._algorithms
        self.active[key] = attempt
        attempt.task = asyncio.create_task(self._drive(attempt, source_path, source_filename, options))
        attempt.task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(attempt.task)

    def _execute(self, attempt, source_path, source_filename, options):
        journal = self.root / "attempts" / f"{attempt.build_id}.json"
        atomic_json(journal, {"build_id": attempt.build_id, "executor_instance": self.lease.identity,
                             "user_id": attempt.user_id, "dataset_id": attempt.dataset_id})
        try:
            return self.store.build(attempt.user_id, attempt.dataset_id, source_path, source_filename,
                                    options, _execution=attempt)
        except BaseException:
            try:
                self.store.finish_interrupted(attempt.user_id, attempt.dataset_id, attempt.build_id)
            except Exception:
                logger.exception("construction_terminal_write_pending build_id=%s", attempt.build_id)
            raise
        finally:
            # A ready record contains the build_id receipt. Recovery will keep
            # it even if the service dies before this journal is removed.
            record = self.store._read(self.store._directory(attempt.user_id, attempt.dataset_id), attempt.dataset_id)
            if record.get("build_id") != attempt.build_id or record["status"] != "building":
                journal.unlink(missing_ok=True)

    async def _drive(self, attempt, source_path, source_filename, options):
        started = time.monotonic()
        future = self.loop.run_in_executor(self.pool, self._execute, attempt, source_path, source_filename, options)
        try:
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    while not future.done():
                        try:
                            await asyncio.shield(future)
                        except asyncio.CancelledError:
                            continue
                if not future.cancelled():
                    future.exception()
                raise
        finally:
            self.active.pop((attempt.user_id, attempt.dataset_id), None)
            logger.info("construction_finished build_id=%s elapsed_seconds=%.3f", attempt.build_id, time.monotonic() - started)

    async def supervise(self, attempt, staging, lease_fds, duration):
        if self.closed:
            raise BuildFailed("construction_interrupted")
        worker = WorkerProcess()
        task = asyncio.create_task(worker.run(
            {"build_id": attempt.build_id, "request_path": str(staging / "request.json"),
             "deadline": time.monotonic() + duration},
            command=[sys.executable, "-m", "graph_data_service.construction_worker"],
            pass_fds=tuple(lease_fds), lease_root=self.root / "workers", job_id=attempt.build_id))
        self.workers[attempt.build_id] = task
        try:
            return await asyncio.wait_for(task, duration)
        except TimeoutError:
            logger.warning("construction_timeout build_id=%s", attempt.build_id)
            raise BuildFailed("construction_time_budget_exhausted") from None
        finally:
            self.workers.pop(attempt.build_id, None)

    async def status(self, user_id, dataset_id):
        attempt = self.active.get((user_id, dataset_id))
        if attempt is not None:
            return await run_sync(attempt.snapshot)
        return await run_sync(self.store.status, user_id, dataset_id)

    async def close(self):
        self.closed = True
        if self.scanner:
            self.scanner.cancel()
            await asyncio.gather(self.scanner, return_exceptions=True)
        for task in list(self.workers.values()):
            task.cancel()
        await asyncio.gather(*(a.task for a in list(self.active.values())), *list(self.recoveries.values()), return_exceptions=True)
        self.pool.shutdown(wait=True)
        if self.lease:
            self.lease.close()
