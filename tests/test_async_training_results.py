from __future__ import annotations

import json
import unittest

import pytest

from backend.app import _messages_from_runs
from backend.event_mapper import map_agentos_event, map_product_events


def _completed_tool_event(name: str, result: dict, call_id: str = "call-status") -> dict:
    return {
        "event": "ToolCallCompleted",
        "run_id": "run-1",
        "tool": {
            "tool_name": name,
            "tool_call_id": call_id,
            "result": json.dumps(result),
        },
    }


@pytest.mark.parametrize("source_tool", ["prepare_large_graph", "run_large_graph_algorithm"])
@pytest.mark.parametrize(
    ("status", "expected_kind", "expected_progress"),
    [
        ("done", "job_done", 1),
        ("failed", "job_failed", 0.4),
        ("cancelled", "job_cancelled", 0.4),
    ],
)
def test_large_graph_terminal_status_emits_terminal_progress(
    source_tool: str,
    status: str,
    expected_kind: str,
    expected_progress: float,
) -> None:
    event = _completed_tool_event(
        "get_job_status",
        {
            "success": True,
            "job": {
                "job_id": "large-job-1",
                "tool_name": source_tool,
                "status": status,
                "progress": 0.4,
                "message": f"job {status}",
                "detail": {"kind": "partition_scan", "partition": 3},
            },
            "progress_updates": [],
        },
    )

    events = map_product_events(event)

    assert len(events) == 1
    assert events[0]["type"] == "tool_progress"
    assert events[0]["tool"] == source_tool
    assert events[0]["call_id"] == "large-job-1"
    assert events[0]["progress"] == {
        "kind": expected_kind,
        "partition": 3,
        "job_id": "large-job-1",
        "status": status,
        "progress": expected_progress,
        "message": f"job {status}",
    }


class AsyncTrainingEventTest(unittest.TestCase):
    def test_queued_train_result_stays_pending(self) -> None:
        event = _completed_tool_event(
            "train",
            {"success": True, "async": True, "job_id": "job-1", "status": "queued"},
            "call-train",
        )

        mapped = map_agentos_event(event)

        self.assertEqual(mapped["type"], "tool_result")
        self.assertFalse(mapped["done"])

    def test_completed_status_emits_final_train_metrics_and_history(self) -> None:
        history = [
            {"kind": "train_epoch", "epoch": 1, "train_loss": 1.2, "val_acc": 0.4},
            {"kind": "train_epoch", "epoch": 2, "train_loss": 0.8, "val_acc": 0.6},
        ]
        event = _completed_tool_event(
            "get_job_status",
            {
                "success": True,
                "job": {
                    "job_id": "job-1",
                    "tool_name": "train",
                    "status": "done",
                    "progress": 1,
                    "detail": history[-1],
                    "result": {
                        "success": True,
                        "model_id": "model-1",
                        "metrics": {"train_loss": 0.8, "val_acc": 0.6, "trained_epochs": 2},
                    },
                },
                "progress_updates": history,
            },
        )

        events = map_product_events(event)

        final = events[-1]
        self.assertEqual(final["type"], "tool_result")
        self.assertEqual(final["tool"], "train")
        self.assertEqual(final["call_id"], "job-1")
        self.assertEqual(final["result"]["metrics"]["val_acc"], 0.6)
        self.assertEqual(final["result"]["history"], history)


class RestoredTrainingMessageTest(unittest.TestCase):
    def test_job_status_is_merged_into_original_train_card(self) -> None:
        runs = [
            {
                "run_id": "run-train",
                "status": "completed",
                "run_input": "train a model",
                "tools": [{
                    "tool_name": "train",
                    "tool_call_id": "call-train",
                    "tool_args": {"task": "node_classification"},
                    "result": json.dumps({
                        "success": True,
                        "async": True,
                        "job_id": "job-1",
                        "status": "queued",
                    }),
                }],
            },
            {
                "run_id": "run-status",
                "status": "completed",
                "tools": [{
                    "tool_name": "get_job_status",
                    "tool_call_id": "call-status",
                    "tool_args": {"job_id": "job-1"},
                    "result": json.dumps({
                        "success": True,
                        "job": {
                            "job_id": "job-1",
                            "tool_name": "train",
                            "status": "done",
                            "progress": 1,
                            "message": "completed",
                            "detail": {"kind": "train_epoch", "epoch": 2, "progress": 1},
                            "result": {
                                "success": True,
                                "model_id": "model-1",
                                "metrics": {"train_loss": 0.8, "val_acc": 0.6, "trained_epochs": 2},
                            },
                        },
                        "progress_updates": [
                            {"kind": "train_epoch", "epoch": 1, "train_loss": 1.2, "val_acc": 0.4},
                            {"kind": "train_epoch", "epoch": 2, "train_loss": 0.8, "val_acc": 0.6},
                        ],
                    }),
                }],
            },
        ]

        messages = _messages_from_runs(runs)
        train_card = next(
            tool
            for message in messages
            for tool in message.get("tools", [])
            if tool.get("tool") == "train"
        )

        self.assertTrue(train_card["done"])
        self.assertEqual(train_card["result"]["metrics"]["val_acc"], 0.6)
        self.assertEqual([row["epoch"] for row in train_card["history"]], [1, 2])

    def test_terminal_job_detail_is_kept_when_progress_queue_is_empty(self) -> None:
        runs = [{
            "run_id": "run-train",
            "status": "completed",
            "tools": [
                {
                    "tool_name": "train",
                    "tool_call_id": "call-train",
                    "result": json.dumps({"success": True, "async": True, "job_id": "job-1"}),
                },
                {
                    "tool_name": "get_job_status",
                    "tool_call_id": "call-status",
                    "result": json.dumps({
                        "success": True,
                        "job": {
                            "job_id": "job-1",
                            "tool_name": "train",
                            "status": "done",
                            "progress": 1,
                            "detail": {"kind": "train_epoch", "epoch": 200, "epochs": 200, "val_acc": 0.773},
                            "result": {"success": True, "metrics": {"trained_epochs": 200, "val_acc": 0.773}},
                        },
                        "progress_updates": [],
                    }),
                },
            ],
        }]

        messages = _messages_from_runs(runs)
        train_card = next(tool for message in messages for tool in message.get("tools", []) if tool.get("tool") == "train")
        self.assertTrue(train_card["done"])
        self.assertEqual(train_card["history"][-1]["epoch"], 200)


if __name__ == "__main__":
    unittest.main()
