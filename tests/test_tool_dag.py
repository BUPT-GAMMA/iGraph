"""Native Agno execution tests: graph dependencies, overlap, errors and HITL."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.tools.function import Function, FunctionCall, ToolResult

from agent_runtime.graph_jobs import GraphMCPTools, completed_graph_result
from agent_runtime.tool_dag import DAGModelMixin, build_dependencies


class DAGModel(DAGModelMixin, Model):
    def invoke(self, *args, **kwargs):
        return ModelResponse(role="assistant", content="完成")

    async def ainvoke(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args, **kwargs):
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, *args, **kwargs):
        yield self.invoke(*args, **kwargs)

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response


def call(name, fn, index, args=None, **options):
    return FunctionCall(function=Function(name=name, entrypoint=fn, **options),
                        arguments=args or {}, call_id=str(index))


async def run_batch(calls, **kwargs):
    results, events = [], []
    async for event in DAGModel(id="test-dag").arun_function_calls(calls, results, **kwargs):
        events.append(event)
    return results, events


def test_native_dag_fanout_write_fanin_and_completion_order():
    async def scenario():
        trace, joined = [], asyncio.Event()
        graph = {"version": 0}

        async def load():
            trace.append("load")
            graph["version"] = 1
            return json.dumps({"success": True})

        async def degree():
            assert graph["version"] == 1
            trace.append("degree-start")
            await asyncio.wait_for(joined.wait(), 2)
            await asyncio.sleep(.02)
            trace.append("degree-end")
            return json.dumps({"success": True, "degree": 2})

        async def centrality():
            assert graph["version"] == 1
            assert "degree-start" in trace
            trace.append("centrality")
            joined.set()
            return json.dumps({"success": True, "node": "B"})

        async def change():
            assert "degree-end" in trace and "centrality" in trace
            trace.append("change")
            graph["version"] = 2
            return json.dumps({"success": True})

        calls = [call("load_graph", load, 0), call("degree_analysis", degree, 1),
                 call("node_centrality", centrality, 2), call("to_undirected", change, 3)]
        assert build_dependencies(calls) == [set(), {0}, {0}, {0, 1, 2}]
        results, events = await run_batch(calls)
        assert [r.tool_call_id for r in results] == ["0", "1", "2", "3"]
        completed = [e.tool_executions[0].tool_call_id for e in events if e.event == "ToolCallCompleted"]
        assert completed.index("2") < completed.index("1")
        assert trace[-1] == "change"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["payload", "exception", "pending"])
def test_failed_predecessor_blocks_dependent_but_independent_branch_finishes(failure):
    async def scenario():
        executed = []
        async def load():
            if failure == "exception":
                raise ValueError("bad graph")
            return json.dumps({"success": failure == "pending", "async": failure == "pending",
                               "job_id": "not-finished", "status": "queued"})
        async def dependent():
            executed.append("dependent")
        async def independent():
            executed.append("independent")
            return "ok"
        results, _ = await run_batch([call("load_graph", load, 0), call("get_graph_info", dependent, 1),
                                     call("get_graph_construction_capabilities", independent, 2)])
        assert executed == ["independent"]
        assert json.loads(results[1].content)["code"] == "TOOL_DEPENDENCY_BLOCKED"
        assert results[1].tool_call_error
        if failure != "pending":
            assert results[0].tool_call_error
    asyncio.run(scenario())


def test_pause_blocks_dependent_and_approved_call_runs_on_continuation():
    async def scenario():
        calls = []
        async def clear():
            calls.append("clear")
            return json.dumps({"success": True})
        async def info():
            calls.append("info")
            return json.dumps({"success": True})
        batch = [call("clear_graph", clear, 0, requires_confirmation=True), call("get_graph_info", info, 1)]
        results, events = await run_batch(batch)
        assert calls == []
        assert any(e.event == "ToolCallPaused" for e in events)
        assert json.loads(results[0].content)["code"] == "TOOL_DEPENDENCY_BLOCKED"
        await run_batch(batch, skip_pause_check=True)
        assert calls == ["clear", "info"]
    asyncio.run(scenario())


def test_limit_and_unknown_tool_barrier(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_MAX_CONCURRENCY", "2")
    async def scenario():
        active, peak, order = 0, 0, []
        async def read():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            order.append("read")
            return "ok"
        async def barrier():
            assert active == 0 and len(order) == 3
            order.append("barrier")
            return "ok"
        batch = [call("get_graph_info", read, i) for i in range(3)]
        batch += [call("custom_unknown", barrier, 3), call("get_graph_info", read, 4)]
        results, _ = await run_batch(batch, function_call_limit=4)
        assert peak == 2 and order == ["read", "read", "read", "barrier"]
        assert len(results) == 5 and results[-1].tool_call_error
    asyncio.run(scenario())


def test_cancellation_drains_started_tools_and_never_starts_dependents():
    async def scenario():
        entered, cleaned = asyncio.Event(), []
        async def reading():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        async def writing():
            pytest.fail("dependent must not run after cancellation")
        task = asyncio.create_task(run_batch([call("get_graph_info", reading, 0),
                                             call("load_graph", writing, 1)]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["done", "failed", "cancelled"])
def test_background_result_waits_and_propagates_final_payload(state):
    async def scenario():
        count = 0
        async def poll(job_id, *, after_seq=0):
            nonlocal count
            count += 1
            assert job_id == "j1"
            return json.dumps({"success": True, "job": {
                "status": "running" if count == 1 else state,
                "result": {"success": True, "graph_version": 2}, "message": "job outcome",
            }})
        result = await completed_graph_result(ToolResult(content=json.dumps({
            "success": True, "async": True, "job_id": "j1", "status": "queued",
        })), poll, interval=0)
        payload = json.loads(result.content)
        assert count == 2 and payload["success"] == (state == "done")
        if state == "done":
            assert payload["graph_version"] == 2 and payload["async"] is False
    asyncio.run(scenario())


def test_real_agent_stores_all_parallel_tool_results(tmp_path):
    async def scenario():
        rounds, overlap = [], asyncio.Event()
        class Scripted(DAGModel):
            def invoke(self, *args, **kwargs):
                if not rounds:
                    rounds.append(True)
                    return ModelResponse(role="assistant", tool_calls=[{
                        "id": str(i), "type": "function", "function": {"name": name, "arguments": "{}"},
                    } for i, name in enumerate(["degree_analysis", "node_centrality"])])
                return ModelResponse(role="assistant", content="两个分支均完成")
        async def degree():
            await asyncio.wait_for(overlap.wait(), 2)
            return '{"success":true,"degrees":{"A":1,"B":2,"C":1}}'
        async def centrality():
            overlap.set()
            return '{"success":true,"top_node":"B"}'
        db = SqliteDb(db_file=str(tmp_path / "dag.db"))
        agent = Agent(id="dag-test", model=Scripted(id="scripted"), db=db, telemetry=False,
                      tools=[Function(name="degree_analysis", entrypoint=degree),
                             Function(name="node_centrality", entrypoint=centrality)])
        try:
            response = await agent.arun("分析 A-B-C 的度和关键节点", session_id="s1", user_id="u1")
            assert response.content == "两个分支均完成"
            saved = db.get_session(session_id="s1", user_id="u1").get_run(response.run_id)
            assert len(saved.tools) == 2
            assert all(not tool.tool_call_error for tool in saved.tools)
            assert {tool.tool_call_id for tool in saved.tools} == {"0", "1"}
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cycle", [False, True])
def test_background_job_chains_are_resolved_before_dependencies(cycle):
    async def scenario():
        calls = []
        async def poll(job_id, *, after_seq=0):
            calls.append(job_id)
            result = {"success": True, "async": True, "job_id": "parent" if cycle else "child"}
            if job_id == "child":
                result = {"success": True, "answer": 42}
            return {"success": True, "job": {"status": "done", "result": result}}
        operation = completed_graph_result({"success": True, "async": True, "job_id": "parent"}, poll)
        if cycle:
            with pytest.raises(RuntimeError, match="DEPENDENCY_INVALID"):
                await operation
        else:
            result = json.loads((await operation).content)
            assert result["answer"] == 42 and calls == ["parent", "child"]
    asyncio.run(scenario())


@pytest.mark.parametrize("error", ["Error: disconnected", "Error from MCP tool 'load_graph': invalid graph",
                                   "MCP tool 'load_graph' failed: timeout"])
def test_mcp_text_errors_cannot_unlock_dependencies(error):
    async def scenario():
        async def poll(_):
            pytest.fail("must not poll a failed submission")
        result = await completed_graph_result(ToolResult(content=error), poll)
        assert json.loads(result.content)["success"] is False
    asyncio.run(scenario())


def test_async_generator_cancellation_owns_native_consumer():
    async def scenario():
        entered, cleaned = asyncio.Event(), []
        async def streaming():
            try:
                entered.set()
                yield "partial"
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        task = asyncio.create_task(run_batch([call("custom_stream", streaming, 0)]))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [True]
    asyncio.run(scenario())


def test_native_mcp_discovery_identity_job_wait_and_saved_state(tmp_path):
    from mcp.types import CallToolResult, TextContent, Tool, ListToolsResult
    from agent_runtime.construction_state import construction_tool_hook
    from agent_runtime.graph_tools import graph_identity_headers

    async def scenario():
        calls, identities, rounds = [], [], []
        class Session:
            async def send_ping(self):
                pass
            async def list_tools(self):
                return ListToolsResult(tools=[Tool(name="load_constructed_graph", inputSchema={
                    "type": "object", "properties": {"dataset_id": {"type": "string"}},
                    "required": ["dataset_id"],
                })])
            async def call_tool(self, name, arguments):
                calls.append(name)
                payload = {"success": True, "async": True, "job_id": "j1", "status": "queued"}
                if name == "get_job_status":
                    payload = {"success": True, "job": {"status": "done", "result": {
                        "success": True, "construction_dataset_id": "d1", "construction_revision": 4,
                    }}}
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])
        class Toolkit(GraphMCPTools):
            @asynccontextmanager
            async def _owned_session(self, headers):
                identities.append(headers)
                yield Session()
        class Scripted(DAGModel):
            def invoke(self, *args, **kwargs):
                if not rounds:
                    rounds.append(True)
                    return ModelResponse(role="assistant", tool_calls=[{
                        "id": "load", "type": "function", "function": {
                            "name": "load_constructed_graph", "arguments": '{"dataset_id":"d1"}'}}])
                return ModelResponse(role="assistant", content="loaded")
        toolkit = Toolkit(url="http://test.invalid/mcp", header_provider=graph_identity_headers)
        db = SqliteDb(db_file=str(tmp_path / "mcp-dag.db"))
        agent = Agent(model=Scripted(id="scripted"), db=db, telemetry=False, tools=[toolkit],
                      tool_hooks=[construction_tool_hook], session_state={
                          "data_state": {"active_dataset_id": "d1", "construction_revision": 1}})
        try:
            result = await agent.arun("加载已构图数据", user_id="u1", session_id="s1")
            assert calls == ["load_constructed_graph", "get_job_status"]
            assert all(h["X-User-ID"] == "u1" and h["X-Session-ID"] == "s1"
                       and h["X-Run-ID"] == result.run_id for h in identities if h)
            state = db.get_session(user_id="u1", session_id="s1").session_data["session_state"]
            assert state["data_state"]["construction_revision"] == 4
        finally:
            db.close()
    asyncio.run(scenario())


def test_native_agent_pause_continue_replans_blocked_child(tmp_path):
    async def scenario():
        rounds, invoked = [], []
        class Scripted(DAGModel):
            def invoke(self, *args, **kwargs):
                rounds.append(True)
                if len(rounds) <= 2:
                    names = ["clear_graph", "get_graph_info"] if len(rounds) == 1 else ["get_graph_info"]
                    return ModelResponse(role="assistant", tool_calls=[{
                        "id": f"{len(rounds)}-{name}", "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    } for name in names])
                return ModelResponse(role="assistant", content="confirmed and checked")
        async def clear():
            invoked.append("clear")
            return '{"success":true}'
        async def info():
            invoked.append("info")
            return '{"success":true,"nodes":0}'
        db = SqliteDb(db_file=str(tmp_path / "pause.db"))
        agent = Agent(model=Scripted(id="scripted"), db=db, telemetry=False, tools=[
            Function(name="clear_graph", entrypoint=clear, requires_confirmation=True),
            Function(name="get_graph_info", entrypoint=info),
        ])
        try:
            paused = await agent.arun("清空后检查图", user_id="u1", session_id="s1")
            assert paused.is_paused and invoked == []
            pending = [tool for tool in paused.tools if tool.requires_confirmation]
            assert len(pending) == 1
            pending[0].confirmed = True
            finished = await agent.acontinue_run(run_id=paused.run_id, updated_tools=pending,
                                                 user_id="u1", session_id="s1")
            assert finished.content == "confirmed and checked"
            assert invoked == ["clear", "info"]
            saved = db.get_session(user_id="u1", session_id="s1").get_run(paused.run_id)
            assert str(getattr(saved.status, "value", saved.status)).lower() == "completed"
        finally:
            db.close()
    asyncio.run(scenario())
