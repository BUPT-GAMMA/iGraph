"""Bounded, credential-safe discovery and generation checks for each protocol."""
import asyncio
import json
import re
import time
import httpx
from agent_runtime.execution_store import ControlError
from model_protocols import api_base, generation_url, protocol_for, request_headers


def check_status(status, *, discovery=False):
    if status in {401, 403}:
        raise ControlError('MODEL_AUTH_FAILED', 422)
    if status == 429:
        raise ControlError('MODEL_RATE_LIMITED', 422)
    if discovery and status in {404, 405}:
        raise ControlError('MODEL_DISCOVERY_UNSUPPORTED', 422)
    if status >= 300:
        raise ControlError('MODEL_TEST_FAILED', 422)


async def discover_models(data):
    started = time.monotonic()
    protocol = protocol_for(data)
    url = api_base(data['base_url'], protocol) + '/models'
    params = {'pageSize': 1000} if protocol == 'gemini' else {'limit': 1000} if protocol == 'anthropic' else {}
    unique, cursors, total_size = {}, set(), 0
    truncated = False
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            for page in range(50):
                async with client.stream('GET', url, headers=request_headers(data), params=params) as response:
                    check_status(response.status_code, discovery=True)
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        total_size += len(chunk)
                        if total_size > 4 * 1024 * 1024:
                            raise ControlError('MODEL_DISCOVERY_TOO_LARGE', 422)
                        chunks.append(chunk)
                    payload = json.loads(b''.join(chunks))
                rows = payload.get('models' if protocol == 'gemini' else 'data') if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    raise ControlError('MODEL_INVALID_RESPONSE', 422)
                valid_ids = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    mid = row.get('name') if protocol == 'gemini' else row.get('id')
                    if protocol == 'gemini' and isinstance(mid, str):
                        mid = mid.removeprefix('models/')
                    if not isinstance(mid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', mid):
                        continue
                    valid_ids += 1
                    if protocol == 'gemini' and 'generateContent' not in row.get('supportedGenerationMethods', []):
                        continue
                    name = row.get('displayName') if protocol == 'gemini' else row.get('display_name') or row.get('name')
                    unique[mid] = {'id': mid, 'name': str(name or mid)[:120]}
                if rows and not valid_ids:
                    raise ControlError('MODEL_INVALID_RESPONSE', 422)
                cursor = payload.get('nextPageToken') if protocol == 'gemini' else payload.get('last_id') if protocol == 'anthropic' and payload.get('has_more') else None
                if protocol == 'anthropic' and payload.get('has_more') and not cursor:
                    raise ControlError('MODEL_INVALID_RESPONSE', 422)
                if len(unique) > 5000 or (cursor and page == 49):
                    truncated = True
                    break
                if not cursor:
                    break
                if not isinstance(cursor, str) or len(cursor) > 8192 or cursor in cursors:
                    raise ControlError('MODEL_INVALID_RESPONSE', 422)
                cursors.add(cursor)
                params['pageToken' if protocol == 'gemini' else 'after_id'] = cursor
        return dict(success=True, models=sorted(unique.values(), key=lambda m: m['id'].lower())[:5000],
                    truncated=truncated, latency_ms=round((time.monotonic() - started) * 1000))
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise ControlError('MODEL_CONNECTION_FAILED', 422) from None


async def probe_model(data):
    protocol = protocol_for(data)
    function = dict(name='connection_test', description='A harmless connectivity check; never executed.',
                    parameters={'type': 'object', 'properties': {}})
    messages = [{'role': 'user', 'content': 'Reply OK.'}]
    # Reasoning models can consume their output budget before producing text.
    limit = min(int(data.get('max_output_tokens', 4096)), 1024)
    if protocol == 'gemini':
        body = dict(contents=[{'role': 'user', 'parts': [{'text': 'Reply OK.'}]}], generationConfig={'maxOutputTokens': limit})
        if data.get('tools'):
            body['tools'] = [{'functionDeclarations': [function]}]
    elif protocol == 'anthropic':
        body = dict(model=data['model_id'], messages=messages, max_tokens=limit)
        if data.get('tools'):
            body['tools'] = [dict(name=function['name'], description=function['description'], input_schema=function['parameters'])]
    elif protocol == 'openai-responses':
        body = dict(model=data['model_id'], input=messages, max_output_tokens=limit, store=False)
        if data.get('tools'):
            body['tools'] = [dict(type='function', **function)]
    else:
        body = dict(model=data['model_id'], messages=messages, max_tokens=limit, stream=False)
        if data.get('tools'):
            body['tools'] = [{'type': 'function', 'function': function}]
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            async with client.stream('POST', generation_url(data), headers=request_headers(data), json=body) as response:
                check_status(response.status_code)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise ControlError('MODEL_INVALID_RESPONSE', 422)
                    chunks.append(chunk)
                payload = json.loads(b''.join(chunks))
        field = {'openai-compatible': 'choices', 'openai-responses': 'output', 'anthropic': 'content', 'gemini': 'candidates'}[protocol]
        if not isinstance(payload, dict) or not isinstance(payload.get(field), list) or not payload[field]:
            raise ControlError('MODEL_INVALID_RESPONSE', 422)
        if payload.get('error') or payload.get('status') in {'failed', 'cancelled'}:
            raise ControlError('MODEL_TEST_FAILED', 422)
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise ControlError('MODEL_CONNECTION_FAILED', 422) from None
