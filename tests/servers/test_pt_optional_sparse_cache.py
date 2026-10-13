import sys,types
import pytest
import torch
from graph_data_service.pt_compat import load_pt_payload
from graph_data_service.graph_data import GraphData


def write_legacy(path,monkeypatch,edge=True):
    root=types.ModuleType("torch_sparse");module=types.ModuleType("torch_sparse.tensor")
    cls=type("SparseTensor",(),{"__module__":"torch_sparse.tensor"})
    module.SparseTensor=cls
    monkeypatch.setitem(sys.modules,"torch_sparse",root)
    monkeypatch.setitem(sys.modules,"torch_sparse.tensor",module)
    payload={"num_nodes":3,"x":torch.ones(3,2),"y":torch.tensor([0,1,0]),
             "label_texts":["A","B"],"train_mask":torch.tensor([True,False,False]),
             "adj_t":cls()}
    if edge:payload["edge_index"]=torch.tensor([[0,1],[1,2]])
    torch.save(payload,path)
    monkeypatch.delitem(sys.modules,"torch_sparse.tensor")
    monkeypatch.delitem(sys.modules,"torch_sparse")


def test_optional_cache_load_preserves_graph_features_and_labels(tmp_path,monkeypatch):
    path=tmp_path/"legacy.pt";write_legacy(path,monkeypatch)
    payload=load_pt_payload(path)
    assert torch.equal(payload["edge_index"],torch.tensor([[0,1],[1,2]]))
    g=GraphData.load(str(path))
    assert (g.num_nodes,g.num_edges,g.node_feat_dim)==(3,2,2)
    assert g.label_names==["A","B"]
    assert g.train_mask.tolist()==[True,False,False]


def test_cache_cannot_replace_explicit_edge_index(tmp_path,monkeypatch):
    path=tmp_path/"opaque.pt";write_legacy(path,monkeypatch,edge=False)
    with pytest.raises(ValueError,match="edge_index"):
        load_pt_payload(path)
