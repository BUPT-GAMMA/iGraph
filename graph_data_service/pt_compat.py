"""Read a redundant torch_sparse cache without requiring its compiled extension.

The actual graph must still provide an explicit edge_index. No sparse methods
are emulated and no adjacency is inferred from an opaque cache.
"""
import pickle
import types


class _SparseCache:
    pass


class _CacheUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in {("torch_sparse.tensor", "SparseTensor"),
                              ("torch_sparse.storage", "SparseStorage")}:
            return _SparseCache
        return super().find_class(module, name)


def load_pt_payload(path):
    import torch
    try:
        return torch.load(path, map_location="cpu", weights_only=False)
    except ModuleNotFoundError as exc:
        if not (exc.name == "torch_sparse" or (exc.name or "").startswith("torch_sparse.")):
            raise
    reader = types.SimpleNamespace(Unpickler=_CacheUnpickler, load=pickle.load,
                                   loads=pickle.loads, __name__="igraph_sparse_cache")
    payload = torch.load(path, map_location="cpu", weights_only=False, pickle_module=reader)
    edge_index = payload.get("edge_index") if isinstance(payload, dict) else getattr(payload, "edge_index", None)
    if not isinstance(edge_index, torch.Tensor) or edge_index.ndim != 2 or edge_index.shape[0] != 2:
        raise ValueError("A PT file with an optional torch_sparse cache must also contain explicit edge_index [2,E].")
    return payload
