from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

import numpy as np

from graph_data_service.app import GraphDataService
from graph_data_service.graph_data import GraphData
from graph_data_service.tool_catalog import get_all_public_tools
from graph_data_service.storage.nebula_backend import (
    NebulaConfig,
    NebulaGraphRepository,
    NebulaRepositoryError,
    graph_key_for,
    node_vid,
    scope_key,
)


class RecordingNebulaRepository(NebulaGraphRepository):
    def __init__(self) -> None:
        super().__init__(
            NebulaConfig(
                enabled=True,
                hosts=(("127.0.0.1", 9669),),
                user="root",
                password="nebula",
                space="igraph_test",
                batch_size=2,
                max_retries=1,
            )
        )
        self.queries: list[str] = []
        self.active: dict | None = None
        self.deleted: list[str] = []

    def ensure_schema(self) -> None:
        self._schema_ready = True

    def _execute(self, query: str, *, use_space: bool = True):
        self.queries.append(query)
        return []

    def _active_metadata(self, scope: str):
        return dict(self.active) if self.active is not None else None

    def _publish_metadata(self, graph, **kwargs) -> None:
        super()._publish_metadata(graph, **kwargs)
        self.active = {
            "scope_key": kwargs["scope"],
            "graph_key": kwargs["graph_key"],
            "graph_version": kwargs["graph_version"],
            "node_count": graph.num_nodes,
            "edge_count": graph.num_edges,
        }

    def _delete_graph_key(self, graph_key: str) -> int:
        self.deleted.append(graph_key)
        return 0


class FakeServiceNebulaRepository:
    enabled = True

    def __init__(self) -> None:
        self.closed = False
        self.synced_version: int | None = None

    def close(self) -> None:
        self.closed = True

    def session_status(self, user_id: str, session_id: str, graph_version: int) -> dict:
        return {
            "success": True,
            "backend": "nebula",
            "enabled": True,
            "connected": True,
            "schema_ready": True,
            "session_id": session_id,
            "graph_version": graph_version,
            "synced_graph_version": self.synced_version,
            "synced": self.synced_version == graph_version,
            "stale": self.synced_version not in (None, graph_version),
        }

    def sync_graph(self, graph: GraphData, **kwargs) -> dict:
        callback = kwargs.get("progress_callback")
        if callback:
            callback({"progress": 0.5, "message": "half"})
        version = int(kwargs["graph_version"])
        getter = kwargs.get("current_version_getter")
        if getter and int(getter()) != version:
            raise NebulaRepositoryError("STALE_DURING_SYNC", "stale")
        self.synced_version = version
        return {
            "success": True,
            "backend": "nebula",
            "graph_version": version,
            "node_count": graph.num_nodes,
            "edge_count": graph.num_edges,
            "features_omitted": [],
            "warnings": [],
        }

    def get_node_info(self, graph: GraphData, **kwargs) -> dict:
        return {
            "success": True,
            "backend": "nebula",
            "node": {"node_name": kwargs["node_name"]},
        }

    def get_neighbors(self, graph: GraphData, **kwargs) -> dict:
        return {
            "success": True,
            "backend": "nebula",
            "neighbors": [{"node_name": "B"}],
            "count": 1,
        }

    def shortest_path(self, graph: GraphData, **kwargs) -> dict:
        return {
            "success": True,
            "backend": "nebula",
            "found": True,
            "path": [kwargs["source"], kwargs["target"]],
        }


class RecordingJobManager:
    def __init__(self) -> None:
        self.submission: tuple | None = None

    async def submit(self, session_id, tool_name, fn, *args, **kwargs) -> str:
        self.submission = (session_id, tool_name, fn, args, kwargs)
        return "job-nebula-1"


class NebulaBackendTest(unittest.TestCase):
    def _graph(self) -> GraphData:
        return GraphData(
            num_nodes=3,
            edge_index=np.asarray([[0, 0, 1], [1, 1, 2]], dtype=np.int64),
            id2node={0: '节点"A', 1: "B", 2: "C"},
            node2id={'节点"A': 0, "B": 1, "C": 2},
            edge_names=["e0", "e1", "e2"],
            edge_weight=np.asarray([1.0, np.nan, 3.0], dtype=np.float32),
            node_feat=np.asarray([[1.0], [2.0], [3.0]], dtype=np.float32),
            edge_feat=np.asarray([[0.9], [0.8], [0.7]], dtype=np.float32),
            node_label=np.asarray(["x", "y", None], dtype=object),
            node_type=np.asarray(["person", "person", "org"], dtype=object),
            edge_type=np.asarray(["knows", "knows", "works_at"], dtype=object),
            edge_text=["quoted \\\"text", "", "evidence"],
            directed=True,
        )

    def test_config_and_scoped_ids_are_stable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "NEBULA_ENABLED": "1",
                "NEBULA_HOSTS": "graph-a:9669,graph-b:19669",
                "NEBULA_USER": "tester",
                "NEBULA_PASSWORD": "secret",
                "NEBULA_SPACE": "igraph_dev",
            },
            clear=False,
        ):
            config = NebulaConfig.from_env()
        self.assertTrue(config.enabled)
        self.assertEqual(config.hosts, (("graph-a", 9669), ("graph-b", 19669)))
        config.validate()

        alice = scope_key("alice", "s1")
        self.assertEqual(alice, scope_key("alice", "s1"))
        self.assertNotEqual(alice, scope_key("bob", "s1"))
        graph_key = graph_key_for(alice, 3)
        self.assertEqual(len(graph_key), 64)
        self.assertNotEqual(node_vid(graph_key, 0), node_vid(graph_key, 1))

    def test_sync_generates_batched_heterogeneous_ngql(self) -> None:
        repo = RecordingNebulaRepository()
        graph = self._graph()
        result = repo.sync_graph(
            graph,
            user_id="alice",
            session_id="session-1",
            graph_version=1,
            current_version_getter=lambda: 1,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["node_count"], 3)
        self.assertEqual(result["edge_count"], 3)
        self.assertIn("node_feat", result["features_omitted"])
        statements = "\n".join(repo.queries)
        self.assertIn("INSERT VERTEX igraph_entity", statements)
        self.assertIn("INSERT EDGE igraph_relation", statements)
        self.assertIn('节点\\"A', statements)
        self.assertIn("works_at", statements)
        self.assertIn("@0", statements)
        self.assertIn("NULL", statements)  # NaN weight is normalised.
        self.assertTrue(any("NaN/Inf" in warning for warning in result["warnings"]))

    def test_stale_version_is_not_published(self) -> None:
        repo = RecordingNebulaRepository()
        with self.assertRaises(NebulaRepositoryError) as ctx:
            repo.sync_graph(
                self._graph(),
                user_id="alice",
                session_id="session-1",
                graph_version=1,
                current_version_getter=lambda: 2,
            )
        self.assertEqual(ctx.exception.code, "STALE_DURING_SYNC")
        self.assertIsNone(repo.active)
        self.assertGreaterEqual(len(repo.deleted), 1)

    def test_partial_write_failure_keeps_previous_pointer(self) -> None:
        repo = RecordingNebulaRepository()
        repo.active = {
            "scope_key": scope_key("alice", "session-1"),
            "graph_key": "previous-graph-key",
            "graph_version": 0,
        }
        original_execute = repo._execute

        def fail_on_edges(query: str, *, use_space: bool = True):
            if query.startswith("INSERT EDGE"):
                raise NebulaRepositoryError("QUERY_FAILED", "injected edge failure")
            return original_execute(query, use_space=use_space)

        repo._execute = fail_on_edges  # type: ignore[method-assign]
        with self.assertRaises(NebulaRepositoryError):
            repo.sync_graph(
                self._graph(),
                user_id="alice",
                session_id="session-1",
                graph_version=1,
                current_version_getter=lambda: 1,
            )
        self.assertEqual(repo.active["graph_key"], "previous-graph-key")
        self.assertIn(
            graph_key_for(scope_key("alice", "session-1"), 1),
            repo.deleted,
        )

    def test_same_version_sync_is_idempotent(self) -> None:
        repo = RecordingNebulaRepository()
        first = repo.sync_graph(
            self._graph(),
            user_id="alice",
            session_id="session-1",
            graph_version=1,
            current_version_getter=lambda: 1,
        )
        second = repo.sync_graph(
            self._graph(),
            user_id="alice",
            session_id="session-1",
            graph_version=1,
            current_version_getter=lambda: 1,
        )
        self.assertEqual(first["graph_key"], second["graph_key"])
        self.assertEqual(repo.active["graph_key"], first["graph_key"])
        self.assertEqual(repo.deleted, [])

    def test_path_wrapper_ids_are_returned_in_order(self) -> None:
        vids = ["a" * 64, "b" * 64, "c" * 64]

        class Node:
            def __init__(self, value: str) -> None:
                self.value = value

            def get_id(self) -> str:
                return self.value

        class Path:
            def nodes(self):
                return [Node(value) for value in vids]

        class Value:
            def cast(self):
                return Path()

        self.assertEqual(NebulaGraphRepository._extract_path_vids(Value()), vids)

    def test_disabled_status_does_not_import_client(self) -> None:
        repo = NebulaGraphRepository(
            NebulaConfig(
                enabled=False,
                hosts=(("127.0.0.1", 9669),),
                user="root",
                password="nebula",
                space="igraph",
            )
        )
        status = repo.status()
        self.assertTrue(status["success"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["connected"])

    def test_enabled_with_missing_connection_config_is_isolated_error(self) -> None:
        with patch.dict(os.environ, {"NEBULA_ENABLED": "1"}, clear=True):
            repo = NebulaGraphRepository(NebulaConfig.from_env())
        status = repo.status()
        self.assertFalse(status["success"])
        self.assertFalse(status["configured"])
        self.assertEqual(status["code"], "NEBULA_UNAVAILABLE")


class NebulaServiceToolsTest(unittest.TestCase):
    def test_tools_are_exposed_and_sync_job_completes(self) -> None:
        repo = FakeServiceNebulaRepository()
        service = GraphDataService(nebula_repository=repo)
        tools = get_all_public_tools()
        names = {row["name"] for row in tools}
        self.assertTrue(
            {
                "nebula_status",
                "nebula_sync_graph",
                "nebula_get_node_info",
                "nebula_get_neighbors",
                "nebula_shortest_path",
            }.issubset(names)
        )

        sid = "nebula-tools"
        session = service.get_or_create(sid, user_id="alice")
        session._parse_edgelist("A B\nB C")
        job_manager = RecordingJobManager()
        service._job_manager = job_manager
        queued = asyncio.run(
            service.dispatch_tool(sid, "nebula_sync_graph", {}, user_id="alice")
        )
        self.assertTrue(queued["success"])
        self.assertTrue(queued["async"])
        self.assertEqual(queued["job_id"], "job-nebula-1")
        self.assertEqual(job_manager.submission[1], "nebula_sync_graph")

        sync_result = service._do_nebula_sync_job(
            session, session.get_graph(), session.graph_version, "alice"
        )
        self.assertTrue(sync_result["success"])

        status = service.do_nebula_status(sid, user_id="alice")
        self.assertTrue(status["synced"])

        node = service._dispatch_nebula_query(
            sid, "nebula_get_node_info", {"node_name": "A"}, user_id="alice"
        )
        self.assertEqual(node["node"]["node_name"], "A")

        neighbors = service._dispatch_nebula_query(
            sid,
            "nebula_get_neighbors",
            {"node_name": "A", "direction": "out"},
            user_id="alice",
        )
        self.assertEqual(neighbors["neighbors"][0]["node_name"], "B")

        path_result = service._dispatch_nebula_query(
            sid,
            "nebula_shortest_path",
            {"source": "A", "target": "C"},
            user_id="alice",
        )
        self.assertEqual(path_result["path"], ["A", "C"])


if __name__ == "__main__":
    unittest.main()
