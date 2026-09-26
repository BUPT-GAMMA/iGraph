import json

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
    result = json.loads(reader.entrypoint('remote-graph-qa'))
    assert result['instructions'] == skill.instructions
    assert skills.get_skill('graph-rag') is not None
