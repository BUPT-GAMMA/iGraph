"""Opt-in acceptance test for a real NebulaGraph 3.8.x cluster.

Run with RUN_NEBULA_INTEGRATION=1 after configuring the NEBULA_* variables.
The test uses a UUID-scoped session and removes only the data it created.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

import numpy as np

from backend.runtime.dotenv_compat import load_dotenv_compat
from graph_data_service.graph_data import GraphData
from graph_data_service.storage.nebula_backend import (
    NebulaConfig,
    NebulaGraphRepository,
    NebulaRepositoryError,
    graph_key_for,
    scope_key,
)


@unittest.skipUnless(
    os.getenv("RUN_NEBULA_INTEGRATION", "").strip().lower() in {"1", "true", "yes"},
    "set RUN_NEBULA_INTEGRATION=1 to run remote NebulaGraph acceptance tests",
)
class NebulaGraphIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        # Load deployment settings only after the explicit opt-in has been
        # checked, and restore them even if setup fails or skips the test.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        load_dotenv_compat()
        self.config = NebulaConfig.from_env()
        if not self.config.enabled:
            self.skipTest("NEBULA_ENABLED is not enabled")
        self.repo = NebulaGraphRepository(self.config)
        self.user_id = f"integration-{uuid.uuid4().hex}"
        self.session_id = f"session-{uuid.uuid4().hex}"
        self.scope = scope_key(self.user_id, self.session_id)
        self.graph = GraphData(
            num_nodes=6,
            edge_index=np.asarray(
                [[0, 1, 2, 0, 4, 3, 1], [1, 2, 3, 4, 3, 5, 4]],
                dtype=np.int64,
            ),
            id2node={0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F"},
            node2id={"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5},
            node_type=np.asarray(["person", "person", "person", "place", "place", "place"]),
            edge_type=np.asarray(["link"] * 7),
            edge_weight=np.asarray([1, 1, 1, 1, 1, 1, 1], dtype=np.float32),
            directed=True,
            name="nebula-integration",
        )

    def tearDown(self) -> None:
        for version in (1, 2):
            try:
                self.repo._delete_graph_key(graph_key_for(self.scope, version))
            except Exception:
                pass
        try:
            self.repo._execute(f'DELETE VERTEX "{self.scope}" WITH EDGE')
        except Exception:
            pass
        self.repo.close()

    def test_sync_queries_staleness_and_user_isolation(self) -> None:
        result = self.repo.sync_graph(
            self.graph,
            user_id=self.user_id,
            session_id=self.session_id,
            graph_version=1,
            current_version_getter=lambda: 1,
        )
        self.assertEqual(result["node_count"], 6)
        self.assertEqual(result["edge_count"], 7)

        status = self.repo.session_status(self.user_id, self.session_id, 1)
        self.assertTrue(status["connected"])
        self.assertTrue(status["schema_ready"])
        self.assertTrue(status["synced"])
        self.assertEqual(status["synced_node_count"], 6)
        self.assertEqual(status["synced_edge_count"], 7)

        node = self.repo.get_node_info(
            self.graph,
            user_id=self.user_id,
            session_id=self.session_id,
            graph_version=1,
            node_name="A",
        )
        self.assertEqual(node["node"]["node_name"], "A")

        neighbors = self.repo.get_neighbors(
            self.graph,
            user_id=self.user_id,
            session_id=self.session_id,
            graph_version=1,
            node_name="A",
        )
        self.assertEqual({row["node_name"] for row in neighbors["neighbors"]}, {"B", "E"})

        path = self.repo.shortest_path(
            self.graph,
            user_id=self.user_id,
            session_id=self.session_id,
            graph_version=1,
            source="A",
            target="F",
        )
        self.assertTrue(path["found"])
        self.assertEqual(path["path"][0], "A")
        self.assertEqual(path["path"][-1], "F")

        with self.assertRaises(NebulaRepositoryError) as stale:
            self.repo.get_node_info(
                self.graph,
                user_id=self.user_id,
                session_id=self.session_id,
                graph_version=2,
                node_name="A",
            )
        self.assertEqual(stale.exception.code, "STALE_SYNC")

        with self.assertRaises(NebulaRepositoryError) as isolated:
            self.repo.get_node_info(
                self.graph,
                user_id=f"other-{self.user_id}",
                session_id=self.session_id,
                graph_version=1,
                node_name="A",
            )
        self.assertEqual(isolated.exception.code, "NOT_SYNCED")

        self.repo.sync_graph(
            self.graph,
            user_id=self.user_id,
            session_id=self.session_id,
            graph_version=2,
            current_version_getter=lambda: 2,
        )
        self.assertTrue(self.repo.session_status(self.user_id, self.session_id, 2)["synced"])


if __name__ == "__main__":
    unittest.main()
