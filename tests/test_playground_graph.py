import unittest

from pydantic import ValidationError
from graph_data_service.playground import Scenario, _reply_prefix, expand_graph


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
            Scenario.model_validate(value)
        value = scenario()
        value["relations"][0]["target"] = "Unknown"
        with self.assertRaises(ValidationError):
            Scenario.model_validate(value)

    def test_stream_only_exposes_visible_reply(self):
        self.assertEqual(_reply_prefix('{"reply":"校'), "校")
        self.assertEqual(_reply_prefix('{"reply":"校园","action":"propose","plan":{}'), "校园")
        self.assertEqual(_reply_prefix('{"plan":{}'), "")
