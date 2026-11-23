"""Construction admission and lifetime tests use real threads and processes."""
import asyncio
import concurrent.futures
import json
import threading
import time

import pytest

from execution_lifecycle import ProcessLease, atomic_json
from graph_data_service.async_work import run_sync
from graph_data_service.construction import ConstructionError, ConstructionStore
from graph_data_service.construction_coordinator import ConstructionCoordinator
from graph_engine.construction import register_builtin_algorithms


GRAPH = {"directed": True, "nodes": [{"id": "one", "type": "Person", "properties": {"name": "Alice"}}], "edges": []}


def source_file(tmp_path):
    source = tmp_path / "people.csv"
    source.write_text("id,name\n1,Alice\n")
    return source


async def entered(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 2)


def test_duplicate_builds_do_not_starve_default_executor(tmp_path):
    async def scenario():
        store, source = ConstructionStore(tmp_path), source_file(tmp_path)
        start, release = threading.Event(), threading.Event()
        calls = []
        def algorithm(*_args):
            calls.append(threading.current_thread().name)
            start.set()
            assert release.wait(5)
            return GRAPH
        store.register_algorithm("csv", algorithm)
        owner = ConstructionCoordinator(store)
        asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        first = asyncio.create_task(owner.build("alice", "orders", source, "people.csv"))
        try:
            await entered(start)
            duplicate = await asyncio.wait_for(asyncio.gather(*[
                owner.build("alice", "orders", source, "people.csv") for _ in range(31)]), 1)
            assert len({state["build_id"] for state in duplicate}) == 1
            assert all(state["status"] == "building" for state in duplicate)
            assert len(calls) == 1 and calls[0].startswith("construction")
            state, datasets, result = await asyncio.wait_for(asyncio.gather(
                run_sync(store.status, "alice", "orders"), run_sync(store.list_ready, "alice"), run_sync(lambda: 42)), 1)
            assert state["status"] == "building" and datasets == [] and result == 42
            with pytest.raises(ConstructionError) as error:
                await owner.build("bob", "another", source, "people.csv")
            assert error.value.status_code == 429
            assert store.status("bob", "another")["status"] == "not_built"
            with pytest.raises(ConstructionError) as conflict:
                await owner.build("alice", "orders", source, "people.csv", {"mode": "rules"})
            assert conflict.value.detail["code"] == "construction_build_conflict"
            for operation in [lambda: store.delete("alice", "orders"),
                              lambda: store.register_source("alice", "orders", source, "people.csv"),
                              lambda: store.optimize("alice", "orders", 1, [])]:
                with pytest.raises(ConstructionError) as busy:
                    await asyncio.wait_for(run_sync(operation), 1)
                assert busy.value.detail["code"] == "construction_busy"
        finally:
            release.set()
            built = await first
            await owner.close()
        assert built["status"] == "draft" and built["revision"] == 1
        assert not owner.active
    asyncio.run(scenario())


def test_disconnect_retains_task_and_ready_reuse_does_not_need_capacity(tmp_path):
    async def scenario():
        store, source = ConstructionStore(tmp_path), source_file(tmp_path)
        store.register_algorithm("csv", lambda *_: GRAPH)
        store.build("bob", "ready", source, "people.csv")
        start, release = threading.Event(), threading.Event()
        def slow(*_args):
            start.set()
            assert release.wait(5)
            return GRAPH
        store.register_algorithm("csv", slow)
        owner = ConstructionCoordinator(store)
        request = asyncio.create_task(owner.build("alice", "slow", source, "people.csv"))
        try:
            await entered(start)
            task = owner.active["alice", "slow"].task
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not task.done()
            assert (await owner.status("alice", "slow"))["status"] == "building"
            assert (await owner.build("bob", "ready", source, "people.csv"))["reused"]
        finally:
            release.set()
            await owner.close()
        assert store.graph("alice", "slow")["revision"] == 1
    asyncio.run(scenario())


def test_active_status_reads_the_small_public_progress_sidecar(tmp_path):
    async def scenario():
        store, source = ConstructionStore(tmp_path), source_file(tmp_path)
        start, release = threading.Event(), threading.Event()
        def slow(*_args):
            start.set()
            assert release.wait(5)
            return GRAPH
        store.register_algorithm("csv", slow)
        owner = ConstructionCoordinator(store)
        request = asyncio.create_task(owner.build("alice", "progress", source, "people.csv"))
        try:
            await entered(start)
            attempt = owner.active["alice", "progress"]
            directory = store._directory("alice", "progress")
            atomic_json(directory / "construction-progress.json", {
                "version": "construction_progress.v1", "build_id": attempt.build_id,
                "status": "running", "mode": "llm", "stage": "evolving", "current_message": "Agent 正在验收候选。",
                "elapsed_seconds": 8, "metrics": {"iterations": 2, "evaluated_candidates": 1},
                "events": [{"id": "stage:evolving", "kind": "stage", "stage": "evolving",
                            "message": "Agent 正在尝试候选图结构。"}],
                "queries": [{"must_not_leak": True}],
            })
            status = await owner.status("alice", "progress")
            assert status["status"] == "building"
            assert status["message"] == "Agent 正在验收候选。"
            assert status["progress"]["metrics"] == {"iterations": 2, "evaluated_candidates": 1}
            assert status["progress"]["mode"] == "llm"
            assert "queries" not in status["progress"]
            (directory / "construction-progress.json").write_text("{", encoding="utf-8")
            fallback = await owner.status("alice", "progress")
            assert fallback["status"] == "building" and "progress" not in fallback
        finally:
            release.set()
            await request
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true", "", " 2"])
def test_invalid_capacity_fails_at_creation(tmp_path, value):
    with pytest.raises(ValueError, match="GRAPH_CONSTRUCTION_CONCURRENCY"):
        ConstructionCoordinator(ConstructionStore(tmp_path), concurrency=value)


def test_user_limit_applies_before_global_capacity_is_full(tmp_path):
    async def scenario():
        store, source = ConstructionStore(tmp_path), source_file(tmp_path)
        events = [threading.Event(), threading.Event()]
        release = threading.Event()
        calls = []
        def slow(*_args):
            number = len(calls)
            calls.append(True)
            events[number].set()
            assert release.wait(5)
            return GRAPH
        store.register_algorithm("csv", slow)
        owner = ConstructionCoordinator(store, concurrency=2)
        first = asyncio.create_task(owner.build("alice", "first", source, "people.csv"))
        second = None
        try:
            await entered(events[0])
            with pytest.raises(ConstructionError) as rejected:
                await owner.build("alice", "second", source, "people.csv")
            assert rejected.value.status_code == 429
            second = asyncio.create_task(owner.build("bob", "second", source, "people.csv"))
            await entered(events[1])
            assert len(owner.active) == 2 and len(calls) == 2
        finally:
            release.set()
            await first
            if second:
                await second
            await owner.close()
    asyncio.run(scenario())


def test_process_start_failure_releases_capacity(tmp_path, monkeypatch):
    from graph_data_service.worker_process import WorkerProcess
    async def fail(*_args, **_kwargs):
        raise OSError("injected spawn failure")
    monkeypatch.setattr(WorkerProcess, "run", fail)
    async def scenario():
        store = register_builtin_algorithms(ConstructionStore(tmp_path))
        owner = ConstructionCoordinator(store)
        try:
            result = await owner.build("alice", "spawn", source_file(tmp_path), "people.csv", {"mode": "rules"})
            assert result["status"] == "failed" and not owner.active
            assert not list((owner.root / "attempts").glob("*.json"))
            store.register_algorithm("csv", lambda *_: GRAPH)
            assert (await owner.build("alice", "spawn"))["status"] == "draft"
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_source_validation_failure_releases_capacity_without_publishing(tmp_path):
    async def scenario():
        store, source = ConstructionStore(tmp_path), source_file(tmp_path)
        store.register_algorithm("csv", lambda *_: GRAPH)
        owner = ConstructionCoordinator(store)
        try:
            with pytest.raises(ConstructionError):
                await owner.build("alice", "invalid", tmp_path.parent / "missing.csv", "missing.csv")
            assert not owner.active
            assert store.status("alice", "invalid")["status"] == "not_built"
            assert (await owner.build("alice", "invalid", source, "people.csv"))["revision"] == 1
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_corrupt_recovery_record_blocks_construction_but_not_status(tmp_path):
    async def scenario():
        store = ConstructionStore(tmp_path)
        journal = tmp_path / ".construction_runtime/attempts/broken.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{")
        owner = ConstructionCoordinator(store)
        try:
            await owner.restore()
            assert (await owner.status("alice", "source"))["status"] == "not_built"
            with pytest.raises(ConstructionError) as rejected:
                await owner.build("alice", "source")
            assert rejected.value.status_code == 429
            journal.unlink()
            await owner.scan()
            assert not owner.inventory_uncertain
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_recovery_does_not_downgrade_a_committed_graph(tmp_path):
    async def scenario():
        import uuid
        store = ConstructionStore(tmp_path)
        store.register_algorithm("csv", lambda *_: GRAPH)
        store.build("alice", "committed", source_file(tmp_path), "people.csv")
        store.optimize("alice", "committed", 1, [], commit=True)
        root = tmp_path / ".construction_runtime"
        old = ProcessLease(root / "services")
        build_id = str(uuid.uuid4())
        directory = store._directory("alice", "committed")
        record = store._read(directory, "committed")
        record.update(build_id=build_id, executor_instance=old.identity)
        atomic_json(directory / "record.json", record)
        atomic_json(root / "attempts" / f"{build_id}.json", {"build_id": build_id, "executor_instance": old.identity,
                    "user_id": "alice", "dataset_id": "committed"})
        old.close()
        owner = ConstructionCoordinator(store)
        try:
            await owner.restore()
            await asyncio.gather(*owner.recoveries.values())
            assert store.status("alice", "committed")["status"] == "ready"
            assert store.graph("alice", "committed")["revision"] == 2
            assert store.graph("alice", "committed")["graph"] == GRAPH
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_builtin_csv_uses_managed_process_and_preserves_source_name(tmp_path):
    async def scenario():
        store = register_builtin_algorithms(ConstructionStore(tmp_path))
        owner = ConstructionCoordinator(store)
        try:
            result = await owner.build("alice", "csv", source_file(tmp_path), "people.csv",
                                       {"primary_key": ["id"], "mode": "rules", "query_count": 1, "benchmark_repeats": 1, "experience_enabled": False})
            assert result["status"] == "draft", result
            graph = store.graph("alice", "csv")["graph"]
            assert graph["nodes"][0]["type"] == "people"
            assert graph["nodes"][0]["properties"]["data"]["name"] == "Alice"
            assert result["progress"]["version"] == "construction_progress.v1"
            assert any(event.get("outcome") == "ready" for event in result["progress"]["events"])
            public_sidecar = json.loads((store._directory("alice", "csv") / "construction-progress.json").read_text())
            assert not ({"queries", "source_schema", "history", "tool_trace", "domain_profile"} & public_sidecar.keys())
            assert list((owner.root / "workers").glob("*.identity.json"))
            assert not list((owner.root / "attempts").glob("*.json"))
            assert not list((tmp_path / "users/alice/datasets/csv/construction/.builds").iterdir())
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_builtin_txt_uses_managed_process_and_preserves_source_name(tmp_path):
    async def scenario():
        source = tmp_path / "people.txt"
        source.write_text("id\tname\n1\tAlice\n2\tBob\n", encoding="utf-8")
        store = register_builtin_algorithms(ConstructionStore(tmp_path))
        owner = ConstructionCoordinator(store)
        try:
            result = await owner.build(
                "alice", "txt", source, "people.txt",
                {"mode": "rules", "query_count": 1, "benchmark_repeats": 1, "experience_enabled": False},
            )
            assert result["status"] == "draft", result
            assert result["source_type"] == "txt" and result["algorithm_available"] is True
            graph = store.graph("alice", "txt")["graph"]
            assert len(graph["nodes"]) == 2 and graph["edges"] == []
            assert {node["type"] for node in graph["nodes"]} == {"people"}
            assert graph["nodes"][0]["properties"]["data"]["name"] == "Alice"
            assert any(event.get("outcome") == "ready" for event in result["progress"]["events"])
            assert list((owner.root / "workers").glob("*.identity.json"))
            assert not list((owner.root / "attempts").glob("*.json"))
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_hard_deadline_reaps_uncooperative_worker_before_retry(tmp_path, monkeypatch):
    from graph_data_service.worker_process import WorkerProcess
    original = WorkerProcess.run
    processes = []
    async def stuck(self, payload, *args, **kwargs):
        processes.append(self)
        kwargs["command"] = [__import__("sys").executable, "-c", "import sys,signal,time; sys.stdin.readline(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"]
        return await original(self, payload, *args, **kwargs)
    monkeypatch.setattr(WorkerProcess, "run", stuck)
    async def scenario():
        store = register_builtin_algorithms(ConstructionStore(tmp_path))
        owner = ConstructionCoordinator(store)
        try:
            start = time.monotonic()
            result = await owner.build("alice", "timeout", source_file(tmp_path), "people.csv", {"mode": "rules", "max_seconds": 1})
            assert time.monotonic() - start < 7
            assert result["status"] == "failed" and result["failure_code"] == "construction_time_budget_exhausted"
            assert processes[0].process.returncode is not None
            assert not owner.active
            assert store.status("alice", "timeout")["revision"] == 0
            assert (tmp_path / "users/alice/datasets/timeout/construction/evolution-report.json").exists()
            monkeypatch.setattr(WorkerProcess, "run", original)
            retry = await owner.build("alice", "timeout", options={"primary_key": ["id"], "mode": "rules", "query_count": 1, "experience_enabled": False})
            assert retry["status"] == "draft" and retry["build_id"] != result["build_id"]
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_service_shutdown_reaps_worker_before_releasing_owner_lease(tmp_path, monkeypatch):
    from graph_data_service.worker_process import WorkerProcess
    import sys
    original, workers = WorkerProcess.run, []
    async def slow(self, payload, *args, **kwargs):
        workers.append(self)
        kwargs["command"] = [sys.executable, "-c", "import sys,signal,time; sys.stdin.readline(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)"]
        return await original(self, payload, *args, **kwargs)
    monkeypatch.setattr(WorkerProcess, "run", slow)
    async def scenario():
        owner = ConstructionCoordinator(register_builtin_algorithms(ConstructionStore(tmp_path)))
        request = asyncio.create_task(owner.build("alice", "shutdown", source_file(tmp_path), "people.csv", {"mode": "rules", "max_seconds": 60}))
        try:
            async def launched():
                while not workers or workers[0].process is None:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(launched(), 3)
            assert ProcessLease.inspect(owner.lease.root, owner.lease.identity) == "alive"
            await asyncio.wait_for(owner.close(), 6)
            result = await request
            assert result["status"] == "failed" and result["failure_code"] == "construction_interrupted"
            assert workers[0].process.returncode is not None
            assert ProcessLease.inspect(owner.lease.root, owner.lease.identity) == "exited"
            assert not owner.active
        finally:
            await owner.close()
            await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())


def test_publication_failure_does_not_leave_unrecoverable_build(tmp_path, monkeypatch):
    from graph_data_service import construction
    original = construction._atomic_json
    def fail_ready(path, record, **kwargs):
        if record.get("status") == "draft":
            raise OSError("injected publication failure")
        return original(path, record, **kwargs)
    async def scenario():
        store = ConstructionStore(tmp_path)
        store.register_algorithm("csv", lambda *_: GRAPH)
        owner = ConstructionCoordinator(store)
        try:
            monkeypatch.setattr(construction, "_atomic_json", fail_ready)
            with pytest.raises(OSError):
                await owner.build("alice", "failure", source_file(tmp_path), "people.csv")
            assert store.status("alice", "failure")["status"] == "failed"
            assert not owner.active
            monkeypatch.setattr(construction, "_atomic_json", original)
            assert (await owner.build("alice", "failure"))["status"] == "draft"
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_restart_waits_for_exit_proof_and_preserves_committed_results(tmp_path, monkeypatch):
    import uuid
    from graph_data_service import construction_coordinator as module
    async def scenario():
        store = ConstructionStore(tmp_path)
        root = tmp_path / ".construction_runtime"
        old = ProcessLease(root / "services")
        build_id = str(uuid.uuid4())
        store.register_source("alice", "old", source_file(tmp_path), "people.csv")
        directory = store._directory("alice", "old")
        record = store._read(directory, "old")
        record.update(status="building", build_id=build_id, executor_instance=old.identity)
        atomic_json(directory / "record.json", record)
        atomic_json(root / "attempts" / f"{build_id}.json", {"build_id": build_id, "executor_instance": old.identity,
                    "user_id": "alice", "dataset_id": "old"})
        confirmed = False
        async def recover(*_args):
            return confirmed
        monkeypatch.setattr(module, "recover_worker", recover)
        owner = ConstructionCoordinator(store)
        try:
            await owner.restore()
            assert not owner.pending_recovery  # A live other service is not reclaimed.
            old.close()
            await owner.scan()
            await asyncio.gather(*owner.recoveries.values())
            assert build_id in owner.pending_recovery
            assert store.status("alice", "old")["status"] == "building"
            with pytest.raises(ConstructionError):
                store.delete("alice", "old")
            confirmed = True
            await owner.scan()
            await asyncio.gather(*owner.recoveries.values())
            assert not owner.pending_recovery
            assert store.status("alice", "old")["failure_code"] == "construction_interrupted"
        finally:
            old.close()
            await owner.close()
    asyncio.run(scenario())


def test_restart_reaps_real_orphan_group_and_inherited_dataset_lock(tmp_path):
    import os
    import subprocess
    import sys
    import psutil
    source_file(tmp_path)
    # The child-service launches a real supervised group. Its stand-in worker
    # ignores TERM and starts a descendant so recovery must use durable identity
    # evidence, not merely observe the original service PID disappear.
    script = r'''
import asyncio, json, signal, sys, time
from pathlib import Path
from graph_data_service.construction import ConstructionStore
from graph_data_service.construction_coordinator import ConstructionCoordinator
from graph_data_service.worker_process import WorkerProcess
from graph_engine.construction import register_builtin_algorithms
root = Path(sys.argv[1])
original = WorkerProcess.run
worker_code = """
import json, os, signal, subprocess, sys, time
payload = json.loads(sys.stdin.readline())
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)'])
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child':child.pid,'build_id':payload['build_id']}))
time.sleep(120)
"""
async def slow(self, payload, *args, **kwargs):
    kwargs['command'] = [sys.executable, '-c', worker_code, str(root / 'entered.json')]
    return await original(self, payload, *args, **kwargs)
WorkerProcess.run = slow
async def main():
    owner = ConstructionCoordinator(register_builtin_algorithms(ConstructionStore(root)))
    await owner.build('alice', 'orphan', root / 'people.csv', 'people.csv', {'mode':'rules','max_seconds':60})
asyncio.run(main())
'''
    parent = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    members = []
    try:
        expires = time.monotonic() + 8
        while time.monotonic() < expires:
            marker = tmp_path / "entered.json"
            if marker.exists():
                started = json.loads(marker.read_text())
                identity = tmp_path / ".construction_runtime/workers" / (started["build_id"] + ".identity.json")
                if identity.exists() and len(json.loads(identity.read_text()).get("members", [])) >= 2:
                    break
            time.sleep(.02)
        else:
            pytest.fail("worker and descendant were not registered")
        members = [psutil.Process(started["pid"]), psutil.Process(started["child"])]
        parent.kill()
        parent.wait(timeout=3)
        async def scenario():
            store = ConstructionStore(tmp_path)
            assert store.status("alice", "orphan")["status"] == "building"
            with pytest.raises(ConstructionError):
                store.delete("alice", "orphan")
            owner = ConstructionCoordinator(store)
            try:
                await owner.restore()
                assert owner.pending_recovery
                await asyncio.wait_for(asyncio.gather(*owner.recoveries.values()), 8)
                assert not owner.pending_recovery
                assert store.status("alice", "orphan")["failure_code"] == "construction_interrupted"
                for member in members:
                    assert not member.is_running() or member.status() == psutil.STATUS_ZOMBIE
                assert not list((tmp_path / "users/alice/datasets/orphan/construction/.builds").iterdir())
                store.register_algorithm("csv", lambda *_: GRAPH)
                assert (await owner.build("alice", "orphan"))["status"] == "draft"
            finally:
                await owner.close()
        asyncio.run(scenario())
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        # Only identity-checked handles created by this test grant cleanup.
        for member in members:
            try:
                if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                    member.kill()
            except psutil.NoSuchProcess:
                pass
