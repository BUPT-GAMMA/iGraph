"""Regression gates for candidate scan complexity and construction deadlines."""
import copy
import time

import pytest

from graph_engine.construction.agent import _candidate_examples
from graph_engine.construction.sources import ForeignKey, RelationalSource, Table


class CountedEdges(list):
    visits = 0

    def __iter__(self):
        for edge in super().__iter__():
            self.visits += 1
            yield edge


def orders(size):
    source = RelationalSource({
        "customers": Table("customers", ["id"], [{"id": 1}], ["id"]),
        "orders": Table("orders", ["id", "customer_id"], [], ["id"],
                        [ForeignKey(["customer_id"], "customers", ["id"])]),
    })
    nodes = [{"id": f"order-{i}", "type": "orders", "properties": {"table": "orders", "data": {"id": i}}}
             for i in range(size)]
    edges = CountedEdges({"id": f"edge-{i}", "source": node["id"], "target": "customer",
                          "type": "fk", "properties": {"foreign_key": {"table": "orders"}}}
                         for i, node in enumerate(nodes))
    return source, {"directed": True, "nodes": nodes, "edges": edges}


@pytest.mark.parametrize("size", [2000, 4000, 8000, 32000])
def test_one_to_many_candidate_scan_visits_edges_once(size):
    source, graph = orders(size)
    assert _candidate_examples(source, graph, []) == []
    assert graph["edges"].visits <= size


def test_candidate_index_preserves_parallel_edges_self_loops_and_order():
    source, graph = orders(12)
    # The old membership check counts a loop once, and distinct parallel edges
    # twice. Keep precisely its proposal semantics; tools validate proposals.
    graph["edges"] = [dict(edge, source=edge["source"], target=edge["source"]) for edge in graph["edges"]]
    graph["edges"] += [dict(edge, id=edge["id"] + "-parallel") for edge in graph["edges"][:11]]
    original = copy.deepcopy(graph)
    result = _candidate_examples(source, graph, [])
    assert [op["node_id"] for op in result[0]["arguments"]["operations"]] == [f"order-{i}" for i in range(10)]
    assert graph == original


def test_expired_candidate_budget_stops_before_scanning():
    from graph_engine.construction.budget import deadline_scope
    source, graph = orders(32000)
    with deadline_scope(time.monotonic() - 1), pytest.raises(TimeoutError):
        _candidate_examples(source, graph, [])
    assert graph["edges"].visits == 0


def test_cache_reuses_rejected_version_and_detaches_returned_proposals():
    from graph_engine.construction.agent import CandidateCache
    cache, calls = CandidateCache(), []
    def candidates():
        calls.append(True)
        return [{"arguments": {"node_ids": ["current"]}}]
    returned = cache.get(0, candidates)
    returned[0]["arguments"]["node_ids"].append("changed-by-consumer")
    assert cache.get(0, candidates)[0]["arguments"]["node_ids"] == ["current"]
    assert len(calls) == 1
    assert cache.get(1, candidates) == [{"arguments": {"node_ids": ["current"]}}]
    assert len(calls) == 2


def test_deadline_checked_within_large_scan_and_context_does_not_leak(monkeypatch):
    from graph_engine.construction import budget
    clock = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: clock[0])
    visited = []
    def rows():
        for number in range(10000):
            visited.append(number)
            if number == 100:
                clock[0] = 2.0
            yield number
    with pytest.raises(budget.ConstructionBudgetExceeded), budget.deadline_scope(1.0):
        list(budget.checked(rows()))
    assert len(visited) <= 1025
    assert budget.current_deadline() is None
    assert list(budget.checked([1, 2])) == [1, 2]


@pytest.mark.parametrize("phase", ["source", "initial", "representation", "executor", "fidelity", "ranking", "transform"])
def test_expired_budget_is_not_converted_into_invalid_graph_or_candidate(tmp_path, phase):
    from graph_engine.construction.budget import deadline_scope
    from graph_engine.construction.sources import load_source, initial_graph
    from graph_engine.construction.representation import resolve_rows
    from graph_engine.construction.workload import GraphExecutor
    from graph_engine.construction.tools import fidelity_errors, transform
    from graph_engine.construction.domain import rank_candidates
    path = tmp_path / "people.csv"
    path.write_text("id,name\n1,Alice\n")
    source = load_source(path, {"primary_key": ["id"]})
    graph = initial_graph(source)
    operations = {
        "source": lambda: load_source(path), "initial": lambda: initial_graph(source),
        "representation": lambda: resolve_rows(graph), "executor": lambda: GraphExecutor(graph),
        "fidelity": lambda: fidelity_errors(source, graph),
        "ranking": lambda: rank_candidates([], {"key": "general"}, [], source, graph, []),
        "transform": lambda: transform(graph, source, "optimize_graph", {"operations": []}),
    }
    with deadline_scope(time.monotonic() - 1), pytest.raises(TimeoutError):
        operations[phase]()
