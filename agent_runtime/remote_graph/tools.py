"""Native tools receive identity from Agno and a server-frozen binding."""
from typing import Any

from agno.run import RunContext
from pydantic import ValidationError

from agent_runtime.execution_store import ControlError
from agent_runtime.workspace_tools import workspace_root
from .contracts import QueryRequest, RemoteBinding


def remote_graph_context_hook(binding):
    source = RemoteBinding.model_validate(binding).model_dump() if binding else None

    async def set_remote_graph_context(run_context: RunContext):
        if run_context.session_state is None:
            run_context.session_state = {}
        run_context.session_state['remote_graph'] = source
        # Older persisted/client-supplied snapshots never determine tool access.
        run_context.session_state.pop('remote_graph_binding', None)

    return set_remote_graph_context


def create_remote_graph_tools(executor, binding: RemoteBinding | None) -> list:
    source = RemoteBinding.model_validate(binding) if binding else None

    def identity(context):
        if source is None or executor is None:
            raise ControlError('REMOTE_GRAPH_NOT_SELECTED', 409)
        if not context.user_id or not context.session_id:
            raise ControlError('REMOTE_GRAPH_NOT_FOUND', 404)
        return context.user_id

    def error(exc):
        return {'success': False, 'error_code': exc.code if isinstance(exc, ControlError) else 'REMOTE_GRAPH_INVALID_CONFIG'}

    async def describe_remote_graph(run_context: RunContext, labels: list[str] | None = None,
                                    relationship_types: list[str] | None = None) -> dict[str, Any]:
        """Discover the selected remote database's labels, relationships and property types."""
        try:
            uid = identity(run_context)
            return await executor.describe(uid, source, labels, relationship_types)
        except (ControlError, ValidationError, ValueError) as exc:
            return error(exc)

    async def query_remote_graph(query: str, run_context: RunContext, parameters: dict[str, Any] | None = None,
                                 save_result: bool = False) -> dict[str, Any]:
        """Run parameterized Cypher on the selected remote database; optionally save bounded JSON."""
        try:
            uid = identity(run_context)
            request = QueryRequest(query=query, parameters=parameters or {}, save_result=save_result)
            directory = None
            if save_result:
                root = workspace_root(run_context)
                directory = root / 'Ephemeral' / 'remote_graph'
                if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                    raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
            result = await executor.query(uid, source, request, directory)
            return result.model_dump()
        except (ControlError, ValidationError, ValueError) as exc:
            return error(exc)

    return [describe_remote_graph, query_remote_graph]
