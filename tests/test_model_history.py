import copy
import json
from types import SimpleNamespace
import httpx
import pytest
from agno.models.message import Message
from openai import AsyncOpenAI
from agent_runtime.model_history import normalize_history, prepare_history
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
            'model': 'upstream-b', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})
    config = dict(model_id='custom-test', upstream_model_id='upstream-b', base_url='http://example.test/v1',
        api_key='', allow_anonymous=True, context_window=32768, max_output_tokens=1024, vision=False)
    model = create_model('custom-test', SimpleNamespace(), config)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model.async_client = AsyncOpenAI(api_key='local', base_url=config['base_url'], http_client=client)
        messages = [Message(role='user', content='old question'), Message(role='assistant', content='old answer', reasoning_content='DO NOT TRANSMIT'), Message(role='user', content='new question')]
        result = await model.ainvoke(messages, Message(role='assistant'))
    assert result.content == 'OK'
    assert captures[0]['model'] == 'upstream-b' and captures[0]['max_tokens'] == 1024
    assert 'DO NOT TRANSMIT' not in json.dumps(captures)
    assert len(captures[0]['messages']) == 3


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
