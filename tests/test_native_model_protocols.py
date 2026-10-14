"""Exercise real SDK serialization and Agno tool round trips without paid APIs."""
import asyncio
import base64
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.exceptions import RunCancelledException
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
from google import genai
from agent_runtime.model_factory import create_model
from agent_runtime.execution_scope import RunScope, current_scope
from graph_engine.model_support import ProviderGateway
from model_protocols import sdk_endpoint

PROTOCOLS = ['openai-compatible', 'openai-responses', 'anthropic', 'gemini']


def config(protocol):
    return dict(model_id='custom-test', upstream_model_id='test-model', protocol=protocol,
        base_url='https://provider.test/proxy/' + ('v1beta' if protocol == 'gemini' else 'v1'),
        api_key='test-key', allow_anonymous=True, context_window=32768, max_output_tokens=1024, vision=False)


def payload(protocol, *, tool=False):
    if protocol == 'anthropic':
        content = [{'type': 'tool_use', 'id': 'toolu_test', 'name': 'measure', 'input': {'value': 2}}] if tool else [{'type': 'text', 'text': 'OK'}]
        return dict(id='msg_test', type='message', role='assistant', model='test-model', content=content,
            stop_reason='tool_use' if tool else 'end_turn', stop_sequence=None, usage={'input_tokens': 10, 'output_tokens': 5})
    if protocol == 'gemini':
        part = {'functionCall': {'name': 'measure', 'args': {'value': 2}}, 'thoughtSignature': base64.b64encode(b'signed-state').decode()} if tool else {'text': 'OK'}
        return dict(candidates=[{'content': {'role': 'model', 'parts': [part]}, 'finishReason': 'STOP', 'index': 0}],
            usageMetadata={'promptTokenCount': 10, 'candidatesTokenCount': 5, 'totalTokenCount': 15}, modelVersion='test-model')
    if protocol == 'openai-responses':
        output = [{'type': 'function_call', 'id': 'fc_test', 'call_id': 'call_test', 'name': 'measure', 'arguments': '{"value":2}', 'status': 'completed'}] if tool else [
            {'type': 'message', 'id': 'msg_test', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'OK', 'annotations': []}]}]
        if tool:
            output.insert(0, {'type': 'reasoning', 'id': 'rs_test', 'summary': [], 'encrypted_content': 'encrypted-test'})
        return dict(id='resp_test', object='response', created_at=1, model='test-model', status='completed', output=output, error=None,
            usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}})
    message = dict(role='assistant', content=None if tool else 'OK')
    if tool:
        message['tool_calls'] = [{'id': 'call_test', 'type': 'function', 'function': {'name': 'measure', 'arguments': '{"value":2}'}}]
    return dict(id='chat_test', object='chat.completion', created=1, model='test-model', choices=[{
        'index': 0, 'message': message, 'finish_reason': 'tool_calls' if tool else 'stop'}], usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})


def stream_body(protocol, *, tool=False):
    if protocol == 'anthropic':
        events = [dict(type='message_start', message={**payload(protocol), 'content': [], 'stop_reason': None}),
            dict(type='content_block_start', index=0, content_block={'type': 'text', 'text': ''}),
            dict(type='content_block_delta', index=0, delta={'type': 'text_delta', 'text': 'OK'}),
            dict(type='content_block_stop', index=0),
            dict(type='message_delta', delta={'stop_reason': 'end_turn', 'stop_sequence': None}, usage={'output_tokens': 5}), dict(type='message_stop')]
    elif protocol == 'openai-responses':
        events = [dict(type='response.created', response={**payload(protocol), 'status': 'in_progress', 'output': []}, sequence_number=0),
            dict(type='response.output_text.delta', delta='OK', item_id='msg_test', output_index=0, content_index=0, sequence_number=1),
            dict(type='response.completed', response=payload(protocol), sequence_number=2)]
    elif protocol == 'gemini':
        events = [payload(protocol)]
    else:
        events = [dict(id='chat_test', object='chat.completion.chunk', created=1, model='test-model', choices=[{
            'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}])]
    if tool:
        response = payload(protocol, tool=True)
        if protocol == 'gemini':
            events = [response]
        elif protocol == 'anthropic':
            call = response['content'][0]
            events = [dict(type='message_start', message={**response, 'content': [], 'stop_reason': None}),
                dict(type='content_block_start', index=0, content_block={**call, 'input': {}}),
                dict(type='content_block_delta', index=0, delta={'type': 'input_json_delta', 'partial_json': '{"value":2}'}),
                dict(type='content_block_stop', index=0),
                dict(type='message_delta', delta={'stop_reason': 'tool_use', 'stop_sequence': None}, usage={'output_tokens': 5}), dict(type='message_stop')]
        elif protocol == 'openai-responses':
            call = response['output'][1]
            events = [dict(type='response.created', response={**response, 'status': 'in_progress', 'output': []}, sequence_number=0),
                dict(type='response.output_item.added', item={**call, 'arguments': ''}, output_index=1, sequence_number=1),
                dict(type='response.function_call_arguments.delta', delta=call['arguments'], item_id=call['id'], output_index=1, sequence_number=2),
                dict(type='response.output_item.done', item=call, output_index=1, sequence_number=3),
                dict(type='response.completed', response=response, sequence_number=4)]
        else:
            call = response['choices'][0]['message']['tool_calls'][0]
            events = [dict(id='chat_test', object='chat.completion.chunk', created=1, model='test-model', choices=[{
                'index': 0, 'delta': {'role': 'assistant', 'tool_calls': [{'index': 0, **call}]}, 'finish_reason': 'tool_calls'}])]
    return ''.join((f"event: {event['type']}\n" if 'type' in event else '') + 'data: ' + json.dumps(event) + '\n\n' for event in events).encode()


@asynccontextmanager
async def inject_client(model, protocol, handler):
    settings = config(protocol)
    root, version = sdk_endpoint(settings['base_url'], protocol)
    transport_module = httpx2 if protocol == 'anthropic' else httpx
    def transport_handler(request):
        response = handler(request)
        if protocol != 'anthropic':
            return response
        class Stream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in response.stream:
                    yield chunk
            async def aclose(self):
                await response.stream.aclose()
        return httpx2.Response(response.status_code, headers=dict(response.headers), stream=Stream())
    async with transport_module.AsyncClient(transport=transport_module.MockTransport(transport_handler)) as client:
        if protocol == 'gemini':
            native = genai.Client(api_key='test-key', vertexai=False, http_options={'base_url': root, 'api_version': version, 'httpx_async_client': client})
            model.client = native
        elif protocol == 'anthropic':
            native = AsyncAnthropic(api_key='test-key', base_url=root, http_client=client, max_retries=0)
            model.async_client = native
        else:
            native = AsyncOpenAI(api_key='test-key', base_url=root, http_client=client, max_retries=0)
            model.async_client = native
        yield native
        if protocol == 'gemini':
            await native.aio.aclose()
            native.close()


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('stream', [False, True])
def test_sdk_text_request_and_stream(protocol, stream, monkeypatch):
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    async def scenario():
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=stream_body(protocol)) if stream else httpx.Response(200, json=payload(protocol))
        model = create_model('custom-test', SimpleNamespace(), config(protocol))
        async with inject_client(model, protocol, respond):
            if stream:
                output = [event async for event in model.ainvoke_stream([Message(role='user', content='Hello')], Message(role='assistant'))]
                assert 'OK' in ''.join(event.content or '' for event in output)
            else:
                result = await model.ainvoke([Message(role='user', content='Hello')], Message(role='assistant'))
                assert result.content == 'OK'
        assert len(seen) == 1
        request = seen[0]
        endpoint = {'openai-compatible': '/proxy/v1/chat/completions', 'openai-responses': '/proxy/v1/responses', 'anthropic': '/proxy/v1/messages',
            'gemini': '/proxy/v1beta/models/test-model:' + ('streamGenerateContent' if stream else 'generateContent')}[protocol]
        assert request.url.path == endpoint
        header = 'x-api-key' if protocol == 'anthropic' else 'x-goog-api-key' if protocol == 'gemini' else 'authorization'
        assert request.headers[header] == ('test-key' if protocol in {'gemini', 'anthropic'} else 'Bearer test-key')
        body = json.loads(request.content)
        if protocol == 'openai-responses':
            assert body['store'] is False and body['max_output_tokens'] == 1024
    asyncio.run(scenario())


@pytest.mark.parametrize('language,reminder', [
    ('Chinese', '本轮界面语言：简体中文'),
    ('English', 'Current interface language: English'),
])
@pytest.mark.parametrize('stream', [False, True])
def test_gemini_wire_preserves_system_rules_with_language_reminder(language, reminder, stream):
    async def scenario():
        bodies = []

        def respond(request):
            bodies.append(json.loads(request.content))
            if stream:
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=stream_body('gemini'))
            return httpx.Response(200, json=payload('gemini'))

        model = create_model('custom-test', SimpleNamespace(), config('gemini'))
        model.igraph_response_language = language
        messages = [Message(role='system', content='BASE_GRAPH_RULE: use graph tools.'),
                    Message(role='developer', content='EVIDENCE_RULE: cite verified evidence.'),
                    Message(role='user', content='Analyse this graph.')]
        before = copy.deepcopy(messages)
        async with inject_client(model, 'gemini', respond):
            if stream:
                events = [event async for event in model.ainvoke_stream(messages, Message(role='assistant'))]
                assert ''.join(event.content or '' for event in events) == 'OK'
            else:
                assert (await model.ainvoke(messages, Message(role='assistant'))).content == 'OK'
        assert messages == before
        system = '\n'.join(part['text'] for part in bodies[0]['systemInstruction']['parts'])
        assert 'BASE_GRAPH_RULE: use graph tools.' in system
        assert 'EVIDENCE_RULE: cite verified evidence.' in system
        assert reminder in system
        assert system.index('BASE_GRAPH_RULE') < system.index('EVIDENCE_RULE') < system.index(reminder)

    asyncio.run(scenario())


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('stream', [False, True])
def test_actual_agno_tool_round_trip_preserves_native_ids_and_signatures(protocol, stream, monkeypatch):
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    async def scenario():
        requests, called = [], []
        def measure(value: int) -> int:
            """Return a measured value."""
            called.append(value)
            return value * 3
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=stream_body(protocol, tool=len(requests) == 1)) if stream else httpx.Response(200, json=payload(protocol, tool=len(requests) == 1))
        model = create_model('custom-test', SimpleNamespace(), config(protocol))
        async with inject_client(model, protocol, respond):
            agent = Agent(model=model, tools=[measure], telemetry=False)
            if stream:
                events = [event async for event in agent.arun('Measure 2.', stream=True)]
                assert 'OK' in ''.join(str(event.content or '') for event in events)
            else:
                response = await agent.arun('Measure 2.')
                assert response.content == 'OK'
        assert called == [2] and len(requests) == 2
        body = requests[1]
        if protocol == 'gemini':
            parts = [part for content in body['contents'] for part in content['parts']]
            call = next(p for p in parts if 'functionCall' in p)
            assert call['thoughtSignature'] == base64.b64encode(b'signed-state').decode()
            assert any('functionResponse' in p for p in parts)
        elif protocol == 'openai-responses':
            call = next(p for p in body['input'] if p.get('type') == 'function_call')
            result = next(p for p in body['input'] if p.get('type') == 'function_call_output')
            assert call['call_id'] == result['call_id'] == 'call_test'
            assert any(p.get('encrypted_content') == 'encrypted-test' for p in body['input'])
        elif protocol == 'anthropic':
            assert any(isinstance(m['content'], list) and any(p['type'] == 'tool_result' for p in m['content']) for m in body['messages'])
    asyncio.run(scenario())


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_cancel_before_first_token_closes_owned_http_stream(protocol, monkeypatch):
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        class BlockingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b''
            async def aclose(self):
                closed.set()
        model = create_model('custom-test', SimpleNamespace(), config(protocol))
        scope = RunScope('test-run', 'test-user', 'test-session', 1)
        token = current_scope.set(scope)
        async with inject_client(model, protocol, lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=BlockingStream())):
            async def consume():
                async for _ in model.ainvoke_stream([Message(role='user', content='Hello')], Message(role='assistant')):
                    pass
            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(entered.wait(), 3)
                scope.cancel()
                with pytest.raises(RunCancelledException):
                    await asyncio.wait_for(task, 3)
                assert closed.is_set() and scope.execution_stopped
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                current_scope.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_graph_ingestion_uses_selected_protocol(protocol):
    async def scenario():
        settings = {**config(protocol), 'model_id': 'test-model'}
        gateway = ProviderGateway(model='ignored', api_key='test-key', base_url=settings['base_url'], temperature=0,
            logger=None, resolved_config=settings)
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=payload(protocol))
        dummy = SimpleNamespace()
        # Close clients created by the real constructor before injecting a transport.
        if protocol == 'gemini':
            await gateway.client.aio.aclose(); gateway.client.close()
        else:
            await gateway.client.close()
        async with inject_client(dummy, protocol, respond) as native:
            gateway.client = native
            assert await gateway.invoke_text([{'role': 'system', 'content': 'Extract entities.'}, {'role': 'user', 'content': 'Alice knows Bob.'}], step=1) == 'OK'
        assert len(requests) == 1 and '/proxy/' in requests[0].url.path
    asyncio.run(scenario())
