import json
from agno.models.message import Message
from agent_runtime.kinship_review import review_request


def messages():
    return [
        Message(role="system", content="Answer in English."),
        Message(role="user", content="Explain the family."),
        Message(
            role="assistant",
            tool_calls=[
                {"id": "k", "function": {"name": "inspect_kinship", "arguments": "{}"}}
            ],
        ),
        Message(
            role="tool",
            tool_call_id="k",
            tool_name="inspect_kinship",
            content=json.dumps(
                {
                    "success": True,
                    "facts": [
                        {
                            "source_name": "G",
                            "target_name": "C",
                            "relation": "祖母",
                            "kind": "biological_grandparent",
                            "role": "paternal_grandmother",
                        }
                    ],
                    "biological_ancestry": {
                        "C": {
                            "name": "C",
                            "ancestors": [
                                {
                                    "name": "G",
                                    "generations": 2,
                                    "relationship_to_subject": "祖母",
                                }
                            ],
                        }
                    },
                    "pair_checks": [],
                }
            ),
        ),
    ]


def test_current_turn_and_machine_output_boundaries():
    assert review_request(messages(), "draft") is not None
    assert (
        review_request(
            messages() + [Message(role="user", content="financial analysis")], "draft"
        )
        is None
    )
    assert review_request(messages(), "draft", {"type": "json_object"}) is None
    assert review_request([Message(role="user", content="no graph")], "draft") is None


def test_review_includes_current_algorithm_membership_and_rank_evidence():
    from agent_runtime.kinship_review import analysis_evidence

    ms = messages() + [
        Message(
            role="tool",
            tool_name="community_detection",
            content=json.dumps(
                {
                    "success": True,
                    "algorithm": "louvain",
                    "num_communities": 2,
                    "communities": [
                        {
                            "community_id": 1,
                            "size": 3,
                            "nodes_preview": ["n"],
                            "nodes_truncated": True,
                        }
                    ],
                    "focus_node_communities": [{"node": "n", "community_id": 1}],
                    "node_labels": {"n": "Alice"},
                }
            ),
        )
    ]
    evidence = json.loads(review_request(ms, "draft")[0][-1].content)["ledger"][
        "analysis_results"
    ]
    assert evidence[0]["communities"][0]["nodes_preview"] == ["Alice"]
    assert evidence[0]["communities"][0]["nodes_truncated"] is True
    assert evidence[0]["focus_node_communities"][0]["node"] == "Alice"
    assert analysis_evidence(ms + [Message(role="user", content="next graph")]) == []

def test_blank_tool_arguments_do_not_accept_nonobject_json_arguments():
    from agent_runtime.model_history import normalize_history
    for invalid in ([], 0, False, "[]", "0"):
        ms = messages()
        ms[2].tool_calls[0]["function"]["arguments"] = invalid
        assert normalize_history(ms)[2].tool_calls is None
