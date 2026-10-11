import json
from types import SimpleNamespace
import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.metrics import MessageMetrics
from agent_runtime.model_history import ModelHistoryMixin
from agent_runtime.kinship_review import review_request


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


class Provider:
    calls = []

    async def ainvoke_stream(self, *args, **kwargs):
        yield ModelResponse(content="WRONG DRAFT")
        yield ModelResponse(
            response_usage=MessageMetrics(input_tokens=100, output_tokens=20)
        )

    async def ainvoke(self, ms, assistant, response_format, tools, *args, **kwargs):
        self.calls.append((ms, tools))
        assert tools is None
        return ModelResponse(
            content="Conclusion: Correct paternal grandmother.\nSummary: Confirmed.",
            response_usage=MessageMetrics(input_tokens=40, output_tokens=10),
            provider_data={"signed": "draft"},
            reasoning_content="review scratchpad",
        )


class Model(ModelHistoryMixin, Provider):
    id = "model"


@pytest.mark.anyio
async def test_streamed_draft_is_not_exposed_and_review_uses_same_run_evidence():
    model = Model()
    model.calls = []
    run = SimpleNamespace(metadata={})
    events = [
        e
        async for e in model.ainvoke_stream(
            messages(), Message(role="assistant"), run_response=run
        )
    ]
    text = "".join(e.content or "" for e in events)
    assert "WRONG DRAFT" not in text and text.startswith("Conclusion:")
    assert (
        len(model.calls) == 1
        and run.metadata["kinship_answer_review"]["fact_count"] == 1
    )
    review_ms = model.calls[0][0]
    assert any(m.content == "Answer in English." for m in review_ms)
    ledger = json.loads(review_ms[-1].content)["ledger"]
    assert ledger["verified_ancestors"][0]["ancestor_is_persons"] == "祖母"
    assert sum(e.response_usage.input_tokens for e in events if e.response_usage) == 140
    assert all(not e.provider_data and not e.reasoning_content for e in events)


@pytest.mark.anyio
async def test_tool_calls_are_forwarded_without_extra_review_or_execution():
    class ToolProvider(Provider):
        async def ainvoke_stream(self, *args, **kwargs):
            yield ModelResponse(
                tool_calls=[
                    {
                        "id": "v",
                        "function": {"name": "visualize_subgraph", "arguments": "{}"},
                    }
                ]
            )

    class ToolModel(ModelHistoryMixin, ToolProvider):
        id = "tool-model"

    model = ToolModel()
    model.calls = []
    result = [
        e async for e in model.ainvoke_stream(messages(), Message(role="assistant"))
    ]
    assert result[0].tool_calls[0]["id"] == "v" and model.calls == []


@pytest.mark.anyio
async def test_failed_review_never_falls_back_to_unchecked_draft():
    class FailedProvider(Provider):
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    class FailedModel(ModelHistoryMixin, FailedProvider):
        id = "failed"

    emitted = []
    with pytest.raises(RuntimeError, match="provider unavailable"):
        async for event in FailedModel().ainvoke_stream(
            messages(), Message(role="assistant")
        ):
            emitted.append(event)
    assert not emitted


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


@pytest.mark.anyio
async def test_nonstream_review_counts_both_requests_and_does_not_recurse():
    class NonstreamProvider(Provider):
        async def ainvoke(self, ms, assistant, response_format, tools, *args, **kwargs):
            self.calls.append((ms, tools))
            return ModelResponse(
                content="draft" if len(self.calls) == 1 else "checked",
                response_usage=MessageMetrics(input_tokens=100, output_tokens=10),
            )

    class NonstreamModel(ModelHistoryMixin, NonstreamProvider):
        id = "nonstream"

    model = NonstreamModel()
    model.calls = []
    result = await model.ainvoke(messages(), Message(role="assistant"))
    assert result.content == "checked" and len(model.calls) == 2
    assert result.response_usage.input_tokens == 200


@pytest.mark.anyio
async def test_real_empty_argument_call_keeps_evidence_after_history_normalization():
    from agent_runtime.model_history import prepare_history

    ms = messages()
    ms[2].tool_calls[0]["function"]["arguments"] = ""
    prepared, _ = prepare_history(ms)
    assert next(m for m in prepared if m.tool_name == "inspect_kinship").role == "tool"
    assert prepared[2].tool_calls[0]["function"]["arguments"] == "{}"
    model = Model()
    model.calls = []
    model.igraph_capabilities = {
        "context_window": 300000,
        "max_output_tokens": 4000,
        "vision": False,
    }
    model.igraph_protocol = "anthropic"
    run = SimpleNamespace(metadata={})
    events = [
        e
        async for e in model.ainvoke_stream(
            ms, Message(role="assistant"), run_response=run
        )
    ]
    assert run.metadata["kinship_answer_review"]["performed"]
    assert "WRONG DRAFT" not in "".join(e.content or "" for e in events)
    assert len(model.calls) == 1


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

@pytest.mark.anyio
async def test_community_answer_is_reviewed_even_without_kinship_tool_call():
    ms = [Message(role="system", content="Answer in Chinese."),
          Message(role="user", content="Who belongs to each community?"),
          Message(role="tool", tool_name="community_detection", content=json.dumps({
              "success": True, "algorithm": "louvain", "communities": [
                  {"community_id": 1, "size": 2, "nodes_preview": ["a", "b"]}],
              "node_labels": {"a": "Alice", "b": "Bob"}}))]
    assert review_request(ms, "Alice and Bob are separate") is not None
    model = Model()
    model.calls = []
    run = SimpleNamespace(metadata={})
    events = [e async for e in model.ainvoke_stream(ms, Message(role="assistant"), run_response=run)]
    assert run.metadata["kinship_answer_review"]["performed"]
    assert "WRONG DRAFT" not in "".join(e.content or "" for e in events)
    assert len(model.calls) == 1
