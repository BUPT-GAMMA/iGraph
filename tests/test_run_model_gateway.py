import httpx
import pytest
from graph_data_service.execution_host import JobContext, job_context
from graph_data_service.run_model import resolve_run_gateway


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_ingestion_resolves_run_version_and_never_uses_service_fallback(monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def respond(req):
        seen.append(req)
        return httpx.Response(200, json={'model_id': 'my-local-model', 'api_key': '', 'base_url': 'http://localhost:1234/v1', 'max_output_tokens': 2048, 'context_window': 8192})
    monkeypatch.setattr('graph_data_service.run_model.httpx.AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    token = job_context.set(JobContext('alice', 'session-A', 'run-A', 1, 'operation-A'))
    try:
        gateway = await resolve_run_gateway(None)
        assert gateway.model == 'my-local-model'
        assert str(gateway.client.base_url) == 'http://localhost:1234/v1/'
        assert seen[0].url.path.endswith('/sessions/session-A/runs/run-A/model-config')
        assert seen[0].headers['X-User-ID'] == 'alice'
        await gateway.client.close()
        monkeypatch.setattr('graph_data_service.run_model.httpx.AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(409))))
        with pytest.raises(ValueError, match='MODEL_RUN_CONFIG_UNAVAILABLE'):
            await resolve_run_gateway(None)
    finally:
        job_context.reset(token)
