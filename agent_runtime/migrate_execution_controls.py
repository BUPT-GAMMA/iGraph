"""Idempotent additive migration; run before enabling cancellation services."""
from agent_runtime.agno_factory import create_db
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.execution_store import ExecutionStore


def main():
    db = create_db(AgentRuntimeConfig.from_env())
    try:
        if db.db_engine.dialect.name != 'postgresql':
            raise RuntimeError('Execution controls require PostgreSQL in deployment')
        ExecutionStore(db.db_engine).migrate()
        print('igraph_execution_controls migration complete')
    finally:
        db.db_engine.dispose()


if __name__ == '__main__':
    main()
