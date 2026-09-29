from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

# The legacy compatibility import loads deployment .env settings.
with patch.dict(os.environ):
    from backend.agent.igraph import IGraph


class _FakeLargeGraphMCP:
    def __init__(
        self,
        tool_name: str,
        *,
        terminal_status: str = "done",
        status_sequence: list[str] | None = None,
        pending_updates_sequence: list[list[dict]] | None = None,
        result_payload: dict | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.terminal_status = terminal_status
        self.status_sequence = list(status_sequence or [])
        self.status_index = 0
        self.pending_updates_sequence = list(pending_updates_sequence or [])
        self.result_payload = result_payload
        self.current_status = terminal_status
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == self.tool_name:
            return {
                "success": True,
                "async": True,
                "job_id": "lg_compute_123",
                "status": "queued",
            }
        if name == "get_job_status":
            if self.status_sequence:
                sequence_index = min(self.status_index, len(self.status_sequence) - 1)
                self.current_status = self.status_sequence[sequence_index]
                self.status_index += 1
            else:
                self.current_status = self.terminal_status
            status = self.current_status
            poll_index = max(0, self.status_index - 1)
            pending_updates = (
                self.pending_updates_sequence[
                    min(poll_index, len(self.pending_updates_sequence) - 1)
                ]
                if self.pending_updates_sequence
                else []
            )
            return {
                "success": True,
                "job": {
                    "job_id": "lg_compute_123",
                    "tool_name": self.tool_name,
                    "status": status,
                    "stage": "community_scan" if status == "running" else "artifact",
                    "progress": 1.0 if status == "done" else 0.4,
                    "message": "scanning communities" if status == "running" else status,
                    "detail": {
                        "partition": 2,
                        "processed_nodes": 128,
                    },
                },
                "progress_updates": pending_updates,
            }
        if name == "get_job_result":
            if self.current_status != "done":
                raise AssertionError("failed/cancelled jobs must not fetch results")
            if self.result_payload is not None:
                return self.result_payload
            return {
                "success": True,
                "result": {
                    "job_id": "lg_compute_123",
                    "status": "done",
                    "summary": {"algorithm": "degree", "result_count": 3},
                    "preview": [{"node": "A", "score": 2}],
                    "artifact_id": "artifact_0123456789abcdef",
                    "representation": "partitioned_artifact",
                },
            }
        raise AssertionError(name)


class _FakeLogger:
    def __init__(self) -> None:
        self.trace_events: list[tuple[str, dict]] = []

    def log_trace_event(self, name: str, payload: dict) -> None:
        self.trace_events.append((name, payload))


class LargeGraphAgentTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _agent(client: _FakeLargeGraphMCP) -> IGraph:
        agent = object.__new__(IGraph)
        agent._mcp_client = client
        agent._has_graph = False
        agent._graph_node_count = 0
        agent._graph_edge_count = 0
        agent._graph_is_directed = False
        agent._graph_has_weights = False
        agent.logger = _FakeLogger()
        return agent

    async def test_completed_large_job_fetches_bounded_result_separately(self) -> None:
        client = _FakeLargeGraphMCP("run_large_graph_algorithm")
        agent = self._agent(client)
        progress: list[dict] = []

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            result_text = await agent._execute_tool_call(
                name="run_large_graph_algorithm",
                args={"graph_version": 2, "algorithm": "degree"},
                call_id="call-large",
                progress_callback=progress.append,
            )

        result = json.loads(result_text)
        self.assertTrue(result["success"])
        self.assertEqual(result["artifact_id"], "artifact_0123456789abcdef")
        self.assertNotIn("path", result)
        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "run_large_graph_algorithm",
                "get_job_status",
                "get_job_result",
            ],
        )
        self.assertEqual(progress[-1]["kind"], "large_graph_compute_done")

    async def test_failed_large_job_emits_terminal_progress_without_result_fetch(
        self,
    ) -> None:
        client = _FakeLargeGraphMCP(
            "prepare_large_graph", terminal_status="failed"
        )
        agent = self._agent(client)
        progress: list[dict] = []

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            result_text = await agent._execute_tool_call(
                name="prepare_large_graph",
                args={"filename": "graph.txt"},
                call_id="call-prepare",
                progress_callback=progress.append,
            )

        result = json.loads(result_text)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            [name for name, _ in client.calls],
            ["prepare_large_graph", "get_job_status"],
        )
        self.assertEqual(progress[-1]["kind"], "job_failed")

    async def test_legacy_algorithm_auto_routed_large_job_is_polled_with_deduped_progress(
        self,
    ) -> None:
        client = _FakeLargeGraphMCP(
            "community_detection",
            status_sequence=["running", "running", "done"],
        )
        agent = self._agent(client)
        progress: list[dict] = []

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            result_text = await agent._execute_tool_call(
                name="community_detection",
                args={"algorithm": "louvain"},
                call_id="call-legacy-large",
                progress_callback=progress.append,
            )

        result = json.loads(result_text)
        self.assertTrue(result["success"])
        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "community_detection",
                "get_job_status",
                "get_job_status",
                "get_job_status",
                "get_job_result",
            ],
        )
        intermediate = [item for item in progress if item.get("kind") == "job_progress"]
        self.assertEqual(len(intermediate), 1)
        self.assertEqual(intermediate[0]["stage"], "community_scan")
        self.assertEqual(intermediate[0]["partition"], 2)
        self.assertEqual(intermediate[0]["processed_nodes"], 128)
        self.assertEqual(intermediate[0]["progress"], 0.4)
        self.assertEqual(progress[-1]["kind"], "large_graph_compute_done")

    async def test_queued_progress_checkpoint_is_not_synthesized_again(self) -> None:
        queued = {
            "kind": "job_progress",
            "stage": "community_scan",
            "progress": 0.4,
            "message": "scanning communities",
            "partition": 2,
            "processed_nodes": 128,
        }
        client = _FakeLargeGraphMCP(
            "community_detection",
            status_sequence=["running", "running", "done"],
            pending_updates_sequence=[[queued], [], []],
        )
        agent = self._agent(client)
        progress: list[dict] = []

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            await agent._execute_tool_call(
                name="community_detection",
                args={"algorithm": "louvain"},
                call_id="call-progress-dedupe",
                progress_callback=progress.append,
            )

        intermediate = [item for item in progress if item.get("kind") == "job_progress"]
        self.assertEqual(intermediate, [queued])

    async def test_completed_large_job_rejects_missing_business_result(self) -> None:
        client = _FakeLargeGraphMCP(
            "run_large_graph_algorithm",
            result_payload={"success": False, "result": None},
        )
        agent = self._agent(client)

        with patch("backend.agent.igraph.asyncio.sleep", new=AsyncMock()):
            result_text = await agent._execute_tool_call(
                name="run_large_graph_algorithm",
                args={"graph_version": 2, "algorithm": "degree"},
                call_id="call-missing-result",
                progress_callback=None,
            )

        result = json.loads(result_text)
        self.assertFalse(result["success"])
        self.assertEqual(result["job_id"], "lg_compute_123")
        self.assertIn("结果查询失败", result["error"])


if __name__ == "__main__":
    unittest.main()
