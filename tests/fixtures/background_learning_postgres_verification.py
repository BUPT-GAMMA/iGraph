"""Run inside agent-runtime: isolated PostgreSQL multi-replica learning check."""
import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

from agno.db.postgres import PostgresDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from sqlalchemy.schema import CreateSchema, DropSchema

from agent_runtime.background_learning import BackgroundLearning
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.execution_store import ExecutionStore


async def main():
    schema = 'learning_check_' + uuid4().hex
    db = PostgresDb(db_url=AgentRuntimeConfig.from_env().database_url, db_schema=schema)
    engine = db.db_engine
    with engine.begin() as conn:
        conn.execute(CreateSchema(schema))
    db.db_engine = engine.execution_options(schema_translate_map={None: schema})
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def capture(**kwargs):
        seen.append(kwargs['user_id'])
        if kwargs['user_id'] == 'alice':
            entered.set()
            await release.wait()
    factory = lambda *_: SimpleNamespace(acapture_background_turn=capture)
    one = BackgroundLearning(db, ExecutionStore(db.db_engine), factory)
    two = BackgroundLearning(db, ExecutionStore(db.db_engine), factory)
    try:
        one.store.migrate()
        for uid in ('alice', 'bob'):
            sid, rid = str(uuid4()), str(uuid4())
            run = RunOutput(run_id=rid, session_id=sid, user_id=uid, agent_id='igraph-agent', status=RunStatus.completed,
                messages=[Message(role='user', content='hello'), Message(role='assistant', content='Hi')])
            db.upsert_session(AgentSession(session_id=sid, user_id=uid, agent_id='igraph-agent', runs=[run],
                                          created_at=int(time.time()), updated_at=int(time.time())))
            await one.enqueue(user_id=uid, session_id=sid, run_id=rid, planning_mode='auto')
        task = asyncio.create_task(one.process_user('alice'))
        await asyncio.wait_for(entered.wait(), 5)
        assert await asyncio.wait_for(two.process_user('alice'), 2) is False
        assert await asyncio.wait_for(two.process_user('bob'), 2) is True
        # Foreground session writes are not serialized behind learning.
        def foreground():
            with one.store.transaction('alice', 'foreground'):
                return True
        assert await asyncio.wait_for(asyncio.to_thread(foreground), 2)
        release.set()
        await asyncio.wait_for(task, 5)
        assert seen == ['alice', 'bob']
        assert not one.pending()
        print('PASS: PostgreSQL excludes duplicate work across replicas, isolates users and permits foreground writes.')
    finally:
        release.set()
        await one.close()
        await two.close()
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()

asyncio.run(main())
