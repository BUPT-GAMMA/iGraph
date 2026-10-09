"""Runtime discovery and callable-contract checks for the ownership workflow."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_runtime.config import AgentRuntimeConfig
from graph_data_service.tool_catalog import get_all_public_tools


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "finance-ownership-workflow"
SKILL_PATH = ROOT / "agent_runtime" / "resources" / "skills" / SKILL_NAME / "SKILL.md"


def _documented_calls():
    text = SKILL_PATH.read_text(encoding="utf-8")
    return [json.loads(block) for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)]


@pytest.mark.parametrize("call", _documented_calls(), ids=lambda call: call["tool"])
def test_workflow_calls_match_published_tool_contracts(call):
    """A skill recipe must remain callable when the actual tool schemas evolve."""
    catalog = {tool["name"]: tool for tool in get_all_public_tools()}
    spec = catalog[call["tool"]]["inputSchema"]
    arguments = call["arguments"]
    properties = spec["properties"]
    assert set(spec.get("required", [])) <= arguments.keys()
    assert arguments.keys() <= properties.keys()
    for name, value in arguments.items():
        parameter = properties[name]
        if isinstance(value, int):
            assert parameter.get("minimum", value) <= value <= parameter.get("maximum", value)
        if "enum" in parameter:
            assert value in parameter["enum"]


def test_finance_workflow_is_discovered_from_runtime_bundle(tmp_path, monkeypatch):
    """Use the same materialization and Agno loader as the deployed factory."""
    agno_skills = pytest.importorskip("agno.skills", reason="Agno runtime dependencies required")
    from agent_runtime.skills_runtime import bundled_skills_path

    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_RUNTIME_SKILLS_ROOT", str(tmp_path / "skills"))
    monkeypatch.setenv("GRAPH_ENGINE_MCP_URL", "http://127.0.0.1:21110/mcp")
    config = AgentRuntimeConfig.from_env()
    bundled_skills_path.cache_clear()
    try:
        bundle = bundled_skills_path(config)
        loaded = agno_skills.LocalSkills(str(bundle)).load()
        found = [skill for skill in loaded if skill.name == SKILL_NAME]
        assert len(found) == 1
        assert found[0].description
        assert found[0].instructions
        assert (bundle / SKILL_NAME / "SKILL.md").is_file()
        assert all(call["tool"] in found[0].instructions for call in _documented_calls())
    finally:
        bundled_skills_path.cache_clear()


def test_workflow_does_not_embed_acceptance_dataset_answers():
    """Bundled instructions must also work for previously unseen equity graphs."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert not re.search(r"SYN-(?:ENT|IDV)-\d+", text)
    assert "finance-validation" not in text
    assert "tasks.pptx" not in text
