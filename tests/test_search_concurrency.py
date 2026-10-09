from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_data_service.search import EntitySearchIndex, NodeSearchRequest


class _TestEmbeddingProvider:
    name = "test-semantic"
    available = True
    dim = 2

    def __init__(self):
        self.blocked = Event()
        self.release = Event()

    def encode(self, texts):
        if texts == ["alpha blocked"]:
            self.blocked.set()
            if not self.release.wait(5):
                raise TimeoutError("Test embedding was not released")
        return np.asarray([[1.0, 0.0] if "alpha" in text else [0.0, 1.0]
                           for text in texts], dtype=np.float32)


class _YieldDuringEviction(dict):
    """Expose a thread switch between selecting an eviction key and removal."""

    def __iter__(self):
        key = next(super().__iter__())
        # CPython normally executes this tiny window too quickly to exercise
        # consistently; widening it reproduces competing eviction selections.
        time.sleep(0.002)
        yield key


class SearchConcurrencyTests(unittest.TestCase):
    def setUp(self):
        graph = GraphData(
            num_nodes=2, edge_index=np.array([[0], [1]]),
            id2node={0: "A", 1: "B"}, node2id={"A": 0, "B": 1},
            node_text=["alpha", "beta"],
        )
        self.provider = _TestEmbeddingProvider()
        self.index = EntitySearchIndex(graph, 1, self.provider)

    def test_parallel_searches_evict_without_errors_or_unbounded_growth(self):
        for index in range(128):
            self.index._query_vector(f"warmup {index}")
        self.index._query_vec_cache = _YieldDuringEviction(self.index._query_vec_cache)

        def search(number):
            prefix = "alpha" if number % 2 == 0 else "beta"
            result = self.index.search_nodes(NodeSearchRequest(query=f"{prefix} query {number}"))
            return number, result

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(search, range(64)))

        self.assertEqual(len(results), 64)
        self.assertLessEqual(len(self.index._query_vec_cache), 128)
        for number, result in results:
            self.assertTrue(result["success"])
            self.assertEqual(result["best"]["node_name"], "A" if number % 2 == 0 else "B")

    def test_slow_embedding_does_not_hold_up_an_existing_cache_hit(self):
        request = NodeSearchRequest(query="beta cached")
        expected = self.index.search_nodes(request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked = pool.submit(self.index.search_nodes, NodeSearchRequest(query="alpha blocked"))
            try:
                self.assertTrue(self.provider.blocked.wait(2))
                cached = pool.submit(self.index.search_nodes, request)
                self.assertEqual(cached.result(timeout=2), expected)
                self.assertFalse(blocked.done())
            finally:
                self.provider.release.set()
            self.assertEqual(blocked.result(timeout=2)["best"]["node_name"], "A")


if __name__ == "__main__":
    unittest.main()

def test_duplicate_property_names_remain_ambiguous_even_when_top_k_is_one():
    from graph_data_service.embedding import LexicalProvider
    graph = GraphData(
        num_nodes=3, edge_index=np.array([[0, 1], [2, 2]]),
        id2node={0: "a", 1: "b", 2: "c"}, node2id={"a": 0, "b": 1, "c": 2},
        node_type=["Person", "Organization", "Person"],
        graph_metadata={"construction_graph": {"nodes": [
            {"id": "a", "properties": {"data": {"name": "同名"}}},
            {"id": "b", "properties": {"name": "同名"}},
            {"id": "c", "properties": {"name": "目标"}},
        ]}},
    )
    index = EntitySearchIndex(graph, 1, LexicalProvider())
    result = index.search_nodes(NodeSearchRequest(query="同名", top_k=1))
    assert result["ambiguous"] and result["best"]["confidence"] == "medium"
    filtered = index.search_nodes(NodeSearchRequest(query="同名", filters={"node_type": "Person"}))
    assert not filtered["ambiguous"] and filtered["best"]["node_name"] == "a"
    # Internal IDs still take precedence and remain valid tool arguments.
    assert index.search_nodes(NodeSearchRequest(query="b"))["best"]["node_id"] == 1
    from graph_data_service.search import EdgeSearchRequest
    edges = index.search_edges(EdgeSearchRequest(src_query="同名", dst_query="目标"))
    assert {(row["src_name"], row["dst_name"]) for row in edges["candidates"]} == {("a", "c"), ("b", "c")}
