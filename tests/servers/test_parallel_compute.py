from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from graph_data_service.tool_scheduler import ToolResourceScheduler
from graph_engine.tool_execution import execution_policy

try:
    from graph_data_service.app import GraphDataService, create_app
    from graph_data_service.compute import run_nx_tool
except ImportError:
    GraphDataService = None


def _instrumented_nx(name, graph, arguments):
    """Separate worker PIDs must rendezvous before running real algorithms."""
    barrier = Path(arguments["_barrier_dir"])
    (barrier / f"{os.getpid()}.ready").touch()
    deadline = time.monotonic() + 8
    while len(list(barrier.glob("*.ready"))) < 3:
        if time.monotonic() > deadline:
            raise AssertionError("Three independent graph tools did not overlap")
        time.sleep(0.01)
    return {"pid": os.getpid(), "result": run_nx_tool(name, graph, arguments)}


def _slow_nx(name, graph, arguments):
    barrier = Path(arguments["_barrier_dir"])
    (barrier / "started").touch()
    deadline = time.monotonic() + 8
    while not (barrier / "release").exists():
        if time.monotonic() > deadline:
            raise AssertionError("Test worker was not released")
        time.sleep(0.01)
    return run_nx_tool(name, graph, arguments)


class ResourceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_readers_overlap_writer_keeps_arrival_order(self):
        scheduler = ToolResourceScheduler()
        owner = ("u", "s")
        reader = execution_policy("degree_analysis")
        first = await scheduler.acquire(owner, reader)
        second = await asyncio.wait_for(scheduler.acquire(owner, reader), 1)
        writer = asyncio.create_task(scheduler.acquire(owner, execution_policy("clear_graph")))
        await asyncio.sleep(0)
        later = asyncio.create_task(scheduler.acquire(owner, reader))
        await asyncio.sleep(0)
        self.assertFalse(writer.done())
        self.assertFalse(later.done())
        scheduler.release(first)
        scheduler.release(second)
        write_lease = await asyncio.wait_for(writer, 1)
        self.assertFalse(later.done())
        scheduler.release(write_lease)
        scheduler.release(await asyncio.wait_for(later, 1))
        self.assertEqual(scheduler._pending, [])

    async def test_cancelling_waiter_does_not_cancel_predecessor(self):
        scheduler = ToolResourceScheduler()
        first = await scheduler.acquire(("u", "s"), execution_policy("load_graph"))
        waiting = asyncio.create_task(scheduler.acquire(("u", "s"), execution_policy("get_graph_info")))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(first.done.done())
        scheduler.release(first)
        self.assertEqual(scheduler._pending, [])

    async def test_user_resources_conflict_across_sessions_but_graphs_do_not(self):
        scheduler = ToolResourceScheduler()
        first = await scheduler.acquire(("u", "s1"), execution_policy("commit_graph_optimization", {"dataset_id": "d"}))
        waiting = asyncio.create_task(scheduler.acquire(("u", "s2"), execution_policy("get_constructed_graph", {"dataset_id": "d"})))
        unrelated = await asyncio.wait_for(scheduler.acquire(("v", "s2"), execution_policy("get_constructed_graph", {"dataset_id": "d"})), 1)
        graph = await asyncio.wait_for(scheduler.acquire(("u", "s2"), execution_policy("load_graph")), 1)
        self.assertFalse(waiting.done())
        scheduler.release(first)
        scheduler.release(await asyncio.wait_for(waiting, 1))
        scheduler.release(unrelated)
        scheduler.release(graph)


@unittest.skipUnless(GraphDataService is not None, "requires Graph Engine Linux dependencies")
class ParallelGraphComputeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "IGRAPH_DATA_ROOT": self.directory.name,
            "IGRAPH_RUN_CANCEL_ENABLED": "false",
            "GRAPH_STORAGE_BACKEND": "local",
            "GRAPH_COMPUTE_CONCURRENCY": "3",
        })
        self.environment.start()
        self.limits = patch.dict(os.environ)
        self.limits.start()
        os.environ.pop("GRAPH_COMPUTE_PER_USER_CONCURRENCY", None)
        os.environ.pop("GRAPH_COMPUTE_GLOBAL_CONCURRENCY", None)
        self.service = GraphDataService(migrate_storage=False)
        self.service.get_or_create("s", user_id="u")
        await self.service.dispatch_tool("s", "load_graph", {"edge_text": "A B 1\nB C 2\nA C 5\nC D 1"}, "u")

    async def asyncTearDown(self):
        self.service._job_manager.cancel_all()
        await asyncio.gather(*(self.service._job_manager.wait(j) for j in self.service._job_manager._tasks))
        await asyncio.gather(*self.service._tool_reservations)
        self.service._process_pool.shutdown(wait=True, cancel_futures=True)
        self.service._nebula.close()
        from graph_engine.observability import close_service_loggers
        close_service_loggers("s")
        self.limits.stop()
        self.environment.stop()
        self.directory.cleanup()

    async def test_three_real_algorithms_overlap_and_match_sequential_answers(self):
        self.assertEqual(self.service._compute_per_user_limit, 3)
        self.assertEqual(self.service._heavy_per_user_limit, 1)
        tools = [("shortest_path", {"source": "A", "target": "D"}),
                 ("degree_analysis", {}), ("graph_connectivity", {})]
        _, _, graph = self.service._require_graph("s", "u")
        expected = [run_nx_tool(name, graph, args) for name, args in tools]
        barrier = Path(self.directory.name) / "barrier"
        barrier.mkdir()
        with patch("graph_data_service.app.run_nx_tool", _instrumented_nx):
            results = await asyncio.wait_for(asyncio.gather(*[
                self.service.dispatch_tool("s", name, {**args, "_barrier_dir": str(barrier)}, "u")
                for name, args in tools
            ]), 15)
        self.assertEqual(len({result["pid"] for result in results}), 3)
        self.assertEqual([result["result"] for result in results], expected)
        self.assertEqual(results[0]["result"]["path"], ["A", "B", "C", "D"])
        self.assertEqual(results[0]["result"]["total_length"], 4)

    async def test_async_graph_write_keeps_readers_waiting_and_job_control_available(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def build(*args, **kwargs):
            started.set()
            await finish.wait()
            return self.service.do_load_graph_text("s", "X Y\nY Z", False, "u")

        with patch.object(self.service, "_do_load_graph_from_text_limited", build):
            submitted = await self.service.dispatch_tool("s", "load_graph_from_text", {"text": "new graph"}, "u")
            await started.wait()
            reader = asyncio.create_task(self.service.dispatch_tool("s", "get_graph_info", {}, "u"))
            await asyncio.sleep(0.02)
            self.assertFalse(reader.done())
            status = await asyncio.wait_for(self.service.dispatch_tool("s", "get_job_status", {"job_id": submitted["job_id"]}, "u"), 1)
            self.assertEqual(status["job"]["status"], "running")
            finish.set()
            result = await asyncio.wait_for(reader, 2)
        self.assertEqual(result["num_nodes"], 3)
        self.assertEqual(self.service.get_required("s", "u").get_graph().node2id.keys(), {"X", "Y", "Z"})

    async def test_cancelled_background_write_releases_graph_after_job_exits(self):
        started = asyncio.Event()

        async def build(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(self.service, "_do_load_graph_from_text_limited", build):
            submitted = await self.service.dispatch_tool("s", "load_graph_from_text", {"text": "cancel me"}, "u")
            await started.wait()
            reader = asyncio.create_task(self.service.dispatch_tool("s", "get_graph_info", {}, "u"))
            cancelled = await asyncio.wait_for(self.service.dispatch_tool("s", "cancel_job", {"job_id": submitted["job_id"]}, "u"), 1)
            self.assertTrue(cancelled["success"])
            result = await asyncio.wait_for(reader, 2)
        self.assertEqual(result["num_nodes"], 4)

    async def test_concurrent_appends_do_not_lose_updates(self):
        await asyncio.gather(*[self.service.dispatch_tool("s", "load_graph", {
            "edge_text": f"D N{i}", "append": True}, "u") for i in range(8)])
        graph = self.service.get_required("s", "u").get_graph()
        self.assertEqual(graph.num_nodes, 12)
        self.assertEqual(graph.num_edges, 12)

    async def test_queued_append_rechecks_graph_after_an_earlier_write(self):
        from tests.servers.test_property_graph_analysis import property_graph

        lease = await self.service._tool_scheduler.acquire(("u", "s"), execution_policy("load_graph"))
        task = asyncio.create_task(self.service.dispatch_tool(
            "s", "load_graph", {"edge_text": "B C 1", "append": True}, "u"))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            session = self.service.get_required("s", "u")
            protected = property_graph([("ab2", "A", "B", 2), ("ab7", "A", "B", 7)])
            session.set_graph(protected)
            version = session.graph_version
        finally:
            self.service._tool_scheduler.release(lease)
        result = await asyncio.wait_for(task, 2)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "UNSUPPORTED_PROPERTY_GRAPH_APPEND")
        self.assertIs(session.get_graph(), protected)
        self.assertEqual(session.graph_version, version)

    async def test_direct_http_write_obeys_active_read_reservation(self):
        import httpx
        lease = await self.service._tool_scheduler.acquire(("u", "s"), execution_policy("degree_analysis"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.service)), base_url="http://engine") as client:
            # Session lookup trims whitespace; admission must use the same
            # identity or equivalent URL spellings could bypass its lease.
            write = asyncio.create_task(client.post("/sessions/%20s%20/graph/clear", headers={"X-User-ID": "u"}))
            await asyncio.sleep(0.02)
            self.assertFalse(write.done())
            self.service._tool_scheduler.release(lease)
            response = await asyncio.wait_for(write, 2)
        self.assertTrue(response.json()["success"])
        self.assertIsNone(self.service.get_required("s", "u").get_graph())

    async def test_cancelled_process_retains_graph_and_capacity_until_actual_exit(self):
        barrier = Path(self.directory.name) / "cancel_barrier"
        barrier.mkdir()
        with patch("graph_data_service.app.run_nx_tool", _slow_nx):
            reader = asyncio.create_task(self.service.dispatch_tool("s", "degree_analysis", {"_barrier_dir": str(barrier)}, "u"))
            async def wait_started():
                while not (barrier / "started").exists():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_started(), 5)
            writer = asyncio.create_task(self.service.dispatch_tool("s", "clear_graph", {}, "u"))
            reader.cancel()
            await asyncio.sleep(0.03)
            self.assertFalse(reader.done())
            self.assertFalse(writer.done())
            self.assertEqual(self.service._compute_global_sem._value, 2)
            (barrier / "release").touch()
            with self.assertRaises(asyncio.CancelledError):
                await reader
            await asyncio.wait_for(writer, 2)
        self.assertEqual(self.service._compute_global_sem._value, 3)
        self.assertEqual(self.service._tool_scheduler._pending, [])

    async def test_graph_snapshot_resolution_does_not_block_event_loop(self):
        import threading
        started, release = threading.Event(), threading.Event()
        resolve = self.service._compute.resolve_graph

        def slow_resolve(*args):
            started.set()
            if not release.wait(2):
                raise AssertionError("Event loop was blocked while preparing a graph")
            return resolve(*args)

        with patch.object(self.service._compute, "resolve_graph", slow_resolve):
            reader = asyncio.create_task(self.service.dispatch_tool("s", "degree_analysis", {}, "u"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            release.set()
            result = await asyncio.wait_for(reader, 5)
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
