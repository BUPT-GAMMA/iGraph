"""Native tools receive identity from Agno and a server-frozen binding."""
from typing import Any

from agno.run import RunContext
from pydantic import ValidationError

from agent_runtime.execution_store import ControlError
from agent_runtime.workspace_tools import workspace_root
from .contracts import QueryRequest, RemoteBinding


def remote_graph_instructions(binding) -> list[str]:
    """Route coexisting sources only when this request has a frozen selection."""
    if not binding:
        return []
    return [
        '当前会话同时可用本地图和选中的远程数据库，它们不是同一张图。'
        '用户按属性/业务 ID 指定实体时，先读取 remote-graph-qa skill。'
        '如果修改请求未明确来源且当前对话未确定来源，直接询问“你指的是本地图还是远程数据库？”'
        '先获得来源答复，再决定使用哪类工具；不要把本地图的数字节点 ID 当成同值的远程业务属性。'
        '明确的本地图任务仍可正常编辑；明确的远程任务按该 skill 响应，保留本地图。'
    ]


def remote_graph_context_hook(binding):
    source = RemoteBinding.model_validate(binding).model_dump() if binding else None

    async def set_remote_graph_context(run_context: RunContext):
        if run_context.session_state is None:
            run_context.session_state = {}
        run_context.session_state['remote_graph'] = source
        # Routing guidance is request-scoped instructions, never persisted data.
        run_context.session_state.pop('remote_graph_routing', None)
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
        """Discover the selected remote database's labels, relationships and property types.

        Optional labels and relationship_types are JSON arrays, e.g. labels=["Paper"].
        This is schema evidence, not node/edge counts or business definitions.
        Property types describe observed properties, not their presence on every entity.
        Use coverage for sampling limits and query_remote_graph for required statistics.

        First load remote-graph-qa using get_skill_instructions for this task.
        """
        try:
            uid = identity(run_context)
            return await executor.describe(uid, source, labels, relationship_types)
        except (ControlError, ValidationError, ValueError) as exc:
            return error(exc)

    # Agno treats Any as an empty object schema; bare dict preserves arbitrary
    # JSON values in the model schema. QueryRequest still validates at execution.
    async def query_remote_graph(query: str, run_context: RunContext, parameters: dict | None = None,
                                 save_result: bool = False) -> dict[str, Any]:
        """Run a parameterized native query on the selected remote database; optionally save bounded JSON.

        First load remote-graph-qa using get_skill_instructions for this task.
        Pass parameters as a JSON object, e.g. {"label": 0}, with query using $label.
        """
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


def register_remote_graph_tools(executor, binding: RemoteBinding | None) -> list:
    """Advertise optional structured values by omission for model compatibility.

    Runtime callables retain their nullable arguments and strict shape checks.
    Keeping this local avoids changing schemas for unrelated Agent tools.
    """
    from agno.tools.function import Function

    functions = [Function.from_callable(fn) for fn in create_remote_graph_tools(executor, binding)]
    for function in functions:
        properties = function.parameters['properties']
        for name in ('parameters', 'labels', 'relationship_types'):
            if name in properties:
                properties[name] = next(option for option in properties[name]['anyOf'] if option['type'] != 'null')
    return functions
