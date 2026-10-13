"""First-graph selection, evidence retention, and non-destructive preset upgrades."""
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_engine.borylation_preset import RESOURCES, build_borylation_graph, install_borylation_preset
from graph_engine.bundled_presets import install_bundled_preset


def test_only_first_source_graph_is_needed(tmp_path):
    (tmp_path / 'graphs').mkdir()
    (tmp_path / 'processed').mkdir()
    for name in ('graphs/BO-01.json', 'processed/system_case.json'):
        shutil.copyfile(RESOURCES / name, tmp_path / name)
    graph = build_borylation_graph(tmp_path)
    original = GraphData.load(str(RESOURCES / 'graphs/BO-01.json'))
    bundled = GraphData.load(str(RESOURCES / 'borylation-story.npz'))
    assert (graph.num_nodes, graph.num_edges, graph.num_unique_undirected_edges) == (22, 48, 24)
    assert graph.semantic_type == 'undirected'
    assert graph.graph_metadata['loaded_graph_count'] == 1
    assert set(graph.node2id) == {f'BO-01:atom:{i}' for i in range(22)}
    assert {n['type'] for n in graph.graph_metadata['construction_graph']['nodes']} == {'atom'}
    assert {e['type'] for e in graph.graph_metadata['construction_graph']['edges']} == {'chemical_bond'}
    for actual in (graph, bundled):
        np.testing.assert_array_equal(actual.edge_index, original.edge_index)
        np.testing.assert_array_equal(actual.node_feat, original.node_feat)
        np.testing.assert_array_equal(actual.edge_feat, original.edge_feat)
        evidence = actual.graph_metadata['borylation_case']
        assert len(evidence['condition_records']) == 96
        assert len(evidence['products']) == 3


def test_upgrade_keeps_backup_old_versions_and_custom_names(tmp_path, monkeypatch):
    monkeypatch.delenv('GRAPH_ENGINE_SERVER_DATASETS_DIR', raising=False)
    resources = tmp_path / 'resources'
    resources.mkdir()
    manifest = json.loads((RESOURCES / 'manifest.json').read_text())
    old = b'previous official graph snapshot'
    old_hash = hashlib.sha256(old).hexdigest()
    (resources / manifest['filename']).write_bytes(old)
    old_manifest = {**manifest, 'sha256': old_hash, 'previous_sha256': []}
    (resources / 'manifest.json').write_text(json.dumps(old_manifest))
    data_root = tmp_path / 'data'
    receipt = install_bundled_preset(data_root, resources=resources)
    directory = data_root / 'global/datasets' / receipt['dataset_id']
    meta = json.loads((directory / 'meta.json').read_text())
    meta['name'] = 'My renamed molecule'
    (directory / 'meta.json').write_text(json.dumps(meta))
    shutil.copyfile(RESOURCES / manifest['filename'], resources / manifest['filename'])
    manifest['previous_sha256'] = [old_hash]
    (resources / 'manifest.json').write_text(json.dumps(manifest))
    install_borylation_preset(data_root, resources=resources)
    assert (directory / 'versions/v001.npz').read_bytes() == old
    assert (directory / '.preset-backups' / old_hash / 'graph.npz').read_bytes() == old
    assert GraphData.load(str(directory / 'graph.npz')).num_nodes == 22
    public = data_root / 'server_datasets' / manifest['filename']
    assert public.read_bytes() == (resources / manifest['filename']).read_bytes()
    assert (public.parent / '.preset-backups' / old_hash / public.name).read_bytes() == old
    meta = json.loads((directory / 'meta.json').read_text())
    assert meta['name'] == 'My renamed molecule'
    assert (meta['node_count'], meta['edge_count'], meta['is_directed']) == (22, 48, False)
    assert meta['versions'] == ['v001', 'v002']
    install_borylation_preset(data_root, resources=resources)
    assert json.loads((directory / 'meta.json').read_text()) == meta
    (directory / 'graph.npz').write_bytes(b'user graph edits')
    public.write_bytes(b'user public graph edits')
    install_borylation_preset(data_root, resources=resources)
    assert (directory / 'graph.npz').read_bytes() == b'user graph edits'
    assert public.read_bytes() == b'user public graph edits'
