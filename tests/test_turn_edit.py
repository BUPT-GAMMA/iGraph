import asyncio
from uuid import uuid4

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from pydantic import Field

from agent_runtime.cancellation_compat import ControlledAgent
from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager
from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.turn_edit import NativeTurnEditor


@pytest.fixture
def history(tmp_path):
    db = SqliteDb(db_file=str(tmp_path / 'history.db'))
    old = RunOutput(run_id=str(uuid4()), agent_id='igraph-agent', status=RunStatus.completed,
                    messages=[Message(role='user', content='KEEP QUESTION'), Message(role='assistant', content='KEEP ANSWER')], content='KEEP ANSWER')
    tail = RunOutput(run_id=str(uuid4()), agent_id='igraph-agent', status=RunStatus.completed,
                     messages=[Message(role='user', content='OLD QUESTION'), Message(role='assistant', content='OLD ANSWER')], content='OLD ANSWER')
    db.upsert_session(AgentSession(session_id='s', user_id='alice', agent_id='igraph-agent', runs=[old, tail], created_at=1))
    store = ExecutionStore(db.db_engine)
    store.migrate()
    editor = NativeTurnEditor(db._get_table(table_type='sessions'))
    old = db.get_session(session_id='s').runs[0]
    yield db, store, editor, old, tail
    db.db_engine.dispose()


def register(history, key=None):
    _, store, editor, _, tail = history
    return store.register('alice', 's', key or str(uuid4()), 'edit', replace_run_id=tail.run_id, validate_edit=editor.validate)


def test_preparation_failure_and_duplicate_start_do_not_remove_history(history):
    db, store, editor, _, tail = history
    key = str(uuid4())
    row, claimed = register(history, key)
    assert claimed
    assert register(history, key)[1] is False
    assert db.get_session(session_id='s').get_run(tail.run_id)
    store.preparation_exited('alice', 's', row['run_id'], 1, failed=True)
    assert db.get_session(session_id='s').get_run(tail.run_id)
    with pytest.raises(ControlError, match='RUN_TERMINATED'):
        store.claim_agent('alice', 's', row['run_id'], replace_edit=editor.replace)


def test_rejects_stale_foreign_and_active_targets_without_deleting(history):
    db, store, editor, old, tail = history
    for uid, target, code in [('alice', old.run_id, 'EDIT_TARGET_CHANGED'), ('bob', tail.run_id, 'SESSION_NOT_FOUND')]:
        with pytest.raises(ControlError, match=code):
            store.register(uid, 's', str(uuid4()), 'edit', replace_run_id=target, validate_edit=editor.validate)
    row, _ = register(history)
    with pytest.raises(ControlError, match='RUN_ACTIVE'):
        register(history)
    assert len(db.get_session(session_id='s').runs) == 2
    store.request_cancel('alice', 's', str(uuid4()), run_id=row['run_id'])
    with pytest.raises(ControlError, match='RUN_CANCELLED'):
        store.claim_agent('alice', 's', row['run_id'], replace_edit=editor.replace)
    assert len(db.get_session(session_id='s').runs) == 2


def test_tail_change_at_launch_rolls_back_the_claim(history):
    db, store, editor, _, tail = history
    row, _ = register(history)
    session = db.get_session(session_id='s')
    session.runs.append(RunOutput(run_id=str(uuid4()), status=RunStatus.completed, content='NEWER'))
    db.upsert_session(session)
    with pytest.raises(ControlError, match='EDIT_TARGET_CHANGED'):
        store.claim_agent('alice', 's', row['run_id'], replace_edit=editor.replace)
    assert db.get_session(session_id='s').get_run(tail.run_id)
    assert not store.get('alice', 's', run_id=row['run_id'])['state']['agent_claimed']


class RecordingModel(Model):
    seen: list[str] = Field(default_factory=list)

    def invoke(self, messages, **kwargs):
        self.seen = [str(message.content) for message in messages]
        return ModelResponse(role='assistant', content='NEW ANSWER')

    async def ainvoke(self, messages, **kwargs):
        return self.invoke(messages, **kwargs)

    def invoke_stream(self, messages, **kwargs):
        yield self.invoke(messages, **kwargs)

    async def ainvoke_stream(self, messages, **kwargs):
        yield self.invoke(messages, **kwargs)

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response


@pytest.mark.parametrize('status', [RunStatus.completed, RunStatus.cancelled])
def test_native_model_and_saved_history_replace_only_the_tail(history, status):
    db, store, editor, old, tail = history
    session = db.get_session(session_id='s')
    session.runs[-1].status = status
    db.upsert_session(session)
    row, _ = register(history)

    async def scenario():
        controller = ExecutionController(db, store, RunCancellationManager())
        model = RecordingModel(id='deterministic')
        agent = ControlledAgent(id='igraph-agent', db=db, model=model, add_history_to_context=True,
                                telemetry=False, store_events=True)
        agent.execution_controller = controller
        try:
            result = await agent.arun('NEW QUESTION', session_id='s', user_id='alice', run_id=row['run_id'])
            assert result.content == 'NEW ANSWER'
            assert 'KEEP QUESTION' in model.seen and 'KEEP ANSWER' in model.seen
            assert 'NEW QUESTION' in model.seen
            assert not any('OLD QUESTION' in item or 'OLD ANSWER' in item for item in model.seen)
            saved = db.get_session(session_id='s')
            assert [run.run_id for run in saved.runs] == [old.run_id, row['run_id']]
            assert saved.runs[0].to_dict() == old.to_dict()
            assert saved.runs[-1].content == 'NEW ANSWER'
        finally:
            await controller.close()
    asyncio.run(scenario())
