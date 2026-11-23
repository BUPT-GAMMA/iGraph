import unittest
from unittest.mock import patch

from pydantic import ValidationError
from graph_data_service.playground import Decision, Scenario, _reply_prefix, expand_graph, planner_tool_catalog
from graph_data_service.tool_catalog import INTERNAL_TOOLS, get_all_public_tools


def scenario():
    return {"title": "Research collaborations", "summary": "Fictional campus network",
            "questions": [{"en": "Which lab connects the most teams?", "zh": "哪个实验室连接的团队最多？",
                           "tool": "node_centrality"}],
            "entities": [{"type": "Team", "examples": ["Cedar", "Maple"], "share": 5},
                         {"type": "Lab", "examples": ["Quartz", "Onyx"], "properties": {"capacity": [20, 40]}}],
            "relations": [{"type": "Collaborates in", "source": "Team", "target": "Lab",
                           "properties": {"weight": [1, 3, 5]}}]}


class PlaygroundGraphTest(unittest.TestCase):
    def test_grounded_graph_is_reproducible(self):
        plan = Scenario.model_validate(scenario())
        graph = expand_graph(plan, "scene:one")
        self.assertEqual(graph, expand_graph(plan, "scene:one"))
        self.assertNotEqual(graph, expand_graph(plan, "scene:two"))
        nodes = {n["id"]: n for n in graph["nodes"]}
        self.assertEqual(len(nodes), 100)
        self.assertTrue(all(n["properties"]["name"] for n in nodes.values()))
        self.assertGreater(len(graph["edges"]), 100)
        for edge in graph["edges"]:
            self.assertEqual(nodes[edge["source"]]["type"], "Team")
            self.assertEqual(nodes[edge["target"]]["type"], "Lab")
            self.assertIn(edge["properties"]["weight"], [1, 3, 5])
        self.assertEqual(len({(e["source"], e["target"]) for e in graph["edges"]}), len(graph["edges"]))

    def test_unknown_tool_and_dangling_relation_rejected(self):
        value = scenario()
        value["questions"][0]["tool"] = "imaginary"
        with self.assertRaises(ValidationError):
            Decision.model_validate({"action": "propose", "reply": "Review the questions.", "plan": value},
                                    context={"tools": planner_tool_catalog({})})
        value = scenario()
        value["relations"][0]["target"] = "Unknown"
        with self.assertRaises(ValidationError):
            Scenario.model_validate(value)

    def test_planner_knows_all_registered_tools_and_checks_availability(self):
        native = {"workspace_list": {"category": "workspace", "description": "List workspace files."}}
        with patch.dict("os.environ", {"MCP_ENABLE_REPORTING_TOOLS": "false"}):
            tools = planner_tool_catalog(native)
        expected = {t["name"] for t in [*get_all_public_tools(include_reporting=True), *INTERNAL_TOOLS]}
        self.assertEqual(set(tools), expected | native.keys())
        plan = scenario()
        for name in ("get_node_type_counts", "triangle_count", "finance_control_chain", "get_constructed_graph", "train"):
            plan["questions"][0]["tool"] = name
            self.assertEqual(Scenario.model_validate(plan, context={"tools": tools}).questions[0].tool, name)
        for name in ("register_session", "set_session_config", "create_analysis_report"):
            plan["questions"][0]["tool"] = name
            with self.assertRaises(ValidationError):
                Scenario.model_validate(plan, context={"tools": tools})
        # A previously approved question stays readable if its tool is disabled later.
        self.assertEqual(Scenario.model_validate(plan).questions[0].tool, "create_analysis_report")

    def test_low_share_entities_keep_every_example_referenced_in_questions(self):
        value = scenario()
        value["entities"][0]["share"] = 100
        names = ["Quartz", "Onyx", "Jade", "Amber", "Slate", "Flint"]
        value["entities"][1].update(share=1, examples=names)
        graph = expand_graph(Scenario.model_validate(value), "rare labs")
        self.assertEqual(len(graph["nodes"]), 100)
        self.assertTrue(set(names) <= {n["properties"]["name"] for n in graph["nodes"] if n["type"] == "Lab"})

    def test_stream_only_exposes_visible_reply(self):
        self.assertEqual(_reply_prefix('{"reply":"校'), "校")
        self.assertEqual(_reply_prefix('{"reply":"校园","action":"propose","plan":{}'), "校园")
        self.assertEqual(_reply_prefix('{"plan":{}'), "")
