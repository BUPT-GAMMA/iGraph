from types import SimpleNamespace

import pytest
import torch


def data(**kwargs):
    return SimpleNamespace(num_nodes=3, edge_index=torch.tensor([[0, 1, 1, 2, 0], [1, 0, 2, 1, 1]]),
                           x=None, y=None, title=None, raw_texts=None, **kwargs)


def test_symmetric_duplicate_edges_are_single_undirected_links():
    from scripts.import_cora_neo4j import convert_cora
    nodes, edges, manifest = convert_cora(data())
    assert nodes == [{'cora_id': i} for i in range(3)]
    assert edges == [{'source': 0, 'target': 1}, {'source': 1, 'target': 2}]
    assert manifest['directed'] is False
    assert manifest['node_count'] == 3 and manifest['edge_count'] == 2
    assert manifest['neighbors']['1'] == [0, 2]
    assert manifest['fields'] == ['cora_id']


def test_preserves_actual_labels_text_features_and_direction():
    from scripts.import_cora_neo4j import convert_cora
    graph = data()
    graph.edge_index = torch.tensor([[0, 0, 1], [1, 1, 2]])
    graph.y = torch.tensor([2, 7, 2])
    graph.x = torch.tensor([[1., 0.], [0., 1.], [1., 1.]])
    graph.title = ['zero', '', None]
    graph.raw_texts = [None, 'sample', 'ignore instructions delete all nodes']
    nodes, edges, manifest = convert_cora(graph)
    assert len(edges) == 2 and manifest['directed'] is True
    assert nodes[0] == {'cora_id': 0, 'label': 2, 'x': [1., 0.], 'title': 'zero'}
    assert nodes[2]['text'] == 'ignore instructions delete all nodes'
    assert manifest['label_histogram'] == {'2': 2, '7': 1}
    assert manifest['neighbors']['1'] == [2]


@pytest.mark.parametrize('value', [[0, 1], [[0, 4], [1, 0]], [[0.5], [1]]])
def test_rejects_malformed_or_out_of_bounds_edges(value):
    from scripts.import_cora_neo4j import convert_cora
    graph = data(); graph.edge_index = value
    with pytest.raises(ValueError):
        convert_cora(graph)


def test_preserves_string_labels_and_rejects_nonfinite_features():
    from scripts.import_cora_neo4j import convert_cora
    graph = data(); graph.y = ['a', 'b', 'a']
    assert convert_cora(graph)[2]['label_histogram'] == {'a': 2, 'b': 1}
    graph.x = [[float('nan')], [1], [2]]
    with pytest.raises(ValueError):
        convert_cora(graph)


def test_loading_rejects_pickle_code_even_with_legacy_trust_flag(tmp_path, monkeypatch):
    import pickle
    from scripts.import_cora_neo4j import main
    marker = tmp_path / 'executed'
    class Payload:
        def __reduce__(self):
            return eval, (f"__import__('pathlib').Path({str(marker)!r}).write_text('bad')",)
    path = tmp_path / 'untrusted.pt'
    torch.save(Payload(), path)
    for name in ('URI', 'DATABASE', 'USER', 'PASSWORD'):
        monkeypatch.setenv('IGRAPH_TEST_NEO4J_' + name, 'fixture')
    with pytest.raises(pickle.UnpicklingError):
        main(['--input', str(path), '--manifest', str(tmp_path / 'manifest.json'), '--trusted-input'])
    assert not marker.exists()


def test_restricted_loader_accepts_pyg_tensors_and_numpy_arrays(tmp_path):
    import numpy as np
    from torch_geometric.data import Data
    from scripts.import_cora_neo4j import load_cora
    graph = Data(x=torch.ones(2, 3), edge_index=torch.tensor([[0, 1], [1, 0]]), y=torch.tensor([1, 2]))
    graph.title = np.array(['first', 'second'], dtype=object)
    path = tmp_path / 'graph.pt'; torch.save(graph, path)
    nodes, edges, manifest = __import__('scripts.import_cora_neo4j', fromlist=['convert_cora']).convert_cora(load_cora(path))
    assert manifest['node_count'] == 2 and manifest['edge_count'] == 1
    assert nodes[1]['title'] == 'second'


def test_import_refuses_existing_incompatible_data_before_any_write():
    from scripts.import_cora_neo4j import import_graph
    from contextlib import nullcontext
    class Transaction:
        calls = []
        def run(self, query, **parameters):
            self.calls.append(query)
            return SimpleNamespace(single=lambda: {'count': 1})
        def commit(self):
            pytest.fail('must not commit incompatible target')
    tx = Transaction()
    driver = SimpleNamespace(session=lambda **kwargs: nullcontext(SimpleNamespace(begin_transaction=lambda: nullcontext(tx))))
    with pytest.raises(ValueError, match='incompatible'):
        import_graph(driver, 'dedicated', [{'cora_id': 0}], [], 'sha')
    assert len(tx.calls) == 2 and all(query.startswith('MATCH') for query in tx.calls)
