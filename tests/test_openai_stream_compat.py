import json

import pytest
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from agno.models.message import Message
from agent_runtime.model_factory import DAGOpenAILike, CancellableDAGOpenAILike
from agent_runtime.model_history import normalize_history


def delta(index, id=None, name=None, arguments='', **kwargs):
    return ChoiceDeltaToolCall(index=index, id=id, type='function',
        function={'name': name, 'arguments': arguments}, **kwargs)


@pytest.mark.parametrize('adapter', [DAGOpenAILike, CancellableDAGOpenAILike])
@pytest.mark.parametrize('indices', [(0, 0, 0), (0, 1, 2)])
def test_distinct_calls_and_signed_state(adapter, indices):
    chunks = [delta(i, f'call_{n}', name, '{}', **({'extra_content': {'google': {'thought_signature': 'signed'}}} if n == 0 else {}))
              for n, (i, name) in enumerate(zip(indices, ['density', 'transitivity', 'components']))]
    calls = adapter(id='test').parse_tool_calls(chunks)
    assert [c['function']['name'] for c in calls] == ['density', 'transitivity', 'components']
    assert [json.loads(c['function']['arguments']) for c in calls] == [{}, {}, {}]
    assert [c['id'] for c in calls] == ['call_0', 'call_1', 'call_2']
    assert chunks[1].index == indices[1]
    messages = [Message(role='assistant', tool_calls=calls, provider_data={'igraph_history_key': 'same'}),
                *[Message(role='tool', tool_call_id=c['id'], content='0.5') for c in calls]]
    same = normalize_history(messages, history_key='same')
    assert same[0].tool_calls[0]['extra_content']['google']['thought_signature'] == 'signed'
    other = normalize_history(messages, history_key='other')
    assert 'extra_content' not in other[0].tool_calls[0]


def test_interleaved_fragments_and_repeated_ids():
    calls = DAGOpenAILike(id='test').parse_tool_calls([
        delta(0, 'a', 'measure', '{"value":'), delta(1, 'b', 'measure', '{"value":'),
        delta(0, arguments='1}'), delta(1, 'b', arguments='2}')])
    assert [json.loads(c['function']['arguments']) for c in calls] == [{'value': 1}, {'value': 2}]


def test_bad_previous_run_does_not_poison_retry():
    messages = [Message(role='assistant', tool_calls=[{'id': 'bad', 'type': 'function',
        'function': {'name': 'components', 'arguments': '{}{}'}}]),
        Message(role='tool', tool_call_id='bad', content='Invalid arguments'),
        Message(role='user', content='Try again')]
    normalized = normalize_history(messages)
    assert normalized[0].tool_calls is None
    assert normalized[1].role == 'assistant'
    assert 'Invalid arguments' in normalized[1].content
    assert messages[0].tool_calls[0]['function']['arguments'] == '{}{}'
