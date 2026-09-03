from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from graph_data_service.graph_data import GraphData


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class DatasetMeta:
    dataset_id: str
    name: str
    description: str
    source_type: str
    node_count: int
    edge_count: int
    is_directed: bool
    created_at: str
    updated_at: str
    versions: list[str]
    edge_entry_count: int = 0
    unique_edge_count: int = 0
    num_unique_directed_edges: int = 0
    num_unique_undirected_edges: int = 0
    semantic_type: str = "unknown"
    storage_type: str = "mixed"
    is_symmetric: bool = False
    has_self_loops: bool = False
    has_duplicate_entries: bool = False
    storage_backend: str = "local"
    graph_ref: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetRegistry:
    def __init__(self, base_dir: str = "datasets") -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _dataset_dir(self, dataset_id: str) -> Path:
        return self._base_dir / dataset_id

    def _meta_path(self, dataset_id: str) -> Path:
        return self._dataset_dir(dataset_id) / "meta.json"

    def _graph_path(self, dataset_id: str) -> Path:
        return self._dataset_dir(dataset_id) / "graph.npz"

    def create(
        self,
        name: str,
        description: str,
        graph_data: GraphData,
        *,
        source_type: str = "graph",
    ) -> DatasetMeta:
        dataset_id = f"ds_{uuid.uuid4().hex[:10]}"
        dataset_dir = self._dataset_dir(dataset_id)
        versions_dir = dataset_dir / "versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        self._graph_path(dataset_id).parent.mkdir(parents=True, exist_ok=True)
        self._graph_path(dataset_id).unlink(missing_ok=True)
        graph_data.save(str(self._graph_path(dataset_id)))
        version = "v001"
        graph_data.save(str(versions_dir / f"{version}.npz"))
        now = _now_iso()
        meta = DatasetMeta(
            dataset_id=dataset_id,
            name=name.strip() or dataset_id,
            description=description.strip(),
            source_type=source_type,
            node_count=graph_data.num_nodes,
            edge_count=graph_data.num_edges,
            is_directed=graph_data.directed,
            created_at=now,
            updated_at=now,
            versions=[version],
            edge_entry_count=graph_data.num_edge_entries,
            unique_edge_count=graph_data.num_unique_edges,
            num_unique_directed_edges=graph_data.num_unique_directed_edges,
            num_unique_undirected_edges=graph_data.num_unique_undirected_edges,
            semantic_type=graph_data.semantic_type,
            storage_type=graph_data.storage_type,
            is_symmetric=graph_data.is_symmetric,
            has_self_loops=graph_data.has_self_loops,
            has_duplicate_entries=graph_data.has_duplicate_entries,
        )
        self._save_meta(meta)
        return meta

    def create_remote(
        self,
        name: str,
        description: str,
        *,
        graph_ref: dict,
        source_type: str = "nebula",
    ) -> DatasetMeta:
        """Register a Nebula graph reference without copying it to NPZ."""
        dataset_id = f"ds_{uuid.uuid4().hex[:10]}"
        dataset_dir = self._dataset_dir(dataset_id)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        meta = DatasetMeta(
            dataset_id=dataset_id,
            name=name.strip() or dataset_id,
            description=description.strip(),
            source_type=source_type,
            node_count=int(graph_ref.get("node_count", 0) or 0),
            edge_count=int(graph_ref.get("edge_count", 0) or 0),
            is_directed=bool(graph_ref.get("directed", True)),
            created_at=now,
            updated_at=now,
            versions=["v001"],
            storage_backend="nebula",
            graph_ref=dict(graph_ref),
        )
        self._save_meta(meta)
        return meta

    def list_datasets(self) -> list[DatasetMeta]:
        rows: list[DatasetMeta] = []
        for child in sorted(self._base_dir.iterdir()):
            if not child.is_dir():
                continue
            meta = self.get(child.name, required=False)
            if meta is not None:
                rows.append(meta)
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        return rows

    def get(self, dataset_id: str, *, required: bool = True) -> DatasetMeta | None:
        path = self._meta_path(dataset_id)
        if not path.exists():
            if required:
                raise FileNotFoundError(f"dataset not found: {dataset_id}")
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DatasetMeta(**payload)

    def load_graph(self, dataset_id: str, version: str = "latest") -> GraphData:
        meta = self.get(dataset_id)
        if meta is None:
            raise FileNotFoundError(dataset_id)
        if meta.storage_backend == "nebula":
            raise ValueError(
                f"dataset {dataset_id} is Nebula-backed and must be activated by GraphDataService"
            )
        if version == "latest":
            return GraphData.load(str(self._graph_path(dataset_id)))
        version_path = self._dataset_dir(dataset_id) / "versions" / f"{version}.npz"
        if not version_path.exists():
            raise FileNotFoundError(f"dataset version not found: {dataset_id}/{version}")
        return GraphData.load(str(version_path))

    def save_version(self, dataset_id: str, graph_data: GraphData) -> DatasetMeta:
        meta = self.get(dataset_id)
        next_num = len(meta.versions) + 1
        version = f"v{next_num:03d}"
        graph_data.save(str(self._dataset_dir(dataset_id) / "versions" / f"{version}.npz"))
        graph_data.save(str(self._graph_path(dataset_id)))
        meta.versions.append(version)
        meta.node_count = graph_data.num_nodes
        meta.edge_count = graph_data.num_edges
        meta.is_directed = graph_data.directed
        meta.edge_entry_count = graph_data.num_edge_entries
        meta.unique_edge_count = graph_data.num_unique_edges
        meta.num_unique_directed_edges = graph_data.num_unique_directed_edges
        meta.num_unique_undirected_edges = graph_data.num_unique_undirected_edges
        meta.semantic_type = graph_data.semantic_type
        meta.storage_type = graph_data.storage_type
        meta.is_symmetric = graph_data.is_symmetric
        meta.has_self_loops = graph_data.has_self_loops
        meta.has_duplicate_entries = graph_data.has_duplicate_entries
        meta.updated_at = _now_iso()
        self._save_meta(meta)
        return meta

    def delete(self, dataset_id: str) -> bool:
        dataset_dir = self._dataset_dir(dataset_id)
        if not dataset_dir.exists():
            return False
        shutil.rmtree(dataset_dir)
        return True

    def _save_meta(self, meta: DatasetMeta) -> None:
        path = self._meta_path(meta.dataset_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
