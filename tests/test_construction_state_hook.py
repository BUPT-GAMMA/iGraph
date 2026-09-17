from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_runtime.construction_state import construction_tool_hook


def context():
    return SimpleNamespace(session_state={
        "project_id": "project-a", "active_dataset_id": "dataset-a",
        "data_state": {"active_dataset_id": "dataset-a", "construction_status": "ready",
                       "construction_revision": 1, "source_format": "csv",
                       "available_constructed_datasets": [
                           {"dataset_id": "dataset-b", "revision": 2, "source_format": "txt"},
                       ]},
    })


class ConstructionStateHookTest(unittest.IsolatedAsyncioTestCase):
    async def test_successful_load_updates_persisted_selection_from_engine_result(self):
        for wrapper in (
            lambda payload: payload,
            lambda payload: json.dumps(payload),
            lambda payload: {"content": json.dumps(payload), "metadata": None},
            lambda payload: {"content": [{"type": "text", "text": json.dumps(payload)}]},
            lambda payload: [{"type": "text", "text": json.dumps(payload)}],
        ):
            run = context()
            payload = wrapper({"success": True, "construction_dataset_id": "dataset-b", "construction_revision": 3})
            call = AsyncMock(return_value=payload)
            result = await construction_tool_hook("load_constructed_graph", call, {"dataset_id": "dataset-b"}, run)
            self.assertIs(result, payload)
            call.assert_awaited_once_with(dataset_id="dataset-b")
            self.assertEqual(run.session_state["active_dataset_id"], "dataset-b")
            self.assertEqual(run.session_state["data_state"]["active_dataset_id"], "dataset-b")
            self.assertEqual(run.session_state["data_state"]["construction_revision"], 3)
            self.assertEqual(run.session_state["data_state"]["source_format"], "txt")
            self.assertEqual(run.session_state["project_id"], "project-a")

    async def test_other_project_dataset_cannot_be_loaded_previewed_or_committed(self):
        for tool in ("get_graph_construction_status", "get_constructed_graph", "load_constructed_graph",
                     "preview_graph_optimization", "commit_graph_optimization"):
            run = context()
            before = copy.deepcopy(run.session_state)
            call = AsyncMock()
            result = await construction_tool_hook(tool, call, {"dataset_id": "dataset-other-project"}, run)
            self.assertEqual(json.loads(result)["code"], "DATASET_SCOPE_REQUIRED")
            self.assertEqual(run.session_state, before)
            call.assert_not_awaited()

    async def test_unknown_legacy_session_scope_requires_frontend_selection(self):
        for run in (None, SimpleNamespace(session_state={}), SimpleNamespace(session_state={"active_dataset_id": "dataset-a"})):
            call = AsyncMock()
            result = await construction_tool_hook("load_constructed_graph", call, {"dataset_id": "dataset-a"}, run)
            self.assertFalse(json.loads(result)["success"])
            call.assert_not_awaited()

    async def test_status_allows_current_scope_unbuilt_sources_on_followup_turns(self):
        for field in ("available_construction_sources", "construction_sources"):
            run = context()
            run.session_state["data_state"][field] = [{"dataset_id": "unbuilt"}]
            before = copy.deepcopy(run.session_state)
            call = AsyncMock(return_value={"status": "not_built", "revision": 0})
            result = await construction_tool_hook("get_graph_construction_status", call, {"dataset_id": "unbuilt"}, run)
            self.assertEqual(result["status"], "not_built")
            call.assert_awaited_once()
            call.reset_mock()
            result = await construction_tool_hook("get_constructed_graph", call, {"dataset_id": "unbuilt"}, run)
            self.assertEqual(json.loads(result)["code"], "DATASET_SCOPE_REQUIRED")
            call.assert_not_awaited()
            self.assertEqual(run.session_state, before)

    async def test_read_tools_require_context_and_preserve_selection(self):
        for tool in ("get_graph_construction_status", "get_constructed_graph"):
            call = AsyncMock(return_value={"success": True})
            self.assertEqual(json.loads(await construction_tool_hook(tool, call, {"dataset_id": "dataset-a"}, None))["code"],
                             "DATASET_SCOPE_REQUIRED")
            call.assert_not_awaited()
            run = context()
            before = copy.deepcopy(run.session_state)
            await construction_tool_hook(tool, call, {"dataset_id": "dataset-b"}, run)
            call.assert_awaited_once()
            self.assertEqual(run.session_state, before)

    async def test_failed_load_keeps_previous_selection_and_revision(self):
        run = context()
        before = copy.deepcopy(run.session_state)
        call = AsyncMock(return_value=json.dumps({"success": False, "code": "graph_not_ready"}))
        await construction_tool_hook("load_constructed_graph", call, {"dataset_id": "dataset-b"}, run)
        self.assertEqual(run.session_state, before)

    async def test_preview_and_commit_do_not_switch_current_session_graph(self):
        for tool in ("preview_graph_optimization", "commit_graph_optimization"):
            run = context()
            before = copy.deepcopy(run.session_state)
            call = AsyncMock(return_value=json.dumps({"success": True, "dataset_id": "dataset-b", "revision": 3}))
            await construction_tool_hook(tool, call, {"dataset_id": "dataset-b", "base_revision": 2, "operations": []}, run)
            self.assertEqual(run.session_state, before)
            call.assert_awaited_once()

    async def test_active_dataset_remains_allowed_without_a_catalog_row(self):
        run = context()
        call = AsyncMock(return_value={"success": True, "construction_revision": 4})
        await construction_tool_hook("load_constructed_graph", call, {"dataset_id": "dataset-a"}, run)
        self.assertEqual(run.session_state["data_state"]["construction_revision"], 4)
        self.assertEqual(run.session_state["data_state"]["source_format"], "csv")

    async def test_readonly_and_existing_analysis_tools_keep_existing_identity_boundary(self):
        for tool in ("get_graph_info", "train"):
            call = AsyncMock(return_value="original-result")
            self.assertEqual(await construction_tool_hook(tool, call, {"dataset_id": "other"}, None), "original-result")
            call.assert_awaited_once_with(dataset_id="other")


if __name__ == "__main__":
    unittest.main()
