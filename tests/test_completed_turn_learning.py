"""Exercise learning capture through the native Agent lifecycle, without a provider."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agno.db.sqlite import SqliteDb
from agno.factory import RequestContext
from agno.models.base import Model
from agno.models.metrics import MessageMetrics
from agno.models.response import ModelResponse
from agno.tools.function import Function

from agent_runtime import agno_factory
from agent_runtime.tool_dag import DAGModelMixin


DELETE = "删除节点 0"
REFUSAL = "该操作已拒绝，只能查询。"
FOLLOWUP = "查询当前节点总数"


def runtime(tmp_path, monkeypatch, *, pause=False, block=None, learning_block=None, controller=None, database=None, learning_queue=None):
    """Only provider and unrelated external tools are replaced; stores/DB stay native."""
    extracted, main_contexts = [], []

    class Scripted(DAGModelMixin, Model):
        def invoke(self, messages, **kwargs):
            system = str(messages[0].content or "")
            usage = MessageMetrics(input_tokens=7, output_tokens=3, total_tokens=10)
            if "You are a Session Context Manager" in system:
                if messages[-1].role == "tool":
                    return ModelResponse(role="assistant", content="Context saved", response_usage=usage)
                conversation = system.split("<conversation>", 1)[1].split("</conversation>", 1)[0]
                extracted.append(conversation)
                summary = "Deletion was refused; no deletion remains pending." if REFUSAL in conversation else "Deletion is pending."
                return ModelResponse(role="assistant", response_usage=usage, tool_calls=[{
                    "id": "save-context", "type": "function", "function": {
                        "name": "save_session_context", "arguments": json.dumps({"summary": summary}),
                    },
                }])
            if not any(message.role == "user" and message.content in {DELETE, FOLLOWUP} for message in messages):
                return ModelResponse(role="assistant", content="No profile updates", response_usage=usage)
            main_contexts.append(system)
            if pause and messages[-1].role == "user":
                return ModelResponse(role="assistant", tool_calls=[{
                    "id": "confirmation", "type": "function", "function": {"name": "confirm_operation", "arguments": "{}"},
                }], response_usage=usage)
            user = next(message.content for message in reversed(messages) if message.role == "user")
            return ModelResponse(role="assistant", content=REFUSAL if user == DELETE else "节点总数为 3。", response_usage=usage)

        async def ainvoke(self, messages, **kwargs):
            # Real provider calls yield before producing the final assistant message.
            await asyncio.sleep(0)
            if block is not None and messages[-1].role == "user" and messages[-1].content == DELETE:
                block.set()
                await asyncio.Event().wait()
            if learning_block is not None and "You are a Session Context Manager" in str(messages[0].content) and messages[-1].role != "tool":
                learning_block.set()
                await asyncio.Event().wait()
            return self.invoke(messages, **kwargs)

        def invoke_stream(self, messages, **kwargs):
            yield self.invoke(messages, **kwargs)

        async def ainvoke_stream(self, messages, **kwargs):
            yield await self.ainvoke(messages, **kwargs)

        def _parse_provider_response(self, response, **kwargs):
            return response

        def _parse_provider_response_delta(self, response):
            return response

    def local_graph():
        return {"nodes": 3}

    def confirm_operation():
        return "Confirmed"

    monkeypatch.setattr(agno_factory, "get_custom_model", lambda *_: None)
    monkeypatch.setattr(agno_factory, "allowed_model", lambda *_: "fixture")
    monkeypatch.setattr(agno_factory, "create_model", lambda *_: Scripted(id="fixture"))
    monkeypatch.setattr(agno_factory, "create_graph_tools", lambda *args, **kwargs:
                        Function(name="confirm_operation", entrypoint=confirm_operation, requires_confirmation=True) if pause else local_graph)
    monkeypatch.setattr(agno_factory, "create_workspace_tools", lambda: [])
    monkeypatch.setattr(agno_factory, "register_remote_graph_tools", lambda *_: [])
    monkeypatch.setattr(agno_factory, "build_skills", lambda *_: None)
    db = database or SqliteDb(db_file=str(tmp_path / "learning.db"))
    config = SimpleNamespace(default_model="fixture", embedding_model="", embedding_api_key="")
    agent = agno_factory.create_igraph_factory(config, db, execution_controller=controller, learning_queue=learning_queue).factory(RequestContext(user_id="alice"))
    agent.telemetry = False
    return agent, db, extracted, main_contexts


@pytest.mark.parametrize("stream", [False, True])
def test_next_turn_session_context_includes_current_refusal(tmp_path, monkeypatch, stream):
    """Pre-response capture leaves the refusal pending in the next system prompt."""
    async def scenario():
        agent, db, extracted, contexts = runtime(tmp_path, monkeypatch)
        try:
            if stream:
                _ = [event async for event in agent.arun(DELETE, user_id="alice", session_id="s", stream=True, stream_events=True)]
                first = db.get_session(session_id="s", user_id="alice").runs[-1]
            else:
                first = await agent.arun(DELETE, user_id="alice", session_id="s")
            assert first.content == REFUSAL
            context = agent._learning.session_context_store.get("s")
            assert context.summary == "Deletion was refused; no deletion remains pending."
            assert len(extracted) == 1
            assert f"Assistant: {REFUSAL}" in extracted[0]
            # Context tool round trip: 2 × 10 tokens, plus the main answer.
            assert first.metrics.details["learning_model"][0].total_tokens == 20
            assert first.metrics.total_tokens == 30
            # AgentOS builds a fresh Agent for each request: the second run must
            # recall persisted learning, rather than relying on cached objects.
            followup_agent, _, followup_extracted, followup_contexts = runtime(tmp_path, monkeypatch, database=db)
            if stream:
                _ = [event async for event in followup_agent.arun(FOLLOWUP, user_id="alice", session_id="s", stream=True)]
            else:
                await followup_agent.arun(FOLLOWUP, user_id="alice", session_id="s")
            assert "Deletion was refused; no deletion remains pending." in followup_contexts[-1]
            assert len(followup_extracted) == 1
            assert f"Assistant: {REFUSAL}" in followup_extracted[-1]
            assert "Assistant: 节点总数为 3。" in followup_extracted[-1]
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("capture_phase", ["before", "during"])
def test_controlled_cancel_during_capture_preserves_previous_learning(tmp_path, monkeypatch, capture_phase):
    from uuid import uuid4
    from sqlalchemy import create_engine
    from agno.learn.schemas import SessionContext
    from agno.os.managers import event_buffer
    from agno.run import cancel
    from agno.run.base import RunStatus
    from agno.session.agent import AgentSession
    from agent_runtime.execution_control import ExecutionController
    from agent_runtime.execution_scope import RunCancellationManager
    from agent_runtime.execution_store import ExecutionStore

    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / "controlled.db"))
        db.upsert_session(AgentSession(session_id="s", agent_id="igraph-agent", user_id="alice", runs=[], created_at=1, updated_at=1))
        store = ExecutionStore(create_engine(f"sqlite:///{tmp_path}/controls.db"))
        store.migrate()
        manager = RunCancellationManager()
        previous_manager = cancel.get_cancellation_manager()
        cancel.set_cancellation_manager(manager)
        controller = ExecutionController(db, store, manager, pending_directory=tmp_path / "pending")
        async def no_remote_jobs(*_):
            return {}
        controller._remote_cancel = no_remote_jobs
        row, _ = store.register("alice", "s", str(uuid4()), DELETE)
        rid = row["run_id"]
        store.acknowledge("alice", "s", rid, 1, preparation_stopped=True)
        entered = asyncio.Event()
        release = asyncio.Event()
        if capture_phase == "before":
            original_guard = agno_factory.loop_guard_post_hook
            async def guard_then_wait(run_context, run_output):
                await original_guard(run_context, run_output)
                entered.set()
                await release.wait()
            monkeypatch.setattr(agno_factory, "loop_guard_post_hook", guard_then_wait)
        agent, _, extracted, _ = runtime(tmp_path, monkeypatch, database=db, controller=controller,
                                        learning_block=entered if capture_phase == "during" else None)
        agent.learning.db = db
        agent.learning.session_context_store.save("s", SessionContext(session_id="s", summary="Earlier validated fact"), user_id="alice")
        frames = []
        async def observe():
            async for frame in agent.arun(DELETE, session_id="s", user_id="alice", run_id=rid,
                                     stream=True, stream_events=True, background=True):
                frames.append(frame)
        task = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            scope = manager.scopes[rid]
            store.request_cancel("alice", "s", str(uuid4()), run_id=rid)
            await manager.acancel_run(rid)
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            await asyncio.wait_for(asyncio.gather(*scope.executors, return_exceptions=True), 1)
            controller.wake(rid)
            await asyncio.wait_for(asyncio.shield(controller.tasks[rid]), 3)
            summary = store.summary(store.get("alice", "s", run_id=rid))
            assert summary["execution_stopped"] and summary["terminal_persisted"]
            assert summary["cancel_status"] == "confirmed" and summary["can_send"]
            assert summary["run_status"] == "cancelled"
            saved = db.get_session(session_id="s", user_id="alice").get_run(rid)
            assert saved.status == RunStatus.cancelled
            assert saved.content == REFUSAL
            persisted_events = [event.event for event in saved.events]
            assert persisted_events.index("RunCancelled") < persisted_events.index("RunCompleted")
            streamed_events = [json.loads(line[5:])["event"] for frame in frames
                               for line in frame.splitlines() if line.startswith("data:")]
            assert "RunContentCompleted" in streamed_events
            assert streamed_events.index("RunCancelled") < streamed_events.index("RunCompleted")
            assert agent.learning.session_context_store.get("s").summary == "Earlier validated fact"
            assert extracted == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()
            cancel.set_cancellation_manager(previous_manager)
            event_buffer.cleanup_run(rid)
            db.close()
            store.engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_pause_has_no_capture_and_resume_captures_final_answer(tmp_path, monkeypatch, stream):
    async def scenario():
        agent, db, extracted, _ = runtime(tmp_path, monkeypatch, pause=True)
        try:
            if stream:
                _ = [event async for event in agent.arun(DELETE, user_id="alice", session_id="s", stream=True)]
                paused = db.get_session(session_id="s", user_id="alice").runs[-1]
            else:
                paused = await agent.arun(DELETE, user_id="alice", session_id="s")
            assert paused.is_paused
            assert agent._learning.session_context_store.get("s") is None
            assert extracted == []
            pending = [tool for tool in paused.tools if tool.requires_confirmation]
            pending[0].confirmed = True
            resume_agent, _, resumed_extracted, _ = runtime(tmp_path, monkeypatch, database=db, pause=True)
            if stream:
                _ = [event async for event in resume_agent.acontinue_run(run_id=paused.run_id, updated_tools=pending,
                     user_id="alice", session_id="s", stream=True)]
            else:
                await resume_agent.acontinue_run(run_id=paused.run_id, updated_tools=pending, user_id="alice", session_id="s")
            assert resume_agent._learning.session_context_store.get("s").summary == "Deletion was refused; no deletion remains pending."
            assert len(resumed_extracted) == 1
        finally:
            db.close()
    asyncio.run(scenario())


def test_cancelled_response_does_not_capture_unfinished_turn(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        agent, db, extracted, _ = runtime(tmp_path, monkeypatch, block=entered)
        task = asyncio.create_task(agent.arun(DELETE, user_id="alice", session_id="s"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert agent._learning.session_context_store.get("s") is None
            assert extracted == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            db.close()
    asyncio.run(scenario())


def test_loop_guard_error_does_not_publish_completed_learning(tmp_path, monkeypatch):
    from agent_runtime.loop_guard import _guard_for_run

    async def scenario():
        agent, db, extracted, _ = runtime(tmp_path, monkeypatch)
        async def stop_guard(run_context):
            _guard_for_run(run_context).stopped = True
        agent.pre_hooks.append(stop_guard)
        try:
            _ = [event async for event in agent.arun(DELETE, session_id="s", user_id="alice", stream=True)]
            run = db.get_session(session_id="s", user_id="alice").runs[-1]
            assert str(getattr(run.status, "value", run.status)).lower() == "error"
            assert agent._learning.session_context_store.get("s") is None
            assert extracted == []
        finally:
            db.close()
    asyncio.run(scenario())
