"""Agent-maintained notes cannot overwrite identity, dataset or execution state."""
import json
from agno.run import RunContext
from agent_runtime.execution_scope import current_scope

def update_agent_notes(notes: dict, run_context: RunContext) -> str:
    """Replace concise working notes in the agent_notes namespace. Do not store raw tool output or secrets."""
    scope = current_scope.get()
    if scope:
        scope.check()
    encoded = json.dumps(notes, ensure_ascii=False)
    if len(encoded.encode()) > 8192:
        return json.dumps({"success": False, "error": "工作笔记不能超过 8KB"}, ensure_ascii=False)
    if run_context.session_state is None:
        run_context.session_state = {}
    run_context.session_state["agent_notes"] = json.loads(encoded)
    return json.dumps({"success": True, "agent_notes": notes}, ensure_ascii=False)
