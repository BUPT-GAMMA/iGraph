from __future__ import annotations

import unittest
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from agent_runtime.approval_policy import GRAPH_TOOLS_REQUIRING_CONFIRMATION
from agent_runtime.agent.skills.paths import bundled_skills_dir
from agent_runtime.agent.skills.registry import SkillRegistry


class GraphToolApprovalPolicyTest(unittest.TestCase):
    def test_only_destructive_clear_requires_confirmation(self) -> None:
        self.assertEqual(
            GRAPH_TOOLS_REQUIRING_CONFIRMATION,
            frozenset({"clear_graph"}),
        )
        self.assertNotIn("train", GRAPH_TOOLS_REQUIRING_CONFIRMATION)
        self.assertNotIn("infer", GRAPH_TOOLS_REQUIRING_CONFIRMATION)

    def test_native_mcp_tools_use_shared_policy(self) -> None:
        calls: list[dict] = []
        agno = ModuleType("agno")
        agno_run = ModuleType("agno.run")
        agno_tools = ModuleType("agno.tools")
        agno_tools_mcp = ModuleType("agno.tools.mcp")

        class FakeRunContext:
            pass

        class FakeMCPTools:
            def __init__(self, **kwargs) -> None:
                calls.append(kwargs)

        agno_run.RunContext = FakeRunContext
        agno_tools_mcp.MCPTools = FakeMCPTools
        config = SimpleNamespace(mcp_url="http://graph-engine-mcp/mcp")

        with patch.dict(
            sys.modules,
            {
                "agno": agno,
                "agno.run": agno_run,
                "agno.tools": agno_tools,
                "agno.tools.mcp": agno_tools_mcp,
            },
        ):
            sys.modules.pop("agent_runtime.graph_tools", None)
            from agent_runtime.graph_tools import create_graph_tools

            create_graph_tools(config)

        self.assertEqual(
            calls[0]["requires_confirmation_tools"],
            ["clear_graph"],
        )

    def test_bundled_ml_skills_do_not_restore_confirmation(self) -> None:
        registry = SkillRegistry([Path(bundled_skills_dir())])

        high_risk_tools = registry.high_risk_tool_names()

        self.assertNotIn("train", high_risk_tools)
        self.assertNotIn("infer", high_risk_tools)


if __name__ == "__main__":
    unittest.main()
