import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from backend.playground_context import MAX_NOTES, ScenarioNote
from graph_data_service.playground import (
    Decision, Scenario, _planner_options, _reply_prefix, _validated_decision, expand_graph,
    planner_tool_catalog, requested_question_count,
)
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

    def test_only_explicit_totals_constrain_the_question_count(self):
        cases = {"I need two topology questions: first count triangles.": 2,
                 "只保留两道题，确认前先不要构图。": 2, "请先给四个能用图回答的问题。": 4,
                 "我想要两个节点和三个关系。": None, "Add two more questions.": None,
                 "Suggest two or three questions.": None, "Don't suggest two questions.": None,
                 "先不要生成四道题。": None}
        for message, count in cases.items():
            with self.subTest(message=message):
                self.assertEqual(requested_question_count(message), count)

    def test_flash_planner_reserves_its_budget_for_json_on_supported_endpoints(self):
        model = "deepseek-v4-flash-0731-ga"
        options = _planner_options(model, "https://tokensave.pro/v1")
        self.assertEqual(options["extra_body"]["thinking"], {"type": "disabled"})
        self.assertEqual(options["model"], model)
        self.assertNotIn("extra_body", _planner_options("custom", "https://tokensave.pro/v1"))
        self.assertNotIn("extra_body", _planner_options(model, "https://relay.example/v1"))

    def test_context_preserves_unchanged_user_facts_and_replaces_rejected_topics(self):
        notes = [ScenarioNote(kind="constraint", text="No order volumes", quote="没有订单量"),
                 ScenarioNote(kind="question_choice", text="Compare costs", quote="想比较成本")]
        message = "去掉成本题，只看站点停运影响，保留第一题。"
        validation = {"notes": notes, "user_messages": [message]}
        update = {"retire": [2], "remember": [
            {"kind": "question_choice", "text": "Exclude the cost comparison", "quote": "去掉成本题"},
            {"kind": "goal", "text": "Assess station closures", "quote": "只看站点停运影响"}]}
        reply = {"action": "clarify", "reply": "要看哪一站？", "context_update": update}
        output = Decision.model_validate(reply, context=validation)._notes
        self.assertEqual(output[0], notes[0])
        self.assertNotIn(notes[1], output)
        self.assertEqual(output[-1].quote, "只看站点停运影响")
        unchanged = {"action": "clarify", "reply": "继续", "context_update": {"retire": [], "remember": []}}
        for _ in range(20):
            output = Decision.model_validate(unchanged, context={"notes": output, "user_messages": ["继续"]})._notes
        self.assertEqual([n.text for n in output], ["No order volumes", "Exclude the cost comparison", "Assess station closures"])
        with self.assertRaises(ValidationError):
            Decision.model_validate({**reply, "action": "confirm"}, context=validation)

    def test_context_rejects_invented_evidence_invalid_ids_and_overflow(self):
        note = {"kind": "constraint", "text": "No transport costs", "quote": "没有运费"}
        reply = {"action": "clarify", "reply": "哪些字段可以用？"}
        def parse(previous, delete, add):
            return Decision.model_validate({**reply, "context_update": {"retire": delete, "remember": add}},
                                           context={"notes": previous, "user_messages": ["没有运费"]})
        for retire, remember in [([3], []), ([], [{**note, "quote": "没有里程"}])]:
            with self.subTest(retire=retire, remember=remember), self.assertRaises(ValidationError):
                parse([], retire, remember)
        full = [ScenarioNote(**{**note, "text": f"User fact {i}"}) for i in range(MAX_NOTES)]
        with self.assertRaises(ValidationError):
            parse(full, [], [note])
        self.assertEqual(len(parse(full, [1], [note])._notes), MAX_NOTES)

    def test_repair_count_once_and_validate_the_repaired_plan(self):
        plan = scenario()
        expected = {"action": "propose", "reply": "Review this question.", "plan": plan}
        correct = json.dumps(expected)
        plan["questions"].append({"zh": "哪些团队合作？", "en": "Which teams collaborate?", "tool": "get_neighbors"})
        wrong = json.dumps(expected)
        reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=correct))])
        create = AsyncMock(return_value=reply)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        context = {"tools": planner_tool_catalog({}), "question_count": 1}
        request = {"latest_request": "One question about teams."}
        decision = asyncio.run(_validated_decision(wrong, client, {}, request, context))
        self.assertEqual(len(decision.plan.questions), 1)
        create.assert_awaited_once()
        prompt = json.loads(create.call_args.kwargs["messages"][-1]["content"])
        self.assertEqual(prompt["latest_request"], request["latest_request"])
        self.assertIn("exactly 1", prompt["internal_validation"]["issues"][0]["issue"])
        asyncio.run(_validated_decision('{"reply":', client, {}, request, context))
        truncated = json.loads(create.call_args.kwargs["messages"][-1]["content"])
        self.assertNotIn("rejected_draft", truncated["internal_validation"])
        create.reset_mock()
        self.assertEqual(asyncio.run(_validated_decision(correct, client, {}, request, context)), decision)
        create.assert_not_awaited()
        reply.choices[0].message.content = wrong
        with self.assertRaises(ValidationError):
            asyncio.run(_validated_decision(wrong, client, {}, request, context))
        create.assert_awaited_once()

    def test_stream_only_exposes_visible_reply(self):
        self.assertEqual(_reply_prefix('{"reply":"校'), "校")
        self.assertEqual(_reply_prefix('{"reply":"校园","action":"propose","plan":{}'), "校园")
        self.assertEqual(_reply_prefix('{"plan":{}'), "")
