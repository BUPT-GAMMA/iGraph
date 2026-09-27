"""Offline regressions for feasibility evidence; no database or model calls."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts.probes import remote_graph_memgraph as memgraph
from scripts.probes import remote_graph_nebula as nebula


def snapshot(value=1):
    return {"owned_state": {"ok": True, "rows": [[{"type": 3, "value": value}]]}}


@pytest.mark.parametrize("module", [memgraph, nebula], ids=["memgraph", "nebula"])
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", "pass"),
        ("noop", "fail"),
        ("accepted", "fail"),
        ("candidate_mutation", "fail"),
        ("direct_error", "unverified"),
        ("positive_before_error", "unverified"),
        ("positive_after_error", "unverified"),
        ("candidate_before_error", "unverified"),
        ("candidate_after_error", "unverified"),
        ("both_candidate_errors", "unverified"),
        ("empty", "unverified"),
        ("missing_observation", "unverified"),
    ],
)
def test_control_requires_observed_positive_mutation_and_unchanged_rejection(
    module, scenario, expected
):
    observations = [snapshot(1), snapshot(2), snapshot(1), snapshot(1)]
    error = {"owned_state": {"ok": False, "error": "observer disconnected"}}
    positions = {
        "positive_before_error": 0,
        "positive_after_error": 1,
        "candidate_before_error": 2,
        "candidate_after_error": 3,
    }
    if scenario in positions:
        observations[positions[scenario]] = error
    if scenario == "noop":
        observations[1] = snapshot(1)
    if scenario == "candidate_mutation":
        observations[3] = snapshot(2)
    if scenario == "both_candidate_errors":
        observations[2:] = [error, deepcopy(error)]
    if scenario == "empty":
        observations = [{}, {}, {}, {}]
    if scenario == "missing_observation":
        observations[3]["extra"] = {"ok": True, "rows": []}
    probe = object.__new__(module.Probe)
    probe.events = []
    probe.fixture = Mock()
    probe.snapshot = Mock(side_effect=observations)
    probe.query = Mock(return_value={"ok": scenario != "direct_error"})
    probe.candidate = Mock(return_value={"rejected": scenario != "accepted"})
    probe.negative("owned_control", "fixed fixture mutation")
    assert probe.events[0]["status"] == expected
    if "candidate" in scenario and "error" in scenario:
        assert probe.events[0]["state_unchanged"] is None


@pytest.mark.parametrize("module", [memgraph, nebula], ids=["memgraph", "nebula"])
@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([], "unverified"),
        (["pass"], "pass"),
        (["pass", "unverified"], "unverified"),
        (["pass", "fail"], "fail"),
    ],
)
def test_aggregate_preserves_failed_or_unverified_controls(module, statuses, expected):
    events = [{"direct": {"ok": True}, "status": status} for status in statuses]
    # A syntax failure is not a successful writable positive control.
    events.append({"direct": {"ok": False}, "status": "unverified"})
    assert module.Probe.controls_status(events) == expected


@pytest.mark.parametrize(
    "observation,expected",
    [
        ({"ok": False, "error": "SHOW TRANSACTIONS failed"}, "unverified"),
        ({"ok": True, "rows": []}, "pass"),
        ({"ok": True, "rows": [{"query": memgraph.SLOW}]}, "unverified"),
    ],
)
def test_deadline_needs_successful_server_observation(observation, expected):
    timeout = {"ok": False, "error": "transaction timeout", "elapsed_ms": 2000}
    assert memgraph.Probe.deadline_status(timeout, observation) == expected


@pytest.mark.parametrize("error", [None, RuntimeError("unexpected transport failure")])
def test_nebula_auth_probe_closes_connection_without_hiding_errors(monkeypatch, error):
    connection = Mock()
    if error is not None:
        connection.authenticate.side_effect = error
    monkeypatch.setattr(
        nebula, "Connection", Mock(return_value=connection), raising=False
    )
    result = nebula.authentication_probe("test-password")
    connection.close.assert_called_once()
    if error is None:
        assert result["ok"]
        connection.signout.assert_called_once()
    else:
        assert not result["ok"] and result["error"] == str(error)
        assert result["error_type"] == "RuntimeError"


@pytest.mark.anyio
@pytest.mark.parametrize('later,expected', [
    ({'ok':False,'error':'observer disconnected'}, 'unverified'),
    ({'ok':True}, 'unverified'),
    ({'ok':True,'rows':None}, 'unverified'),
    ({'ok':True,'rows':["malformed row"]}, 'unverified'),
    ({'ok':True,'rows':[{}]}, 'unverified'),
    ({'ok':True,'rows':[]}, 'pass'),
])
async def test_disconnect_requires_valid_observation_after_initial_sighting(monkeypatch, later, expected):
    import asyncio
    query=memgraph.SLOW + ' /* owned-client-disconnect */'
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def run(self,text):
            assert text == query
            await asyncio.Event().wait()
        def cancel(self): pass
    class Driver:
        def session(self): return Session()
        async def close(self): pass
    monkeypatch.setattr(memgraph.AsyncGraphDatabase,'driver',lambda *a,**kw:Driver())
    probe=object.__new__(memgraph.Probe)
    observations=iter([{'ok':True,'rows':[{'username':'qa_probe','transaction_id':'123','query':[query], 'status':'running', 'metadata':{}, 'start_time':None, 'elapsed_ms':1}]}, later])
    def observe(text):
        assert text == 'SHOW TRANSACTIONS'
        return next(observations)
    probe.query=observe
    result=await probe.disconnect_probe('offline-password')
    assert result['server_stopped'] is (expected == 'pass')
    assert result['status'] == expected
    assert result['polls'][-1]['observation'] == later
    if expected == 'unverified':
        assert result['polls'][-1]['running'] is None
