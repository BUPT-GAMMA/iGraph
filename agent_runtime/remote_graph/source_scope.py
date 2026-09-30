"""Require a human source reply before local writes beside a remote selection.

The grant belongs to one native run, including its existing approval resume.
Pending questions are native session data, outside model/client session_state,
and only a completed prior question can be answered. No tool argument, history
text or metadata grants it.
"""
from copy import deepcopy

from agno.exceptions import StopAgentRun
from agno.run.base import RunStatus
from graph_engine.tool_execution import execution_policy


QUESTION = '你指的是当前本地图还是远程数据库？请回复“本地图”或“远程数据库”。'
_PENDING = 'igraph_pending_source_question'
_GRANT = 'igraph_local_source_grant'


def _local_reply(content):
    # A closed set of complete affirmative replies, never substring matching
    # or interpretation by the model. Question marks remain ambiguous.
    return isinstance(content, str) and content.strip().rstrip('。！') in {
        '本地图', '当前本地图', '我指的是本地图', '我指的是当前本地图',
    }


def _local_write(name, arguments=None):
    return any(resource in {'graph', 'dataset_binding'} or resource.startswith('construction:')
               for resource in execution_policy(name, arguments).writes)


def source_scope_hooks(binding, db):
    """Return request-scoped native hooks; absent remote selections are inert."""
    source = deepcopy(binding)
    allowed = False
    session = None
    scope = None
    confirmation_settings = []
    questioned = False

    def current_scope(run_context):
        state = run_context.session_state or {}
        data = state.get('data_state') or {}
        result = dict(user_id=run_context.user_id, session_id=run_context.session_id,
                      remote=source, local={key: deepcopy(data.get(key)) for key in
                      ('active_dataset_id', 'construction_revision', 'graph_version')})
        result['local']['active_dataset_id'] = state.get('active_dataset_id', data.get('active_dataset_id'))
        return result

    async def prepare_source_scope(run_input, run_context, agent, session):
        nonlocal allowed, scope, questioned
        questioned = False
        for target, attribute, value in confirmation_settings:
            setattr(target, attribute, value)
        confirmation_settings.clear()
        # Keep the native session object, never a model-writable state pointer.
        capture_session(session)
        session.session_data = session.session_data or {}
        pending = session.session_data.pop(_PENDING, None)
        session.session_data.pop(_GRANT, None)
        scope = current_scope(run_context)
        # AgentOS background SSE persists the current RUNNING placeholder
        # before pre-hooks. Only that same run is excluded; an intervening
        # different run still invalidates the immediately-next-turn reply.
        previous = next((run for run in reversed(session.runs or [])
                         if run.run_id != run_context.run_id), None)
        allowed = bool(source and isinstance(pending, dict)
                       and pending.get('scope') == scope and previous is not None
                       and pending.get('run_id') == previous.run_id
                       and previous.status == RunStatus.completed
                       and isinstance(previous.content, str) and QUESTION in previous.content
                       and _local_reply(run_input.input_content))
        if allowed:
            # Native approval continuation builds a new Agent and does not run
            # pre-hooks. Retain only this exact run's server-issued scope.
            session.session_data[_GRANT] = dict(scope=deepcopy(scope), run_id=run_context.run_id)
        # Agno checks native confirmations before invoking tool hooks. Let an
        # unresolved local write reach our source question first. Explicitly
        # granted local runs retain the existing confirmation configuration.
        if source and not allowed:
            for toolkit in agent.tools or []:
                if hasattr(toolkit, 'requires_confirmation_tools'):
                    confirmation_settings.append((toolkit, 'requires_confirmation_tools', toolkit.requires_confirmation_tools))
                    toolkit.requires_confirmation_tools = [name for name in toolkit.requires_confirmation_tools
                                                           if not _local_write(name)]
                functions = getattr(toolkit, 'functions', {}).values()
                for function in [toolkit, *functions]:
                    if getattr(function, 'requires_confirmation', False) and _local_write(function.name):
                        confirmation_settings.append((function, 'requires_confirmation', True))
                        function.requires_confirmation = False

    def capture_session(value):
        nonlocal session
        session = value

    async def guard_source_scope(function_name, function_call, arguments, run_context):
        nonlocal questioned, allowed
        if source and session is None and not allowed and _local_write(function_name, arguments):
            saved = db.get_session(session_id=run_context.session_id, user_id=run_context.user_id)
            grant = (saved.session_data or {}).get(_GRANT) if saved else None
            paused = saved.get_run(run_context.run_id) if saved else None
            allowed = bool(isinstance(grant, dict) and grant.get('run_id') == run_context.run_id
                           and paused is not None and paused.status == RunStatus.paused
                           and grant.get('scope') == current_scope(run_context))
        if source and not allowed and _local_write(function_name, arguments):
            if session is not None and scope is not None:
                session.session_data[_PENDING] = dict(scope=deepcopy(scope), run_id=run_context.run_id)
            message = None if questioned else QUESTION
            questioned = True
            raise StopAgentRun('Local graph source requires a human reply', agent_message=message)
        return await function_call(**arguments)

    return prepare_source_scope, guard_source_scope
