"""Keep frontend labels complete as the public tool catalog evolves."""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

from agno.skills import Skills
from agent_runtime.agent_notes import update_agent_notes
from agent_runtime.learning import create_learning_machine
from agent_runtime.workspace_tools import create_workspace_tools
from graph_engine.tool_catalog import get_all_public_tools


def test_chinese_labels_cover_registered_graph_and_runtime_tools():
    labels = json.loads((Path(__file__).resolve().parents[1] / "frontend/src/toolLabels.json").read_text())
    names = {tool["name"] for tool in get_all_public_tools(include_graph_ml=True, include_reporting=True)}
    names.update(tool.__name__ for tool in create_workspace_tools())
    names.add(update_agent_notes.__name__)
    names.update(tool.name for tool in Skills(loaders=[]).get_tools())
    # Enumerate the application's native learning tools without invoking them,
    # connecting to storage, or making model requests.
    machine = create_learning_machine(SimpleNamespace(embedding_model="", embedding_api_key=""), planning_mode="auto")
    names.update(getattr(tool, "name", None) or tool.__name__
        for tool in asyncio.run(machine.aget_tools(user_id="labels", session_id="labels")))
    assert not names - labels.keys(), f"Missing Chinese tool labels: {sorted(names - labels.keys())}"
    assert all(isinstance(label, str) and re.search(r"[\u4e00-\u9fff]", label) for label in labels.values())
