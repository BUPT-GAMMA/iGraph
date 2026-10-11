import copy
import json
from types import SimpleNamespace
import httpx
import pytest
from agno.models.message import Message
from openai import AsyncOpenAI
from agent_runtime.model_history import normalize_history, prepare_history, context_window_usage, estimate_tokens, ModelHistoryMixin
from agent_runtime.model_factory import create_model


def test_cross_provider_tool_pairs_and_interrupted_turn_are_normalized_without_mutation():
    messages = [Message(role='user', content='analyse'), Message(role='assistant', reasoning_content='private reasoning',
        tool_calls=[{'id': 'foreign-1', 'type': 'function', 'signature': 'provider-only', 'function': {'name': 'measure', 'arguments': {'k': 1}}}]),
        Message(role='tool', tool_call_id='foreign-1', content='42'),
        Message(role='assistant', tool_calls=[{'id': 'lost', 'function': {'name': 'mutate', 'arguments': '{}'}}]),
        Message(role='user', content='continue')]
    before = copy.deepcopy(messages)
    normalized = normalize_history(messages)
    assert normalized[1].tool_calls == [{'id': 'foreign-1', 'type': 'function', 'function': {'name': 'measure', 'arguments': '{"k": 1}'}}]
    assert normalized[3].tool_calls is None
    assert 'interrupted' in normalized[3].content
    assert messages == before


def test_context_compaction_keeps_current_turn_and_full_persisted_history():
    messages = [Message(role='system', content='Respond in English.')]
    for i in range(8):
        messages += [Message(role='user', content=f'question-{i} ' + 'x' * 1000), Message(role='assistant', content='answer ' + 'y' * 1000)]
    messages += [Message(role='user', content='current question')]
    before = copy.deepcopy(messages)
    prepared, compacted = prepare_history(messages, context_window=4096, output_tokens=1024)
    assert compacted and prepared[0].content == messages[0].content
    assert prepared[-1].content == 'current question'
    assert 'Compacted earlier conversation' in prepared[1].content
    assert messages == before


def test_current_input_too_large_fails_instead_of_truncating():
    with pytest.raises(ValueError, match='MODEL_CONTEXT_TOO_LARGE'):
        prepare_history([Message(role='user', content='x' * 9000)], context_window=2048, output_tokens=512)


def test_context_window_counts_one_request_and_reserves_tools_output_and_headroom():
    messages = [Message(role='system', content='instructions'), Message(role='user', content='分析人物')]
    tools = [{'name': 'get_node_info', 'description': 'Graph tool'}]
    settings = dict(context_window=10000, max_output_tokens=1000)
    usage = context_window_usage(messages, tools, settings)
    assert usage['tool_tokens'] == estimate_tokens(tools)
    assert usage['remaining_tokens'] == 10000 - usage['input_tokens'] - 1000 - 1000
    assert usage['remaining_ratio'] == usage['remaining_tokens'] / 10000
    assert context_window_usage(messages, tools, settings)['input_tokens'] == usage['input_tokens']
    assert context_window_usage([Message(role='user', content='x' * 50000)], tools, settings)['remaining_tokens'] == 0


def test_last_request_estimate_is_run_owned_and_persisted_even_on_overflow():
    from agent_runtime.execution_scope import RunScope, current_scope
    model = ModelHistoryMixin()
    model.id = 'upstream'
    model.igraph_capabilities = dict(model_id='config-a', context_window=10000, max_output_tokens=1000, vision=False)
    response = SimpleNamespace(metadata={'model_snapshot': {'config_id': 'config-a', 'revision': 2}})
    scope = RunScope('run-a', 'alice', 'A', 1)
    token = current_scope.set(scope)
    try:
        model._prepare_history([Message(role='user', content='short')], [], response)
        usage = response.metadata['context_window_usage']
        assert usage == scope.context_window_usage
        assert usage['config_id'] == 'config-a' and usage['model_revision'] == 2
        assert usage['remaining_tokens'] > 0
        with pytest.raises(ValueError, match='MODEL_CONTEXT_TOO_LARGE'):
            model._prepare_history([Message(role='user', content='x' * 50000)], [], response)
        assert scope.context_window_usage['remaining_tokens'] == 0
    finally:
        current_scope.reset(token)


def test_incompatible_media_is_not_silently_removed():
    message = Message(role='user', content=[{'type': 'image_url', 'image_url': {'url': 'https://example.org/image.png'}}])
    with pytest.raises(ValueError, match='MODEL_MEDIA_UNSUPPORTED'):
        prepare_history([message])
    assert prepare_history([message], vision=True)[0][0].content == message.content


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_native_agno_request_uses_upstream_id_and_strips_foreign_fields(monkeypatch):
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    captures = []
    def respond(req):
        captures.append(json.loads(req.content))
        return httpx.Response(200, json={'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1,
            'model': 'upstream-b', 'usage': {'prompt_tokens': 7000, 'completion_tokens': 10, 'total_tokens': 7010},
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})
    config = dict(model_id='custom-test', upstream_model_id='upstream-b', base_url='http://example.test/v1',
        api_key='', allow_anonymous=True, context_window=32768, max_output_tokens=1024, vision=False)
    model = create_model('custom-test', SimpleNamespace(), config)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model.async_client = AsyncOpenAI(api_key='local', base_url=config['base_url'], http_client=client)
        messages = [Message(role='user', content='old question'), Message(role='assistant', content='old answer', reasoning_content='DO NOT TRANSMIT'), Message(role='user', content='new question')]
        run = SimpleNamespace(metadata={})
        result = await model.ainvoke(messages, Message(role='assistant'), run_response=run)
    assert result.content == 'OK'
    assert captures[0]['model'] == 'upstream-b' and captures[0]['max_tokens'] == 1024
    assert 'DO NOT TRANSMIT' not in json.dumps(captures)
    assert len(captures[0]['messages']) == 3
    assert run.metadata['context_window_usage']['basis'] == 'provider_usage'
    assert run.metadata['context_window_usage']['input_tokens'] == 7000


@pytest.mark.anyio
async def test_stream_usage_replaces_estimate_and_never_accumulates_or_overwrites_another_run():
    from agent_runtime.execution_scope import RunScope, current_scope
    from agno.metrics import MessageMetrics
    from agno.models.response import ModelResponse
    class Provider:
        async def ainvoke_stream(self, *args, **kwargs):
            yield ModelResponse(response_usage=MessageMetrics(input_tokens=7500, cache_read_tokens=1000))
            yield ModelResponse(response_usage=MessageMetrics(input_tokens=7500, cache_read_tokens=1000))
            yield ModelResponse(response_usage=MessageMetrics(output_tokens=100))
    class Model(ModelHistoryMixin, Provider):
        id = 'test'
        igraph_protocol = 'anthropic'
        igraph_capabilities = dict(model_id='test', context_window=10000, max_output_tokens=1000, vision=False)
    model = Model()
    scope = RunScope('primary', 'admin', 'session-a', 1)
    token = current_scope.set(scope)
    try:
        primary = SimpleNamespace(metadata={}, run_id='primary')
        async for _ in model.ainvoke_stream([Message(role='user', content='test')], Message(role='assistant'), run_response=primary):
            pass
        usage = primary.metadata['context_window_usage']
        assert usage['input_tokens'] == 8500  # Includes cached context, not repeated stream usage.
        assert usage['remaining_tokens'] == 0
        assert usage['basis'] == 'provider_usage'
        assert scope.context_window_usage == usage
        background = SimpleNamespace(metadata={}, run_id='learning')
        model._prepare_history([Message(role='user', content='background')], [], background)
        model._record_provider_context_usage(ModelResponse(response_usage=MessageMetrics(input_tokens=100)), background)
        assert background.metadata['context_window_usage']['input_tokens'] == 100
        assert scope.context_window_usage == usage
        model._record_provider_context_usage(ModelResponse(), primary)
        assert primary.metadata['context_window_usage'] == usage
    finally:
        current_scope.reset(token)


@pytest.mark.parametrize('protocol', ['openai-responses', 'anthropic', 'gemini'])
def test_native_connection_switch_does_not_forward_foreign_signed_history(protocol):
    messages = [Message(role='user', content='Measure it'), Message(role='assistant', content='Checking',
        reasoning_content='foreign reasoning', provider_data={'igraph_history_key': 'other-connection', 'signature': 'foreign-signature'},
        tool_calls=[{'id': 'foreign-call', 'thought_signature': 'foreign-signature', 'function': {'name': 'measure', 'arguments': '{}'}}]),
        Message(role='tool', tool_call_id='foreign-call', tool_name='measure', content='Confirmed result: 42'),
        Message(role='user', content='Continue')]
    before = copy.deepcopy(messages)
    prepared = normalize_history(messages, protocol=protocol, history_key='current-connection')
    assert all(not m.tool_calls and m.role != 'tool' for m in prepared)
    assert prepared[2].content.endswith('Confirmed result: 42')
    assert all(m.provider_data is None and m.reasoning_content is None for m in prepared)
    assert 'foreign-signature' not in str([m.to_dict() for m in prepared])
    assert messages == before


def test_canvas_payload_is_bounded_for_model_but_persisted_graph_and_evidence_survive():
    raw = json.dumps({"success": True, "num_nodes": 304, "num_edges": 715,
                      "total_nodes": 304, "total_edges": 715, "truncated": False,
                      "nodes": [{"id": "a", "properties": {"data": "x" * 500000}}],
                      "edges": [{"relation": "生父", "properties": {"evidence": "novel"}}]})
    messages = [Message(role="system", content="rules"), Message(role="user", content="analyse"),
        Message(role="assistant", tool_calls=[{"id":"v1", "function":{"name":"visualize_subgraph", "arguments":"{}"}}]),
        Message(role="tool", tool_call_id="v1", tool_name="visualize_subgraph", content=raw),
        Message(role="assistant", tool_calls=[{"id":"k1", "function":{"name":"inspect_kinship", "arguments":"{}"}}]),
        Message(role="tool", tool_call_id="k1", tool_name="inspect_kinship",
                content='{"biological_father": "A", "nominal_father": "B", "proof": [1, 2]}')]
    before = copy.deepcopy(messages)
    prepared, compacted = prepare_history(messages, context_window=8192, output_tokens=1024)
    assert compacted and len(prepared[3].content) < 1500
    receipt = json.loads(prepared[3].content)
    assert receipt["num_edges"] == 715 and receipt["truncated"] is False
    assert "edges" not in receipt and receipt["model_view"] == "visualization_receipt"
    assert prepared[5].content == messages[5].content
    assert messages == before
    assert prepared[3].tool_call_id == "v1"

@pytest.mark.parametrize("content", ['{"success": false, "error": "' + "x"*9000 + '"}',
                                   "invalid-json-" + "x"*9000])
def test_canvas_errors_and_malformed_results_are_not_disguised_as_success(content):
    from agent_runtime.model_history import summarize_rendering_results
    message = Message(role="tool", tool_name="visualize_subgraph", content=content)
    assert summarize_rendering_results([message]) is False
    assert message.content == content

def test_other_analysis_payloads_are_not_silently_summarized():
    from agent_runtime.model_history import summarize_rendering_results
    message=Message(role="tool", tool_name="get_edge_info", content='{"success":true,"evidence":"'+"x"*9000+'"}')
    before=copy.deepcopy(message)
    assert summarize_rendering_results([message]) is False
    assert message==before


def test_kinship_checklist_uses_current_evidence_and_does_not_leak_into_next_dataset():
    from agent_runtime.model_history import kinship_answer_constraints
    evidence = Message(role="tool", tool_name="inspect_kinship",
        content=json.dumps({"success":True,"facts":[{"source_name":"A","target_name":"B"}]}))
    current=[Message(role="user",content="family"),evidence]
    assert "不同生父只能" in kinship_answer_constraints(current).content
    assert "本轮界面语言" in kinship_answer_constraints(current).content
    assert kinship_answer_constraints(current+[Message(role="user",content="finance")]) is None
    assert kinship_answer_constraints([Message(role="user",content="family"),
        Message(role="tool",tool_name="inspect_kinship",content='{"success":false}')]) is None
