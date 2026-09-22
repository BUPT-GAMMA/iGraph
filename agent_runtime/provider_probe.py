"""Bounded, credential-safe discovery through OpenAI-compatible model APIs."""
import asyncio
import json
import re
import time
import httpx
from agent_runtime.execution_store import ControlError


async def discover_models(data):
    started = time.monotonic()
    headers = {'Authorization': 'Bearer ' + data['api_key']} if data.get('api_key') else {}
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
            async with client.stream('GET', data['base_url'].rstrip('/') + '/models', headers=headers) as response:
                if response.status_code in {401, 403}:
                    raise ControlError('MODEL_AUTH_FAILED', 422)
                if response.status_code == 429:
                    raise ControlError('MODEL_RATE_LIMITED', 422)
                if response.status_code in {404, 405}:
                    raise ControlError('MODEL_DISCOVERY_UNSUPPORTED', 422)
                if response.status_code >= 300:
                    raise ControlError('MODEL_TEST_FAILED', 422)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise ControlError('MODEL_DISCOVERY_TOO_LARGE', 422)
                    chunks.append(chunk)
                payload = json.loads(b''.join(chunks))
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ControlError('MODEL_INVALID_RESPONSE', 422)
        unique = {}
        for row in rows:
            mid = row.get('id') if isinstance(row, dict) else None
            if isinstance(mid, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', mid):
                unique[mid] = {'id': mid, 'name': str(row.get('name') or mid)[:120]}
        if rows and not unique:
            raise ControlError('MODEL_INVALID_RESPONSE', 422)
        return dict(success=True, models=sorted(unique.values(), key=lambda m: m['id'].lower())[:5000],
                    truncated=len(unique) > 5000, latency_ms=round((time.monotonic() - started) * 1000))
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise ControlError('MODEL_CONNECTION_FAILED', 422) from None
