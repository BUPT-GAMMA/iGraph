"""Examples share the same immediate provider stream as ordinary questions."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agent_runtime.model_history import ModelHistoryMixin


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def evidence(kind):
    tool, payload = {
        'kinship': ('inspect_kinship', {'success': True, 'facts': [{
            'source_name': 'G', 'target_name': 'C', 'relation': '祖母',
            'kind': 'biological_grandparent', 'role': 'paternal_grandmother'}]}),
        'borylation': ('borylation_case_analysis', {'success': True, 'step': 'rings', 'result': {'ring_count': 3}}),
        'finance': ('finance_inspect_ownership', {'success': True, 'result': {'count': 5}}),
        'community': ('community_detection', {'success': True, 'communities': [{'community_id': 1, 'size': 2}]}),
    }[kind]
    return [Message(role='system', content='Answer in Chinese.'),
            Message(role='user', content='请根据本轮证据回答。'),
            Message(role='tool', tool_name=tool, content=json.dumps(payload))]


class Provider:
    def __init__(self, mode='normal'):
        self.calls = 0
        self.gate = asyncio.Event()
        self.closed = False
        self.mode = mode

    def invoke(self, messages, *args, **kwargs):
        self.calls += 1
        assert messages[0].content == 'Answer in Chinese.'
        return ModelResponse(content='直接回答。', response_usage=MessageMetrics(input_tokens=100, output_tokens=20))

    async def ainvoke(self, *args, **kwargs):
        return self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, messages, *args, **kwargs):
        self.calls += 1
        assert messages[0].content == 'Answer in Chinese.'
        try:
            if self.mode == 'tool':
                yield ModelResponse(tool_calls=[{'id': 'next-tool', 'function': {'name': 'visualize_subgraph', 'arguments': '{}'}}])
            else:
                yield ModelResponse(content='结论：')
            await self.gate.wait()
            if self.mode == 'error':
                raise RuntimeError('provider interrupted')
            yield ModelResponse(content='依据图中证据。\n总结：完成。',
                response_usage=MessageMetrics(input_tokens=100, output_tokens=20), provider_data={'signed': 'native-state'})
        finally:
            self.closed = True

    def invoke_stream(self, *args, **kwargs):
        self.calls += 1
        try:
            yield ModelResponse(content='结论：')
            yield ModelResponse(content='直接回答。', response_usage=MessageMetrics(input_tokens=100, output_tokens=20))
        finally:
            self.closed = True


class Model(ModelHistoryMixin, Provider):
    id = 'direct-stream-test'


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['kinship', 'borylation', 'finance', 'community'])
async def test_first_delta_arrives_before_provider_finishes_without_review(kind):
    model = Model()
    run = SimpleNamespace(metadata={})
    stream = model.ainvoke_stream(evidence(kind), Message(role='assistant'), run_response=run)
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first.content == '结论：' and not model.closed and not model.gate.is_set()
    model.gate.set()
    events = [first] + [event async for event in stream]
    assert ''.join(event.content or '' for event in events) == '结论：依据图中证据。\n总结：完成。'
    assert sum(event.response_usage.input_tokens for event in events if event.response_usage) == 100
    assert model.calls == 1 and model.closed
    assert events[-1].provider_data == {'signed': 'native-state'}
    assert not any(key.endswith('_answer_review') for key in run.metadata)


@pytest.mark.anyio
async def test_tool_delta_is_not_buffered_behind_the_rest_of_the_request():
    model = Model('tool')
    stream = model.ainvoke_stream(evidence('kinship'), Message(role='assistant'))
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first.tool_calls[0]['id'] == 'next-tool' and not model.closed
    await stream.aclose()
    assert model.closed and model.calls == 1


@pytest.mark.anyio
async def test_cancellation_closes_active_provider_stream():
    model = Model()
    stream = model.ainvoke_stream(evidence('borylation'), Message(role='assistant'))
    await anext(stream)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert model.closed and model.calls == 1


@pytest.mark.anyio
async def test_midstream_failure_is_propagated_without_a_second_model_call():
    model = Model('error')
    stream = model.ainvoke_stream(evidence('finance'), Message(role='assistant'))
    assert (await anext(stream)).content == '结论：'
    model.gate.set()
    with pytest.raises(RuntimeError, match='provider interrupted'):
        await anext(stream)
    assert model.closed and model.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['kinship', 'borylation', 'finance'])
async def test_nonstream_entry_points_also_do_not_add_a_review(kind):
    for asynchronous in (False, True):
        model = Model()
        result = (await model.ainvoke(evidence(kind), Message(role='assistant')) if asynchronous
                  else model.invoke(evidence(kind), Message(role='assistant')))
        assert result.content == '直接回答。' and model.calls == 1
        assert result.response_usage.input_tokens == 100


@pytest.mark.parametrize('kind', ['kinship', 'borylation', 'finance'])
def test_sync_stream_is_immediate_and_early_close_releases_provider(kind):
    model = Model()
    stream = model.invoke_stream(evidence(kind), Message(role='assistant'))
    assert next(stream).content == '结论：' and not model.closed
    stream.close()
    assert model.closed and model.calls == 1
