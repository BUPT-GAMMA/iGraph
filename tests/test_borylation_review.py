import json
from types import SimpleNamespace
from agno.models.message import Message
from agent_runtime.borylation_review import borylation_evidence, review_request


def messages():
    return [Message(role="system", content="Use English for this turn."),
        Message(role="user", content="How many distinct sites?"),
        Message(role="tool", tool_name="borylation_case_analysis", tool_call_id="b",
            content=json.dumps({"success": True, "step": "symmetry", "result": {"distinct_site_count": 6, "matrix": ["large"]},
                "structure_summary": {"per_ring_sites": [{"ring_id": 1, "distinct_site_count": 2}, {"ring_id": 2, "distinct_site_count": 1}, {"ring_id": 3, "distinct_site_count": 3}]}}))]

def test_current_turn_only_and_machine_output_boundaries():
    ms = messages()
    request, count = review_request(ms, "wrong draft")
    assert count == 1
    ledger = json.loads(request[-1].content)["ledger"]
    assert "matrix" not in ledger[0]["result"]
    assert [r["distinct_site_count"] for r in ledger[0]["structure_summary"]["per_ring_sites"]] == [2,1,3]
    assert request[0].content == "Use English for this turn."
    assert review_request(ms + [Message(role="user", content="Finance?")], "draft") is None
    assert review_request(ms, "draft", {"type":"json_object"}) is None
    assert not borylation_evidence([Message(role="tool", tool_name="borylation_case_analysis", content='{"success":false}')])


def test_each_verified_case_turn_requires_a_fresh_read_without_looping():
    from agent_runtime.borylation_review import evidence_tool_choice
    run = SimpleNamespace(session_state={"active_dataset_id": "chem", "data_state": {
        "graph_background": {"case_id": "borylation", "dataset_id": "chem"}}})
    tools = [{"type": "function", "function": {"name": "borylation_case_analysis"}}]
    old = messages() + [Message(role="user", content="Compare those conditions")]
    forced = evidence_tool_choice(old, tools, None, run)
    assert forced["function"]["name"] == "borylation_case_analysis"
    assert evidence_tool_choice(messages(), tools, None, run) is None
    failed = old + [Message(role="tool", tool_name="borylation_case_analysis", content='{"success":false}')]
    assert evidence_tool_choice(failed, tools, None, run) is None
    assert evidence_tool_choice(old, tools, "none", run) == "none"
    assert evidence_tool_choice(old, [], None, run) is None
    assert evidence_tool_choice(old, tools, None, run, {"type":"json_object"}) is None
    run.session_state["active_dataset_id"] = "novel"
    assert evidence_tool_choice(old, tools, None, run) is None
    run.session_state["data_state"]["graph_background"] = None
    assert evidence_tool_choice(old, tools, None, run) is None

def test_evidence_gate_uses_current_agno_system_state_before_output_is_saved():
    from agent_runtime.borylation_review import evidence_tool_choice
    state = {"active_dataset_id": "chem", "data_state": {
        "graph_background": {"case_id": "borylation", "dataset_id": "chem"}}}
    tools = [{"type":"function","function":{"name":"borylation_case_analysis"}}]
    context = Message(role="system",content="Rules\n<session_state>\n"+repr(state)+"\n</session_state>")
    ms = [context, Message(role="user",content="Compare successful conditions")]
    assert evidence_tool_choice(ms,tools,None,SimpleNamespace(session_state=None))["function"]["name"] == "borylation_case_analysis"
    # A user's copy of the marker is never a trusted binding.
    context.role = "user"
    assert evidence_tool_choice(ms,tools,None,None) is None

def test_current_successful_rendering_is_retained_for_review():
    from agent_runtime.borylation_review import rendering_receipts
    render = Message(role="tool",tool_name="visualize_subgraph",content=json.dumps({
        "success":True,"num_nodes":22,"nodes":["large canvas"],"highlights":{"nodes":[{"ids":["BO-01:atom:8"]}]}}))
    ms = messages()+[render]
    request, _ = review_request(ms,"Sites highlighted")
    receipts = json.loads(request[-1].content)["rendering_receipts"]
    assert receipts[0]["highlights"]["nodes"][0]["ids"] == ["BO-01:atom:8"]
    assert "nodes" not in receipts[0]
    assert not rendering_receipts(ms+[Message(role="user",content="Another question")])
    assert not rendering_receipts([Message(role="tool",tool_name="visualize_subgraph",content='{"success":false}')])
