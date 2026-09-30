from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# The legacy compatibility import loads deployment .env settings.
with patch.dict(os.environ):
    from backend.agent.igraph import IGraph


class _FakeMCPClient:
    def __init__(self) -> None:
        self.status_calls = 0
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "load_graph_classification_dataset":
            return {"success": True, "async": True, "job_id": "job-dataset"}
        if name != "get_job_status":
            raise AssertionError(name)
        self.status_calls += 1
        if self.status_calls == 1:
            return {
                "success": True,
                "job": {"status": "running", "detail": {"stage": "validate_graphs"}},
                "progress_updates": [
                    {
                        "kind": "graph_classification_dataset_load_progress",
                        "stage": "validate_graphs",
                        "progress": 0.5,
                        "message": "validating",
                    }
                ],
            }
        return {
            "success": True,
            "job": {
                "status": "done",
                "detail": {"stage": "complete", "progress": 1.0},
                "result": {
                    "success": True,
                    "dataset": {"dataset_id": "gds_hiv", "name": "hiv"},
                    "readiness": {"supported": True},
                },
            },
            "progress_updates": [],
        }


class _FakeLogger:
    def __init__(self) -> None:
        self.trace_events = []

    def log_trace_event(self, name: str, payload: dict) -> None:
        self.trace_events.append((name, payload))

    def set_graph_meta(self, **_kwargs) -> None:
        raise AssertionError("dataset load must not update single-graph state")


class GraphClassificationAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_dataset_job_is_polled_without_setting_has_graph(self) -> None:
        agent = object.__new__(IGraph)
        agent._mcp_client = _FakeMCPClient()
        agent._has_graph = False
        agent._graph_node_count = 0
        agent._graph_edge_count = 0
        agent._graph_is_directed = False
        agent._graph_has_weights = False
        agent.logger = _FakeLogger()
        progress = []

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            result_text = await agent._execute_tool_call(
                name="load_graph_classification_dataset",
                args={"filename": "hiv.pt", "dataset_profile": "hiv"},
                call_id="call-load-dataset",
                progress_callback=progress.append,
            )

        result = json.loads(result_text)
        self.assertTrue(result["success"])
        self.assertEqual(result["dataset"]["dataset_id"], "gds_hiv")
        self.assertFalse(agent._has_graph)
        self.assertEqual(agent._graph_node_count, 0)
        self.assertEqual(
            [event["kind"] for event in progress],
            [
                "graph_classification_dataset_load_progress",
                "graph_classification_dataset_load_done",
            ],
        )
        self.assertEqual(
            [name for name, _args in agent._mcp_client.calls],
            [
                "load_graph_classification_dataset",
                "get_job_status",
                "get_job_status",
            ],
        )


if __name__ == "__main__":
    unittest.main()
