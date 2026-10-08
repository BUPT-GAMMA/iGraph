from __future__ import annotations

import asyncio
import json

import pytest

from agno.exceptions import StopAgentRun
from agno.run import RunContext

import agent_runtime.loop_guard as loop_guard

ToolLoopGuard = loop_guard.ToolLoopGuard
progress_guard_hook = loop_guard.progress_guard_hook


def test_guard_stops_identical_tool_loop():
    guard = ToolLoopGuard(max_same_call=3)
    assert guard.before_call("workspace_bash", {"command": "cat Uploaded/large.txt"}) is None
    assert guard.before_call("workspace_bash", {"command": "cat Uploaded/large.txt"}) is None
    assert guard.before_call("workspace_bash", {"command": "cat Uploaded/large.txt"}) is None
    assert "重复调用" in (guard.before_call("workspace_bash", {"command": "cat Uploaded/large.txt"}) or "")


def test_guard_fingerprint_normalizes_argument_key_order():
    assert hasattr(loop_guard, "tool_call_fingerprint")
    assert loop_guard.tool_call_fingerprint(
        "workspace_publish", {"path": "Ephemeral/a", "name": "a"}
    ) == loop_guard.tool_call_fingerprint(
        "workspace_publish", {"name": "a", "path": "Ephemeral/a"}
    )


def test_guard_different_arguments_can_continue():
    guard = ToolLoopGuard(max_same_call=1)
    assert guard.before_call("workspace_bash", {"command": "head -n 10 Uploaded/a"}) is None
    assert guard.before_call("workspace_bash", {"command": "tail -n 10 Uploaded/a"}) is None


def test_guard_job_polling_is_not_duplicate_loop():
    guard = ToolLoopGuard(max_same_call=1)
    assert guard.before_call("get_job_status", {"job_id": "job-1"}) is None
    assert guard.before_call("get_job_status", {"job_id": "job-1"}) is None


def test_job_polling_allows_active_retries_but_blocks_after_terminal_result():
    guard = ToolLoopGuard(max_same_call=1)
    args = {"job_id": "job-1"}
    queued = {"success": True, "job": {"job_id": "job-1", "status": "queued"}}
    done = {
        "success": True,
        "job": {"job_id": "job-1", "status": "done", "result": {"artifact": "out.csv"}},
    }

    assert guard.before_call("get_job_status", args) is None
    guard.after_result(True, tool_name="get_job_status", arguments=args, result=queued)
    assert guard.before_call("get_job_status", args) is None
    guard.after_result(True, tool_name="get_job_status", arguments=args, result=queued)
    assert guard.before_call("get_job_status", args) is None
    guard.after_result(True, tool_name="get_job_status", arguments=args, result=done)
    assert "重复" in (guard.before_call("get_job_status", args) or "")


def test_job_status_and_artifact_changes_are_real_progress():
    args = {"job_id": "job-1"}
    queued = {"success": True, "job": {"job_id": "job-1", "status": "queued"}}
    running = {"success": True, "job": {"job_id": "job-1", "status": "running"}}
    done = {
        "success": True,
        "job": {"job_id": "job-1", "status": "done", "result": {"artifact": "out.csv"}},
    }
    done_with_new_artifact = {
        "success": True,
        "job": {"job_id": "job-1", "status": "done", "result": {"artifact": "out-v2.csv"}},
    }

    assert loop_guard.has_real_progress("get_job_status", args, queued, None)
    assert loop_guard.has_real_progress("get_job_status", args, running, queued)
    assert loop_guard.has_real_progress("get_job_status", args, done, running)
    assert loop_guard.has_real_progress("get_job_status", args, done_with_new_artifact, done)


def test_default_guard_allows_one_hundred_calls_then_stops():
    guard = ToolLoopGuard()
    for index in range(100):
        assert guard.before_call("get_job_status", {"job_id": f"job-{index}"}) is None
    assert "上限" in (guard.before_call("get_job_status", {"job_id": "job-101"}) or "")


def test_guard_stops_after_call_budget():
    guard = ToolLoopGuard(max_calls=1)
    assert guard.before_call("get_graph_info", {}) is None
    assert "上限" in (guard.before_call("workspace_list", {}) or "")


def test_guard_stops_consecutive_failures():
    guard = ToolLoopGuard(max_failures=2)
    assert guard.after_result(False) is None
    assert "连续失败" in (guard.after_result(False) or "")
    assert "连续失败" in (guard.before_call("workspace_list", {}) or "")


def test_success_resets_consecutive_failures():
    guard = ToolLoopGuard(max_failures=2)
    assert guard.after_result(False) is None
    assert guard.after_result(True) is None
    assert guard.after_result(False) is None


def test_different_arguments_without_changed_evidence_are_not_progress():
    assert loop_guard.has_real_progress(
        "workspace_bash",
        {"command": "printf one"},
        {"success": True, "exit_code": 0, "output": ""},
        None,
    ) is True
    assert loop_guard.has_real_progress(
        "workspace_bash",
        {"command": "printf two"},
        {"success": True, "exit_code": 0, "output": ""},
        {"success": True, "exit_code": 0, "output": ""},
    ) is False

    guard = ToolLoopGuard(max_same_call=1)
    evidence = {"success": True, "exit_code": 0, "output": ""}
    first_args = {"command": "printf one"}
    second_args = {"command": "printf two"}
    assert guard.before_call("workspace_bash", first_args) is None
    guard.after_result(True, tool_name="workspace_bash", arguments=first_args, result=evidence)
    assert guard.before_call("workspace_bash", second_args) is None
    guard.after_result(True, tool_name="workspace_bash", arguments=second_args, result=evidence)
    assert "进展" in (guard.before_call("workspace_bash", {"command": "printf three"}) or "")


def test_workspace_bash_chunk_changes_count_as_progress():
    first = {"success": True, "exit_code": 0, "output": "abc"}
    second = {"success": True, "exit_code": 0, "output": "def"}
    assert loop_guard.has_real_progress("workspace_bash", {"command": "head -n 1 Uploaded/a"}, first, None)
    assert loop_guard.has_real_progress(
        "workspace_bash", {"command": "tail -n 1 Uploaded/a"}, second, first
    )


def test_graph_state_changes_count_as_progress():
    before = {"success": True, "graph_version": 4, "nodes": 2}
    after = {"success": True, "graph_version": 5, "nodes": 3}
    assert loop_guard.has_real_progress("get_graph_info", {}, after, before)


def test_graph_mutation_and_workspace_bash_are_serialized_per_run():
    active = 0
    max_active = 0
    run_context = RunContext(run_id="run-serial", session_id="session")

    async def invoke(name: str, arguments: dict[str, str]):
        nonlocal active, max_active

        async def tool(**_):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {"success": True, "output": arguments.get("command") or arguments.get("edge_text")}

        return await loop_guard.progress_guard_hook(
            run_context=run_context,
            name=name,
            args=arguments,
            func=tool,
        )

    async def run_batch():
        return await asyncio.gather(
            invoke("load_graph", {"edge_text": "a,b"}),
            invoke("workspace_bash", {"command": "printf file"}),
            invoke("workspace_publish", {"path": "Ephemeral/file.txt"}),
        )

    results = asyncio.run(run_batch())
    assert all(result["success"] for result in results)
    assert max_active == 1


def test_guard_state_isolated_between_run_contexts_and_not_session_state():
    async def invoke(run_context):
        async def tool(**_):
            return {"success": True}

        return await progress_guard_hook(
            run_context=run_context,
            name="workspace_bash",
            args={"command": "cat Uploaded/a"},
            func=tool,
        )

    first = RunContext(run_id="run-1", session_id="session", session_state={"project_id": "p"})
    second = RunContext(run_id="run-2", session_id="session", session_state={"project_id": "p"})
    first._igraph_tool_loop_guard = ToolLoopGuard(max_same_call=1)
    second._igraph_tool_loop_guard = ToolLoopGuard(max_same_call=1)
    assert asyncio.run(invoke(first))["success"] is True
    with pytest.raises(StopAgentRun) as first_block:
        asyncio.run(invoke(first))
    assert json.loads(str(first_block.value))["terminal"] is True
    assert asyncio.run(invoke(second))["success"] is True
    assert "_igraph_tool_loop_guard" not in first.session_state
    assert "_igraph_tool_loop_guard" not in second.session_state


def test_hook_raises_structured_failure_and_stops_agentos_run_when_guard_blocks_next_call():
    async def invoke(run_context):
        async def tool(**_):
            return {"success": True}

        return await progress_guard_hook(
            run_context=run_context,
            name="workspace_bash",
            args={"command": "cat Uploaded/a"},
            func=tool,
        )

    run_context = RunContext(run_id="run-1", session_id="session")
    run_context._igraph_tool_loop_guard = ToolLoopGuard(max_same_call=1)
    assert asyncio.run(invoke(run_context))["success"] is True
    with pytest.raises(StopAgentRun) as blocked:
        asyncio.run(invoke(run_context))
    failure = json.loads(str(blocked.value))
    assert failure["success"] is False
    assert failure["terminal"] is True
    assert failure["error"]["code"] == "IGRAPH_TOOL_LOOP_GUARD"


def test_bash_progress_ignores_transport_metadata_but_tracks_artifacts():
    before = {"success": True, "exit_code": 0, "output": "", "sandbox_mode": "bwrap", "artifacts": []}
    after = {**before, "sandbox_warning": "diagnostic only"}
    assert not loop_guard.has_real_progress("workspace_bash", {"command": "true"}, after, before)
    after["artifacts"] = [{"path": "Ephemeral/result.csv", "checksum": "new"}]
    assert loop_guard.has_real_progress("workspace_bash", {"command": "true"}, after, before)


def _complete(guard, name, args, result, success=True):
    assert guard.before_call(name, args) is None
    assert guard.after_result(success, tool_name=name, arguments=args, result=result) is None


def test_changed_results_allow_repeated_query_until_total_budget():
    guard = ToolLoopGuard(max_calls=8)
    for version in range(8):
        _complete(guard, "get_graph_info", {}, {"graph_version": version})
    assert "上限" in guard.before_call("get_graph_info", {})


def test_successful_graph_changes_allow_unchanged_summary_to_be_checked_again():
    guard = ToolLoopGuard()
    for version in range(8):
        _complete(guard, "load_graph", {"edge_text": str(version)}, {"success": True, "graph_version": version})
        _complete(guard, "get_graph_info", {}, {"success": True, "nodes": 2, "edges": 1})
    assert guard.calls == 16
    assert not guard.stopped


@pytest.mark.parametrize("writer,success,result", [
    ("load_graph", False, {"success": False, "graph_version": 99}),
    ("workspace_bash", True, {"success": True, "output": "different"}),
])
def test_failed_or_unrelated_changes_do_not_reset_query_limit(writer, success, result):
    guard = ToolLoopGuard()
    for _ in range(3):
        _complete(guard, "get_graph_info", {}, {"nodes": 2})
    _complete(guard, writer, {}, result, success)
    assert "重复" in guard.before_call("get_graph_info", {})


def test_noop_graph_writes_do_not_allow_endless_queries():
    guard = ToolLoopGuard()
    for i in range(3):
        _complete(guard, "load_graph", {"edge_text": str(i)}, {"success": True, "graph_version": 1})
        _complete(guard, "get_graph_info", {}, {"nodes": 2})
    _complete(guard, "load_graph", {"edge_text": "next"}, {"success": True, "graph_version": 1})
    assert "重复" in guard.before_call("get_graph_info", {})


def test_alternating_queries_do_not_reset_each_others_fingerprint():
    guard = ToolLoopGuard()
    for _ in range(3):
        for node in ("a", "b"):
            _complete(guard, "get_node_info", {"node": node}, {"node": node})
    assert "重复" in guard.before_call("get_node_info", {"node": "a"})


def test_progress_then_stagnation_still_stops():
    guard = ToolLoopGuard()
    for version in range(5):
        _complete(guard, "get_graph_info", {}, {"graph_version": version})
    for _ in range(2):
        _complete(guard, "get_graph_info", {}, {"graph_version": 4})
    assert "重复" in guard.before_call("get_graph_info", {})


def test_resource_invalidation_does_not_reset_writer_or_total_budget():
    guard = ToolLoopGuard(max_calls=6)
    for i in range(3):
        _complete(guard, "load_graph", {}, {"graph_version": i})
        _complete(guard, "get_graph_info", {}, {"nodes": 2})
    assert guard.calls == 6
    assert "上限" in guard.before_call("load_graph", {})


def test_progress_in_late_result_cannot_revive_stopped_guard():
    guard = ToolLoopGuard(max_same_call=1)
    _complete(guard, "get_graph_info", {}, {"graph_version": 1})
    assert guard.before_call("get_graph_info", {}) is not None
    guard.after_result(True, tool_name="load_graph", arguments={}, result={"graph_version": 2})
    assert guard._last_block_reason == "duplicate_call"
    assert guard.before_call("get_graph_info", {}) is not None


def test_native_hook_allows_graph_change_and_recheck_sequence():
    async def scenario():
        context = RunContext(run_id="progress", session_id="session")
        for version in range(6):
            for name, args in (("load_graph", {"edge_text": str(version)}), ("get_graph_info", {})):
                async def tool(**_):
                    return {"success": True, "graph_version": version}
                result = await progress_guard_hook(run_context=context, name=name, args=args, func=tool)
                assert result["graph_version"] == version
        assert loop_guard._guard_for_run(context).calls == 12
    asyncio.run(scenario())


@pytest.mark.parametrize("dataset,allowed", [("a", True), ("b", False)])
def test_only_matching_dataset_resource_invalidates_read_counts(dataset, allowed):
    guard = ToolLoopGuard()
    args = {"dataset_id": "a"}
    for _ in range(3):
        _complete(guard, "get_constructed_graph", args, {"nodes": 2})
    _complete(guard, "commit_graph_optimization", {"dataset_id": dataset}, {"revision": 2})
    assert (guard.before_call("get_constructed_graph", args) is None) is allowed


def test_queued_mutation_does_not_invalidate_queries_before_completion():
    guard = ToolLoopGuard()
    for _ in range(3):
        _complete(guard, "get_graph_info", {}, {"nodes": 2})
    _complete(guard, "load_graph", {}, {"success": True, "job": {"status": "queued", "job_id": "j"}})
    assert "重复" in guard.before_call("get_graph_info", {})
