"""Financial operations cannot choose another session's identity or tool."""
import pytest

from test_projects import FakeServiceClient, client, headers


@pytest.fixture
def finance_calls(monkeypatch):
    calls = []
    original = FakeServiceClient.request
    async def request(self, method, path, *, headers, json_body=None, **kwargs):
        if not self.is_runtime and "/tools/finance_" in path:
            calls.append((path, headers, json_body))
            return {"success": True, "status": "DATA_CONFLICT", "complete": True,
                    "result": {"confirmed_id": None}}
        return await original(self, method, path, headers=headers, json_body=json_body, **kwargs)
    monkeypatch.setattr(FakeServiceClient, "request", request)
    return calls


def test_finance_preserves_business_status_and_session_ownership(client, finance_calls):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    endpoint = f'/api/sessions/{sid}/finance/first-holder'
    args = {"target_id": "A", "policy": {"mode": "equity_only"}}
    response = client.post(endpoint, headers=headers(), json=args)
    assert response.status_code == 200
    assert response.json()['status'] == 'DATA_CONFLICT'
    assert response.json()['result']['confirmed_id'] is None
    assert finance_calls[0][0] == f'/sessions/{sid}/tools/finance_first_holder'
    assert finance_calls[0][1]['X-User-ID'] == 'admin'
    assert finance_calls[0][2] == {"arguments": args}
    before = len(finance_calls)
    # Single-user deployment ignores browser-selected identities.
    assert client.post(endpoint, headers=headers("alice"), json=args).status_code == 200
    assert client.post(endpoint, json=args).status_code == 200
    assert all(call[1]["X-User-ID"] == "admin" for call in finance_calls)
    assert client.post("/api/sessions/missing/finance/first-holder", json=args).status_code == 404
    assert len(finance_calls) == before + 2


def test_finance_route_never_proxies_arbitrary_tools_or_nonobjects(client, finance_calls):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    root = f'/api/sessions/{sid}/finance/'
    assert client.post(root + 'clear_graph', headers=headers(), json={}).status_code == 404
    assert client.post(root + 'inspect', headers=headers(), json=[]).status_code == 422
    assert client.post(root + 'inspect', headers=headers(), content='invalid').status_code == 422
    assert finance_calls == []
