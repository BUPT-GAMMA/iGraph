"""Resolve the run's fixed model lazily, without persisting credentials in jobs."""
import os
from urllib.parse import quote
import httpx
from execution_protocol import service_headers, controls_enabled
from graph_data_service.execution_host import job_context
from graph_engine.model_support import ProviderGateway


async def resolve_run_gateway(logger, session_id='', user_id=''):
    ctx = job_context.get()
    if not ctx and not (controls_enabled() and session_id and user_id):
        return None
    url = os.getenv('AGENT_RUNTIME_URL', 'http://agent-runtime:21112').rstrip('/')
    path = (f'/internal/sessions/{quote(ctx.session_id, safe="")}/runs/{quote(ctx.run_id, safe="")}/model-config' if ctx
            else f'/internal/sessions/{quote(session_id, safe="")}/model-config')
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(url + path, headers=service_headers(ctx.user_id if ctx else user_id))
            if response.status_code != 200:
                raise ValueError('MODEL_RUN_CONFIG_UNAVAILABLE')
            config = response.json()
        return ProviderGateway(model=config['model_id'], api_key=config['api_key'], base_url=config['base_url'],
            temperature=0.0, logger=logger, resolved_config=config)
    except (httpx.HTTPError, KeyError):
        # Never fall back to deployment credentials after an explicit run choice.
        raise ValueError('MODEL_RUN_CONFIG_UNAVAILABLE') from None
