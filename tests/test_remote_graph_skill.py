import json

from agno.models.message import Message
from agno.models.openai.like import OpenAILike

from tests.test_skill_api import skill_client
from agent_runtime.skills_runtime import build_skills


def test_remote_skill_is_discovered_and_retrievable_without_local_graph(skill_client):
    _, config = skill_client
    skills = build_skills(config, 'alice')
    skill = skills.get_skill('remote-graph-qa')
    assert skill is not None
    assert skill.metadata['igraph']['any_tools'] == ['describe_remote_graph', 'query_remote_graph']
    assert skill.metadata['igraph']['requires_graph_state'] == 'none'
    reader = next(tool for tool in skills.get_tools() if tool.name == 'get_skill_instructions')
    raw = reader.entrypoint('remote-graph-qa')
    result = json.loads(raw)
    assert result['instructions'] == skill.instructions
    # The model reads the tool message itself, not json.loads(tool_message).
    # Preserve readable Chinese through the actual provider message formatter.
    message = OpenAILike(id='test')._format_message(
        Message(role='tool', tool_call_id='skill-read', content=raw)
    )
    assert '远程图数据库问答' in message['content']
    assert json.loads(message['content']) == result
    assert skills.get_skill('graph-rag') is not None
