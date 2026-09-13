from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator

from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.run import RunStatus

from agent_runtime.loop_guard import agentos_tool_hook, progress_guard_hook


@dataclass
class _RepeatedCallModel(Model):
    """Deterministic provider used to exercise AgentOS's native model loop."""

    calls: int = 0

    def __init__(self) -> None:
        super().__init__(id="test-repeated-call-model")
        self.calls = 0

    def _response(self) -> ModelResponse:
        self.calls += 1
        if self.calls <= 4:
            name = "repeat_tool"
            arguments: dict[str, Any] = {"value": "same"}
        elif self.calls == 5:
            name = "sentinel_tool"
            arguments = {}
        else:
            return ModelResponse(content="done")
        return ModelResponse(
            tool_calls=[
                {
                    "id": f"call-{self.calls}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        )

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._response()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._response()

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._response()

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self._response()

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


class _BatchRepeatedCallModel(_RepeatedCallModel):
    def _response(self) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            calls = [
                {
                    "id": f"repeat-{index}",
                    "type": "function",
                    "function": {"name": "repeat_tool", "arguments": '{"value":"same"}'},
                }
                for index in range(4)
            ]
            calls.append(
                {
                    "id": "sentinel",
                    "type": "function",
                    "function": {"name": "sentinel_tool", "arguments": "{}"},
                }
            )
            return ModelResponse(tool_calls=calls)
        return ModelResponse(content="done")


class _MutationBatchModel(_RepeatedCallModel):
    def _response(self) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[
                    {
                        "id": "graph-mutation",
                        "type": "function",
                        "function": {"name": "load_graph", "arguments": '{"edge_text":"a,b"}'},
                    },
                    {
                        "id": "workspace-mutation",
                        "type": "function",
                        "function": {"name": "workspace_bash", "arguments": '{"command":"printf file"}'},
                    },
                ]
            )
        return ModelResponse(content="done")



import asyncio
import copy
import os
import signal
from types import SimpleNamespace

import pytest
from agno.run import RunContext
from agno.tools.function import ToolResult
from agent_runtime.loop_guard import loop_guard_tool_hook, loop_guard_post_hook, _guard_for_run, ToolLoopGuard
from agent_runtime.tool_dag import DAGModelMixin
from agent_runtime.execution_scope import RunScope, current_scope
from agent_runtime.workspace_tools import create_workspace_tools, workspace_root_for
from workspace_storage import derived_file, visible_files

class GuardDAGModel(DAGModelMixin, _RepeatedCallModel):
    pass

def test_guard_is_shared_across_dag_batches_and_persists_native_error():
    async def scenario():
        executed = []
        async def repeat_tool(value: str):
            executed.append(value)
            return json.dumps({"success": True, "value": value})
        db = InMemoryDb()
        agent = Agent(model=GuardDAGModel(), db=db, tools=[repeat_tool],
            tool_hooks=[loop_guard_tool_hook], post_hooks=[loop_guard_post_hook],
            checkpoint="tool-batch", store_events=True, events_to_skip=[])
        events = [event async for event in agent.arun("repeat", session_id="s", user_id="u", stream=True, stream_events=True)]
        saved = db.get_session(session_id="s", user_id="u").runs[-1]
        assert executed == ["same"] * 3
        assert saved.status == RunStatus.error
        assert events[-1].event == "RunError"
        assert not any(event.event == "RunCompleted" for event in events)
    asyncio.run(scenario())

@pytest.mark.parametrize("wrap", [lambda value: json.dumps(value), lambda value: ToolResult(content=json.dumps(value))])
def test_mcp_and_serialized_business_failures_count_toward_guard(wrap):
    async def scenario():
        context = RunContext(run_id="r", session_id="s", user_id="u")
        setattr(context, "_igraph_tool_loop_guard", ToolLoopGuard(max_failures=2))
        async def fail(**kwargs):
            return wrap({"success": False, "error": "bad input"})
        await loop_guard_tool_hook("x", fail, {"n": 1}, context)
        from agno.exceptions import StopAgentRun
        with pytest.raises(StopAgentRun):
            await loop_guard_tool_hook("x", fail, {"n": 2}, copy.copy(context))
    asyncio.run(scenario())

def test_guard_uses_execution_scope_even_with_distinct_tool_contexts():
    scope = RunScope("r", "u", "s", 1)
    token = current_scope.set(scope)
    try:
        assert _guard_for_run(SimpleNamespace()) is _guard_for_run(SimpleNamespace())
    finally:
        current_scope.reset(token)

def test_workspace_uses_shared_data_root_and_confines_downloads(tmp_path, monkeypatch):
    monkeypatch.delenv("IGRAPH_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    root = workspace_root_for("u", "s")
    assert root == tmp_path / "workspaces/u/s"
    (root / "Derived/visible.txt").write_text("ok")
    (root / "Ephemeral/private.txt").write_text("private")
    assert derived_file("u", "s", "visible.txt").read_text() == "ok"
    for path in ("../Ephemeral/private.txt", "/etc/passwd", "folder/../../Ephemeral/private.txt"):
        with pytest.raises(ValueError):
            derived_file("u", "s", path)
    (root / "Derived/link").symlink_to(root / "Ephemeral", target_is_directory=True)
    with pytest.raises(ValueError):
        derived_file("u", "s", "link/private.txt")
    assert [item["path"] for item in visible_files("u", "s")] == ["Derived/visible.txt"]
    assert visible_files("other-user", "s") == []

def test_workspace_cancel_stops_subprocess_group(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("IGRAPH_WORKSPACE_SANDBOX_MODE", "fallback")
    async def scenario():
        tools = {tool.__name__: tool for tool in create_workspace_tools()}
        ctx = SimpleNamespace(user_id="u", session_id="s")
        root = workspace_root_for("u", "s")
        task = asyncio.create_task(tools["workspace_bash"]("echo $$ > Ephemeral/pid; sleep 30", ctx))
        for _ in range(100):
            if (root / "Ephemeral/pid").exists():
                break
            await asyncio.sleep(.01)
        pid = int((root / "Ephemeral/pid").read_text())
        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert asyncio.get_running_loop().time() - started < 3
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    asyncio.run(scenario())


def test_agent_notes_cannot_overwrite_controlled_state():
    from agent_runtime.agent_notes import update_agent_notes
    ctx = RunContext(run_id="r", session_id="s", user_id="u",
                     session_state={"project_id": "original", "data_state": {"active_dataset_id": "safe"}})
    update_agent_notes({"project_id": "untrusted", "data_state": {}}, ctx)
    assert ctx.session_state["project_id"] == "original"
    assert ctx.session_state["data_state"]["active_dataset_id"] == "safe"
    assert ctx.session_state["agent_notes"]["project_id"] == "untrusted"


def test_checkpoint_never_overwrites_approval_pause():
    async def scenario():
        class ApprovalModel(GuardDAGModel):
            def _response(self):
                self.calls += 1
                return ModelResponse(tool_calls=[{"id": "clear", "type": "function",
                    "function": {"name": "clear_graph", "arguments": "{}"}}])
        from agno.tools.function import Function
        executed = []
        async def clear_graph():
            executed.append(True)
            return "cleared"
        function = Function.from_callable(clear_graph)
        function.requires_confirmation = True
        db = InMemoryDb()
        agent = Agent(model=ApprovalModel(), db=db, tools=[function],
            checkpoint="tool-batch", store_events=True, events_to_skip=[],
            tool_hooks=[loop_guard_tool_hook], post_hooks=[loop_guard_post_hook])
        events = [event async for event in agent.arun("clear", session_id="approval", user_id="u", stream=True, stream_events=True)]
        saved = db.get_session(session_id="approval", user_id="u").runs[-1]
        assert saved.status == RunStatus.paused
        assert not executed
        assert any(event.event == "RunPaused" for event in events)
        assert not any(event.event == "RunCompleted" for event in events)
    asyncio.run(scenario())
