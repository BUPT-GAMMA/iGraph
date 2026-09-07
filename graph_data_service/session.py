"""
GraphSession — 单会话图数据状态容器（纯存储）

只负责 GraphData 的装载、覆盖、清理与版本管理，不执行图算法计算。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

import graph_engine.paths as _dp
from graph_data_service.graph_data import GraphData
from graph_data_service.graph_snapshot import GraphSnapshot, snapshot_summary
from execution_lifecycle import atomic_json

logger = logging.getLogger(__name__)


@dataclass
class GraphSessionSummary:
    loaded: bool
    graph_version: int
    num_nodes: int = 0
    num_edges: int = 0
    num_edge_entries: int = 0
    num_unique_edges: int = 0
    num_unique_directed_edges: int = 0
    num_unique_undirected_edges: int = 0
    semantic_type: str = "unknown"
    storage_type: str = "mixed"
    is_symmetric: bool = False
    has_self_loops: bool = False
    has_duplicate_entries: bool = False
    directed: bool = True
    has_node_names: bool = False
    has_edge_weight: bool = False
    has_node_text: bool = False
    has_split_masks: bool = False
    message: str = ""


@dataclass
class UploadFileRecord:
    file_token: str
    original_filename: str
    saved_path: str


class GraphSession:
    """单会话图状态，负责 GraphData 读写与版本号。"""

    def __init__(self, session_id: str, user_id: str = "") -> None:
        self.session_id = session_id
        self.user_id = user_id.strip()
        self.llm_model: str = ""
        self._graph: Optional[GraphData] = None
        # Nebula primary mode keeps only a durable graph reference in the
        # session. Bounded GraphData objects are materialised on demand.
        self._remote_graph: Optional[dict] = None
        self._graph_version: int = 0
        self._uploads_by_name: Dict[str, UploadFileRecord] = {}
        if self.user_id:
            storage_root = _dp.user_sessions_dir(self.user_id)
            storage_root.mkdir(parents=True, exist_ok=True)
            self._session_dir = storage_root / session_id
        else:
            self._session_dir = Path(self._graph_storage_root()) / session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots_dir = self._session_dir / "snapshots"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("GraphSession created  session_id=%s  user_id=%s", session_id, self.user_id)

    @property
    def graph_version(self) -> int:
        return self._graph_version

    def _bump_version(self) -> None:
        max_existing = self._graph_version
        for path in self._snapshots_dir.glob("v*.npz"):
            try:
                max_existing = max(max_existing, int(path.stem[1:]))
            except Exception:
                continue
        for rec in self._load_version_records():
            try:
                max_existing = max(max_existing, int(rec.get("graph_version", 0)))
            except Exception:
                continue
        self._graph_version = max_existing + 1

    def set_graph(self, gd: GraphData, description: str = "") -> None:
        gd.ensure_node_names()
        self._graph = gd
        self._remote_graph = None
        self._bump_version()
        self.save_snapshot(description=description or "更新图")

    def publish_snapshot(self, source, summary, *, expected_version, receipt, description='任务计算结果', activate_version=None, discard_version=None, before_commit=None):
        """Short commit: the worker has finished all graph-sized I/O/validation.

        graph_versions.json is the authoritative commit boundary. Unlisted
        staged/orphan files never become visible versions after a crash.
        Call under the session write lock after the executor-generation check.
        """
        if self.graph_version != expected_version:
            raise ValueError('GRAPH_VERSION_CHANGED')
        version = int(activate_version) if activate_version is not None else self.next_graph_version()
        target = self._snapshots_dir / f'v{version:03d}.npz'
        candidate = GraphSnapshot(target, summary)
        records = self._load_version_records()
        manifest = json.loads(self._versions_path().read_text()) if self._versions_path().is_file() else {}
        if not isinstance(manifest, dict) or manifest.get('format') != 2:
            # Upgrade visibility before moving a candidate file, so even the
            # first failed publication cannot expose an uncommitted orphan.
            atomic_json(self._versions_path(), {'format': 2, 'session_id': self.session_id,
                'current_graph_version': self.graph_version, 'versions': records})
        receipt = {**receipt, 'result': {**receipt.get('result', {}), 'graph_version': version}}
        if activate_version is None:
            os.replace(source, target)
            records = [row for row in records if int(row['graph_version']) != version]
            records.append({'graph_version': version, 'description': description,
                'created_at': self._now_iso(), 'loaded': True,
                'node_count': summary['num_nodes'], 'edge_count': summary['num_edges'],
                'edge_entry_count': summary['num_edge_entries'], 'unique_edge_count': summary['num_unique_edges'],
                **{key: summary[key] for key in ('num_unique_directed_edges', 'num_unique_undirected_edges',
                    'semantic_type', 'storage_type', 'is_symmetric', 'has_self_loops', 'has_duplicate_entries')},
                'is_directed': summary['directed'], 'has_weights': summary['has_edge_weight'],
                'snapshot': str(target), 'summary': summary, 'commit_receipt': receipt})
        elif not target.is_file():
            raise ValueError('GRAPH_VERSION_NOT_FOUND')
        else:
            records = [{**row, 'summary': summary} if int(row['graph_version']) == version else row for row in records]
        if discard_version is not None:
            if int(discard_version) == version:
                raise ValueError('CANNOT_DISCARD_ACTIVE_GRAPH')
            records = [row for row in records if int(row['graph_version']) != int(discard_version)]
        atomic_json(self._versions_path(), {'format': 2, 'session_id': self.session_id,
            'current_graph_version': version, 'versions': records, 'commit_receipt': receipt}, before_replace=before_commit)
        # Once published, cancellation preserves this result. No graph-sized
        # allocation or write occurs between publication and in-memory adoption.
        self._graph, self._remote_graph, self._graph_version = candidate, None, version
        if discard_version is not None:
            try:
                (self._snapshots_dir / f'v{int(discard_version):03d}.npz').unlink(missing_ok=True)
            except OSError:
                logger.warning('discarded_snapshot_cleanup_pending session_id=%s version=%s', self.session_id, discard_version)
        try:
            self._write_session_meta(candidate)
        except OSError:
            logger.warning('graph_metadata_sync_pending session_id=%s', self.session_id)
        return version

    def next_graph_version(self) -> int:
        """Return the next durable version without mutating session state."""
        max_existing = self._graph_version
        for path in self._snapshots_dir.glob("v*.npz"):
            try:
                max_existing = max(max_existing, int(path.stem[1:]))
            except Exception:
                continue
        for rec in self._load_version_records():
            try:
                max_existing = max(max_existing, int(rec.get("graph_version", 0)))
            except Exception:
                continue
        return max_existing + 1

    @property
    def remote_graph(self) -> Optional[dict]:
        return dict(self._remote_graph) if self._remote_graph is not None else None

    @property
    def is_remote(self) -> bool:
        return self._remote_graph is not None

    def set_remote_graph(
        self,
        *,
        graph_key: str,
        graph_version: int,
        node_count: int,
        edge_count: int,
        directed: bool,
        graph_name: str = "",
        graph_desc: str = "",
        has_weights: bool = False,
        description: str = "",
        record_version: bool = True,
    ) -> None:
        """Attach a Nebula-backed graph without retaining a full GraphData."""
        self._graph = None
        self._graph_version = int(graph_version)
        self._remote_graph = {
            "backend": "nebula",
            "graph_key": str(graph_key),
            "graph_version": int(graph_version),
            "node_count": int(node_count),
            "edge_count": int(edge_count),
            "directed": bool(directed),
            "graph_name": str(graph_name or ""),
            "graph_desc": str(graph_desc or ""),
            "has_weights": bool(has_weights),
        }
        if record_version:
            records = [
                row for row in self._load_version_records()
                if int(row.get("graph_version", -1)) != self._graph_version
            ]
            records.append(
                {
                    "graph_version": self._graph_version,
                    "description": description or "NebulaGraph 图版本",
                    "created_at": self._now_iso(),
                    "loaded": True,
                    "node_count": int(node_count),
                    "edge_count": int(edge_count),
                    "is_directed": bool(directed),
                    "has_weights": bool(has_weights),
                    "snapshot": "",
                    "backend": "nebula",
                    "graph_key": str(graph_key),
                }
            )
            self._write_version_records(records)
        else:
            # Keep the persisted current-version pointer in sync when merely
            # activating an existing Nebula graph version.
            self._write_version_records(self._load_version_records())
        self._write_session_meta(None)

    def clear_remote_graph(self, description: str = "清空图") -> None:
        self._graph = None
        self._remote_graph = None
        self._graph_version = self.next_graph_version()
        records = [
            row for row in self._load_version_records()
            if int(row.get("graph_version", -1)) != self._graph_version
        ]
        records.append(
            {
                "graph_version": self._graph_version,
                "description": description,
                "created_at": self._now_iso(),
                "loaded": False,
                "node_count": 0,
                "edge_count": 0,
                "is_directed": True,
                "has_weights": False,
                "snapshot": "",
                "backend": "nebula",
                "graph_key": "",
            }
        )
        self._write_version_records(records)
        self._write_session_meta(None)

    def _parse_edgelist(self, text: str, append: bool = False) -> Tuple[int, int]:
        """从文本边列表建图，返回 (节点数, 边数)。"""
        raw_edges: List[Tuple[str, str, float]] = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            src, dst = parts[0], parts[1]
            weight = float(parts[2]) if len(parts) >= 3 else 1.0
            raw_edges.append((src, dst, weight))

        if append and self._graph is not None:
            node2id: Dict[str, int] = dict(self._graph.node2id)
            id2node: Dict[int, str] = dict(self._graph.id2node)
            all_pairs: List[Tuple[int, int]] = list(self._graph.all_edges())
            all_weights: List[float] = [
                (w if (w := self._graph.get_edge_weight(s, d)) is not None else 1.0)
                for s, d in all_pairs
            ]
        else:
            node2id = {}
            id2node = {}
            all_pairs = []
            all_weights = []

        next_id = len(id2node)
        for src_name, dst_name, weight in raw_edges:
            for name in (src_name, dst_name):
                if name not in node2id:
                    node2id[name] = next_id
                    id2node[next_id] = name
                    next_id += 1
            all_pairs.append((node2id[src_name], node2id[dst_name]))
            all_weights.append(weight)

        if not all_pairs:
            graph = GraphData(
                num_nodes=next_id,
                id2node=id2node or None,
                node2id=node2id or None,
            )
        else:
            edge_index = np.array(all_pairs, dtype=np.int64).T
            edge_weight = np.array(all_weights, dtype=np.float32)
            graph = GraphData(
                num_nodes=next_id,
                edge_index=edge_index,
                id2node=id2node or None,
                node2id=node2id or None,
                edge_weight=edge_weight,
            )

        self.set_graph(graph, description="从边列表文本加载图" + ("（追加）" if append else ""))
        n, e = graph.num_nodes, graph.num_edges
        logger.info(
            "_parse_edgelist  session=%s  nodes=%d  edges=%d  append=%s  version=%d",
            self.session_id, n, e, append, self.graph_version,
        )
        return n, e

    def load_graph_from_upload_path(self, saved_path: str, append: bool = False) -> Tuple[int, int]:
        """从上传文件路径加载图，支持文本格式、.npz 与 .json（graph_data.v1）格式。"""
        resolved_path = self._resolve_existing_upload_path(saved_path)
        if resolved_path is None:
            raise FileNotFoundError(f"文件不存在: {saved_path}")

        lower_path = resolved_path.lower()
        if lower_path.endswith(".npz") or lower_path.endswith(".pt"):
            loaded = GraphData.load(resolved_path)
            if append and self._graph is not None:
                existing_text = self._graph_to_text(self._graph)
                new_text = self._graph_to_text(loaded)
                return self._parse_edgelist(existing_text + "\n" + new_text, append=False)
            self.set_graph(loaded, description=f"从上传文件加载图: {os.path.basename(resolved_path)}")
            n, e = loaded.num_nodes, loaded.num_edges
            logger.info(
                "load_graph_from_upload_path  session=%s  file=%s  nodes=%d  edges=%d  append=%s  version=%d",
                self.session_id, resolved_path, n, e, append, self.graph_version,
            )
            return n, e

        if lower_path.endswith(".json"):
            # 先尝试 graph_data.v1 格式；若不匹配则 fallback 到文本边列表解析
            try:
                loaded = GraphData.load(resolved_path)
                if append and self._graph is not None:
                    existing_text = self._graph_to_text(self._graph)
                    new_text = self._graph_to_text(loaded)
                    return self._parse_edgelist(existing_text + "\n" + new_text, append=False)
                self.set_graph(loaded, description=f"从上传文件加载图: {os.path.basename(resolved_path)}")
                n, e = loaded.num_nodes, loaded.num_edges
                logger.info(
                    "load_graph_from_upload_path  session=%s  file=%s  nodes=%d  edges=%d  append=%s  version=%d",
                    self.session_id, resolved_path, n, e, append, self.graph_version,
                )
                return n, e
            except (ValueError, KeyError):
                logger.info(
                    "load_graph_from_upload_path  session=%s  file=%s  不是 graph_data.v1 格式，尝试文本解析",
                    self.session_id, resolved_path,
                )

        with open(resolved_path, "r", encoding="utf-8") as f:
            text = f.read()
        return self._parse_edgelist(text, append=append)

    @staticmethod
    def _graph_to_text(gd: GraphData) -> str:
        """将 GraphData 序列化为边列表文本（供内部合并使用）。"""
        lines = []
        id2node = gd.id2node
        src_arr, dst_arr = gd.edge_index
        for i in range(gd.num_edges):
            s, d = int(src_arr[i]), int(dst_arr[i])
            sname = id2node.get(s, str(s))
            dname = id2node.get(d, str(d))
            w = float(gd.edge_weight[i]) if gd.edge_weight is not None else 1.0
            lines.append(f"{sname} {dname} {w}")
        return "\n".join(lines)

    def clear_graph(self) -> None:
        self._graph = None
        self._remote_graph = None
        self._bump_version()
        self.save_snapshot(description="清空图")
        logger.info("clear_graph  session=%s  version=%d", self.session_id, self.graph_version)

    def register_upload(
        self,
        file_token: str,
        original_filename: str,
        saved_path: str,
    ) -> None:
        """注册会话上传文件映射（文件名 -> 实际保存路径）。"""
        normalized_saved_path = self._resolve_existing_upload_path(saved_path) or saved_path
        self._uploads_by_name[original_filename] = UploadFileRecord(
            file_token=file_token,
            original_filename=original_filename,
            saved_path=normalized_saved_path,
        )
        self._write_session_meta(self._graph)
        logger.info(
            "register_upload  session=%s  filename=%s  token=%s  path=%s",
            self.session_id,
            original_filename,
            file_token,
            normalized_saved_path,
        )

    def resolve_upload_by_name(self, filename: str) -> Optional[UploadFileRecord]:
        return self._uploads_by_name.get(filename)

    def mark_version_upload_source(
        self,
        graph_version: int,
        upload: UploadFileRecord,
    ) -> bool:
        """Persist the upload identity that produced a graph version."""
        records = self._load_version_records()
        updated = False
        for row in records:
            if int(row.get("graph_version", -1)) != int(graph_version):
                continue
            row["source"] = {
                "kind": "upload",
                "file_token": str(upload.file_token),
                "original_filename": str(upload.original_filename),
                "saved_name": os.path.basename(upload.saved_path),
            }
            updated = True
            break
        if updated:
            self._write_version_records(records)
        return updated

    def nebula_versions_for_upload(
        self,
        upload: UploadFileRecord,
    ) -> list[dict]:
        """Return reusable Nebula versions produced by the same upload.

        Older records predate explicit source metadata, so their exact saved
        filename in the description is accepted as a backwards-compatible
        match. A newly uploaded file with the same display name has a new token
        and therefore is not mistaken for the previous upload.
        """
        expected_description = (
            f"从上传文件加载图: {os.path.basename(upload.saved_path)}"
        )
        matches: list[dict] = []
        for row in self.list_versions():
            if (
                row.get("backend") != "nebula"
                or not row.get("loaded")
                or not row.get("graph_key")
            ):
                continue
            source = (
                row.get("source")
                if isinstance(row.get("source"), dict)
                else {}
            )
            token = str(source.get("file_token") or "")
            if token == str(upload.file_token) or (
                not token
                and str(row.get("description") or "") == expected_description
            ):
                matches.append(dict(row))
        return sorted(
            matches,
            key=lambda row: int(row.get("graph_version", 0)),
        )

    @staticmethod
    def _project_root() -> str:
        # __file__ lives at <project_root>/graph_data_service/session.py, so
        # the project root sits exactly two dirnames above this file.
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def _resolve_existing_upload_path(cls, saved_path: str) -> Optional[str]:
        if not saved_path:
            return None

        candidates: List[str] = []
        candidates.append(saved_path)
        candidates.append(os.path.realpath(saved_path))
        candidates.append(os.path.abspath(saved_path))

        if not os.path.isabs(saved_path):
            candidates.append(os.path.abspath(saved_path))
            candidates.append(os.path.join(cls._project_root(), saved_path))

        project_root = cls._project_root()
        filename = os.path.basename(saved_path)
        if filename:
            # 兼容多种部署形态：项目根目录上传目录、旧目录结构、网关子目录等。
            candidates.append(os.path.join(project_root, "uploads", filename))
            candidates.append(os.path.join(project_root, "backend", "uploads", filename))
            candidates.append(
                os.path.join(project_root, "backend", "api_gateway", "uploads", filename)
            )

            for env_name in ("API_UPLOAD_DIR", "GRAPH_UPLOAD_DIR", "GRAPH_DATA_UPLOAD_DIR"):
                upload_dir = os.getenv(env_name)
                if not upload_dir:
                    continue
                candidates.append(os.path.join(upload_dir, filename))
                candidates.append(os.path.join(project_root, upload_dir, filename))
                if not os.path.isabs(saved_path):
                    candidates.append(os.path.join(upload_dir, saved_path))
                    candidates.append(os.path.join(project_root, upload_dir, saved_path))

        seen = set()
        for path in candidates:
            norm = os.path.realpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            if os.path.exists(norm):
                return norm
        return None

    def get_graph(self) -> Optional[GraphData]:
        return self._graph

    def has_graph(self) -> bool:
        return self._graph is not None or self._remote_graph is not None

    def graph_summary(self) -> GraphSessionSummary:
        if self._remote_graph is not None:
            return GraphSessionSummary(
                loaded=True,
                graph_version=self.graph_version,
                num_nodes=int(self._remote_graph.get("node_count", 0)),
                num_edges=int(self._remote_graph.get("edge_count", 0)),
                directed=bool(self._remote_graph.get("directed", True)),
                has_node_names=True,
                has_edge_weight=bool(self._remote_graph.get("has_weights", False)),
            )
        if self._graph is None:
            return GraphSessionSummary(
                loaded=False,
                graph_version=self.graph_version,
                message="尚未加载任何图数据。",
            )

        return GraphSessionSummary(
            loaded=True,
            graph_version=self.graph_version,
            num_nodes=self._graph.num_nodes,
            num_edges=self._graph.num_edges,
            num_edge_entries=self._graph.num_edge_entries,
            num_unique_edges=self._graph.num_unique_edges,
            num_unique_directed_edges=self._graph.num_unique_directed_edges,
            num_unique_undirected_edges=self._graph.num_unique_undirected_edges,
            semantic_type=self._graph.semantic_type,
            storage_type=self._graph.storage_type,
            is_symmetric=self._graph.is_symmetric,
            has_self_loops=self._graph.has_self_loops,
            has_duplicate_entries=self._graph.has_duplicate_entries,
            directed=self._graph.directed,
            has_node_names=self._graph.has_node_names,
            has_edge_weight=self._graph.has_edge_weight,
            has_node_text=self._graph.has_node_text,
            has_split_masks=self._graph.has_split_masks,
        )

    @staticmethod
    def _graph_storage_root() -> str:
        root = os.getenv("GRAPH_DATA_UPLOAD_DIR", "uploads/graph_data")
        return os.path.abspath(root)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _versions_path(self) -> Path:
        return self._session_dir / "graph_versions.json"

    def _load_version_records(self) -> list[dict]:
        path = self._versions_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("versions") if isinstance(payload, dict) else payload
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        except Exception:
            pass
        return []

    def _write_version_records(self, records: list[dict]) -> None:
        payload = {
            'format': 2,
            "session_id": self.session_id,
            "current_graph_version": self.graph_version,
            "versions": sorted(records, key=lambda r: int(r.get("graph_version", 0))),
        }
        atomic_json(self._versions_path(), payload)

    def _legacy_session_dirs(self) -> list[Path]:
        if not self.user_id:
            return []
        legacy = _dp.user_uploads_dir(self.user_id) / self.session_id
        if legacy.resolve() == self._session_dir.resolve():
            return []
        return [legacy]

    def delete_storage(self) -> None:
        """Delete only storage owned by this conversation runtime."""
        if self.user_id:
            upload_root = _dp.user_uploads_dir(self.user_id)
        else:
            upload_root = Path(
                os.getenv("GRAPH_DATA_UPLOAD_DIR", "uploads/graph_data")
            ).resolve()

        targets = (
            (self._session_dir, self._session_dir.parent),
            (upload_root / self.session_id, upload_root),
        )
        for target, expected_parent in targets:
            resolved = target.resolve()
            if resolved.parent != expected_parent.resolve():
                raise ValueError(
                    f"refusing to delete storage outside session root: {resolved}"
                )
            if resolved.exists():
                shutil.rmtree(resolved)

    def _migrate_legacy_session_storage(self) -> None:
        if (self._session_dir / "graph_current.npz").exists():
            return
        for legacy_dir in self._legacy_session_dirs():
            if not (legacy_dir / "graph_current.npz").exists():
                continue
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._snapshots_dir.mkdir(parents=True, exist_ok=True)
            for name in (
                "graph_current.npz",
                "session_meta.json",
                "dataset_binding.json",
                "graph_ml_dataset_binding.json",
            ):
                src = legacy_dir / name
                if src.exists() and not (self._session_dir / name).exists():
                    shutil.copy2(src, self._session_dir / name)
            legacy_snapshots = legacy_dir / "snapshots"
            if legacy_snapshots.exists():
                for src in legacy_snapshots.glob("*.npz"):
                    dst = self._snapshots_dir / src.name
                    if not dst.exists():
                        shutil.copy2(src, dst)
            legacy_versions = legacy_dir / "graph_versions.json"
            new_versions = self._versions_path()
            if legacy_versions.exists() and not new_versions.exists():
                text = legacy_versions.read_text(encoding="utf-8")
                text = text.replace(str(legacy_dir), str(self._session_dir))
                new_versions.write_text(text, encoding="utf-8")
            logger.info(
                "migrated legacy graph session storage  session=%s  user_id=%s  from=%s  to=%s",
                self.session_id,
                self.user_id,
                legacy_dir,
                self._session_dir,
            )
            return

    def _upsert_version_record(
        self,
        *,
        description: str,
        snapshot_path: Optional[Path],
        graph: Optional[GraphData],
    ) -> None:
        records = [
            r for r in self._load_version_records()
            if int(r.get("graph_version", -1)) != self.graph_version
        ]
        record = {
            "graph_version": self.graph_version,
            "description": description or ("空图版本" if graph is None else "图版本"),
            "created_at": self._now_iso(),
            "loaded": graph is not None,
            "node_count": graph.num_nodes if graph is not None else 0,
            "edge_count": graph.num_edges if graph is not None else 0,
            "edge_entry_count": graph.num_edge_entries if graph is not None else 0,
            "unique_edge_count": graph.num_unique_edges if graph is not None else 0,
            "num_unique_directed_edges": graph.num_unique_directed_edges if graph is not None else 0,
            "num_unique_undirected_edges": graph.num_unique_undirected_edges if graph is not None else 0,
            "semantic_type": graph.semantic_type if graph is not None else "unknown",
            "storage_type": graph.storage_type if graph is not None else "mixed",
            "is_symmetric": graph.is_symmetric if graph is not None else False,
            "has_self_loops": graph.has_self_loops if graph is not None else False,
            "has_duplicate_entries": graph.has_duplicate_entries if graph is not None else False,
            "is_directed": graph.directed if graph is not None else True,
            "has_weights": graph.has_edge_weight if graph is not None else False,
            "snapshot": str(snapshot_path) if snapshot_path is not None else "",
        }
        records.append(record)
        self._write_version_records(records)

    def save_snapshot(self, description: str = "") -> Optional[str]:
        current_path = self._session_dir / "graph_current.npz"
        metadata_path = self._session_dir / "session_meta.json"
        if self._graph is None:
            if current_path.exists():
                current_path.unlink()
            self._upsert_version_record(
                description=description or "清空图",
                snapshot_path=None,
                graph=None,
            )
            self._write_session_meta(None)
            return None

        self._graph.save(str(current_path))
        snapshot_path = self._snapshots_dir / f"v{self.graph_version:03d}.npz"
        self._graph.save(str(snapshot_path))

        history = sorted(self._snapshots_dir.glob("v*.npz"))
        max_versions = int(os.getenv("GRAPH_VERSION_HISTORY_LIMIT", "50"))
        while max_versions > 0 and len(history) > max_versions:
            removable = history[1:] if len(history) > 1 else history
            if not removable:
                break
            oldest = removable.pop(0)
            history.remove(oldest)
            oldest.unlink(missing_ok=True)

        self._upsert_version_record(
            description=description or "图版本",
            snapshot_path=snapshot_path,
            graph=self._graph,
        )
        self._write_session_meta(self._graph)
        return str(snapshot_path)

    def load_snapshot(self) -> bool:
        self._migrate_legacy_session_storage()
        current_path = self._session_dir / "graph_current.npz"
        meta_path = self._session_dir / "session_meta.json"
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                for name, rec in (meta.get("uploads") or {}).items():
                    saved_path = self._resolve_existing_upload_path(rec.get("saved_path", "")) or rec.get("saved_path", "")
                    if saved_path:
                        self._uploads_by_name[name] = UploadFileRecord(
                            file_token=rec.get("file_token", ""),
                            original_filename=rec.get("original_filename", name),
                            saved_path=saved_path,
                        )
            except Exception:
                pass
        if self._versions_path().exists():
            manifest = json.loads(self._versions_path().read_text())
            if isinstance(manifest, dict) and manifest.get('format') == 2:
                version = int(manifest.get('current_graph_version', 0))
                record = next((r for r in manifest['versions'] if int(r['graph_version']) == version), None)
                if record and record.get('summary') and record.get('snapshot'):
                    path = Path(record['snapshot'])
                    if not path.is_file():
                        raise ValueError('COMMITTED_GRAPH_SNAPSHOT_MISSING')
                    self._graph, self._remote_graph, self._graph_version = GraphSnapshot(path, record['summary']), None, version
                    return True
        remote = meta.get("remote_graph") if isinstance(meta, dict) else None
        if isinstance(remote, dict) and remote.get("graph_key"):
            self._remote_graph = dict(remote)
            self._graph = None
            self._graph_version = int(
                remote.get("graph_version") or meta.get("graph_version") or 0
            )
            return True
        if not current_path.exists():
            return False
        self._remote_graph = None
        self._graph = GraphData.load(str(current_path))
        meta_version = 0
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            meta_version = int(meta.get("graph_version") or 0)
        except Exception:
            meta_version = 0
        if meta_version > 0:
            self._graph_version = meta_version
        else:
            self._bump_version()
        return True

    def undo(self) -> bool:
        records = [r for r in self.list_versions() if r.get("loaded")]
        previous = [r for r in records if int(r.get("graph_version", 0)) < self.graph_version]
        if not previous:
            return False
        target_rec = previous[-1]
        target = Path(str(target_rec.get("snapshot") or ""))
        if not target.exists():
            return False
        self._graph = GraphData.load(str(target))
        self._bump_version()
        self._graph.save(str(self._session_dir / "graph_current.npz"))
        self._upsert_version_record(
            description=f"回滚到版本 {target_rec.get('graph_version')}",
            snapshot_path=self._snapshots_dir / f"v{self.graph_version:03d}.npz",
            graph=self._graph,
        )
        self._graph.save(str(self._snapshots_dir / f"v{self.graph_version:03d}.npz"))
        self._write_session_meta(self._graph)
        return True

    def list_versions(self) -> list[dict]:
        existing = {
            int(path.stem[1:]): path
            for path in self._snapshots_dir.glob("v*.npz")
            if path.stem[1:].isdigit()
        }
        if self._versions_path().exists():
            manifest = json.loads(self._versions_path().read_text())
            if isinstance(manifest, dict) and manifest.get('format') == 2:
                existing = {}  # Only the committed manifest grants visibility.
        records_by_version: dict[int, dict] = {}
        for rec in self._load_version_records():
            try:
                version = int(rec.get("graph_version"))
            except Exception:
                continue
            path_text = str(rec.get("snapshot") or "")
            loaded = bool(rec.get("loaded", True)) and (not path_text or Path(path_text).exists())
            row = dict(rec)
            row.pop('commit_receipt', None)
            row.pop('summary', None)
            row["graph_version"] = version
            row["loaded"] = loaded
            row["current"] = version == self.graph_version
            records_by_version[version] = row

        for version, path in existing.items():
            if version in records_by_version:
                continue
            try:
                gd = GraphData.load(str(path))
                records_by_version[version] = {
                    "graph_version": version,
                    "description": f"历史图版本 {version}",
                    "created_at": "",
                    "loaded": True,
                    "node_count": gd.num_nodes,
                    "edge_count": gd.num_edges,
                    "edge_entry_count": gd.num_edge_entries,
                    "unique_edge_count": gd.num_unique_edges,
                    "num_unique_directed_edges": gd.num_unique_directed_edges,
                    "num_unique_undirected_edges": gd.num_unique_undirected_edges,
                    "semantic_type": gd.semantic_type,
                    "storage_type": gd.storage_type,
                    "is_symmetric": gd.is_symmetric,
                    "has_self_loops": gd.has_self_loops,
                    "has_duplicate_entries": gd.has_duplicate_entries,
                    "is_directed": gd.directed,
                    "has_weights": gd.has_edge_weight,
                    "snapshot": str(path),
                    "current": version == self.graph_version,
                }
            except Exception:
                pass

        return [
            records_by_version[v]
            for v in sorted(records_by_version)
        ]

    def load_version(self, graph_version: int) -> Optional[dict]:
        version = int(graph_version)
        match = None
        for rec in self.list_versions():
            if int(rec.get("graph_version", -1)) == version:
                match = rec
                break
        if match is None:
            return None
        path = Path(str(match.get("snapshot") or ""))
        if not match.get("loaded") or not path.exists():
            return None
        self._graph = GraphData.load(str(path))
        self._graph_version = version
        self._graph.save(str(self._session_dir / "graph_current.npz"))
        self._write_session_meta(self._graph)
        records = self._load_version_records()
        self._write_version_records(records)
        return {
            **match,
            "current": True,
            "num_nodes": self._graph.num_nodes,
            "num_edges": self._graph.num_edges,
            "num_edge_entries": self._graph.num_edge_entries,
            "num_unique_edges": self._graph.num_unique_edges,
            "num_unique_directed_edges": self._graph.num_unique_directed_edges,
            "num_unique_undirected_edges": self._graph.num_unique_undirected_edges,
            "semantic_type": self._graph.semantic_type,
            "storage_type": self._graph.storage_type,
            "is_symmetric": self._graph.is_symmetric,
            "has_self_loops": self._graph.has_self_loops,
            "has_duplicate_entries": self._graph.has_duplicate_entries,
        }

    def discard_version(
        self,
        graph_version: int,
        fallback_graph_version: Optional[int] = None,
    ) -> Optional[dict]:
        version = int(graph_version)
        versions_before = self.list_versions()
        target = next(
            (rec for rec in versions_before if int(rec.get("graph_version", -1)) == version),
            None,
        )
        if target is None:
            return None

        fallback = None
        if version == self.graph_version:
            if fallback_graph_version is not None:
                fallback_num = int(fallback_graph_version)
                fallback = next(
                    (
                        rec for rec in versions_before
                        if int(rec.get("graph_version", -1)) == fallback_num
                        and int(rec.get("graph_version", -1)) != version
                        and rec.get("loaded")
                    ),
                    None,
                )
            if fallback is None:
                candidates = [
                    rec for rec in versions_before
                    if int(rec.get("graph_version", -1)) < version and rec.get("loaded")
                ]
                fallback = candidates[-1] if candidates else None
            if fallback is None:
                return None
            fallback_path = Path(str(fallback.get("snapshot") or ""))
            if not fallback_path.exists():
                return None
            self._graph = GraphData.load(str(fallback_path))
            self._graph_version = int(fallback.get("graph_version"))
            self._graph.save(str(self._session_dir / "graph_current.npz"))
            self._write_session_meta(self._graph)

        snapshot_text = str(target.get("snapshot") or "")
        if snapshot_text:
            Path(snapshot_text).unlink(missing_ok=True)
        fallback_version = self.graph_version
        records = [
            rec for rec in self._load_version_records()
            if int(rec.get("graph_version", -1)) != version
        ]
        self._write_version_records(records)
        return {
            "discarded_graph_version": version,
            "graph_version": fallback_version,
            "num_nodes": self._graph.num_nodes if self._graph is not None else 0,
            "num_edges": self._graph.num_edges if self._graph is not None else 0,
            "num_edge_entries": self._graph.num_edge_entries if self._graph is not None else 0,
            "num_unique_edges": self._graph.num_unique_edges if self._graph is not None else 0,
            "num_unique_directed_edges": self._graph.num_unique_directed_edges if self._graph is not None else 0,
            "num_unique_undirected_edges": self._graph.num_unique_undirected_edges if self._graph is not None else 0,
            "semantic_type": self._graph.semantic_type if self._graph is not None else "unknown",
            "storage_type": self._graph.storage_type if self._graph is not None else "mixed",
            "is_symmetric": self._graph.is_symmetric if self._graph is not None else False,
            "has_self_loops": self._graph.has_self_loops if self._graph is not None else False,
            "has_duplicate_entries": self._graph.has_duplicate_entries if self._graph is not None else False,
        }

    def bind_dataset(self, dataset_id: str, version: str) -> None:
        payload = {"dataset_id": dataset_id, "dataset_version": version}
        (self._session_dir / "dataset_binding.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_dataset_binding(self) -> dict:
        path = self._session_dir / "dataset_binding.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def bind_graph_ml_dataset(
        self,
        *,
        dataset_id: str,
        source_filename: str,
        fingerprint: str,
        dataset_profile: str,
    ) -> None:
        """Persist the independent graph-classification dataset binding."""
        values = {
            "dataset_id": str(dataset_id or "").strip(),
            "source_filename": str(source_filename or "").strip(),
            "fingerprint": str(fingerprint or "").strip(),
            "dataset_profile": str(dataset_profile or "").strip().lower(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("图分类数据集绑定缺少字段: " + ", ".join(missing))
        if values["dataset_profile"] not in {"hiv", "pcba"}:
            raise ValueError("图分类数据集绑定 profile 必须是 hiv 或 pcba")

        path = self._session_dir / "graph_ml_dataset_binding.json"
        temp_path = self._session_dir / ".graph_ml_dataset_binding.json.tmp"
        temp_path.write_text(
            json.dumps(values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def load_graph_ml_dataset_binding(self) -> dict:
        path = self._session_dir / "graph_ml_dataset_binding.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        required = {
            "dataset_id",
            "source_filename",
            "fingerprint",
            "dataset_profile",
        }
        if not required.issubset(payload):
            return {}
        if not all(
            isinstance(payload[key], str) and payload[key].strip()
            for key in required
        ):
            return {}
        if payload["dataset_profile"] not in {"hiv", "pcba"}:
            return {}
        return {key: payload[key] for key in sorted(required)}

    @classmethod
    def discover_restorable_sessions(cls) -> list[str]:
        root = Path(cls._graph_storage_root())
        if not root.exists():
            return []
        rows: list[str] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if (child / "graph_current.npz").exists() or (child / 'graph_versions.json').exists():
                rows.append(child.name)
                continue
            meta_path = child / "session_meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(meta.get("remote_graph"), dict):
                        rows.append(child.name)
                except Exception:
                    pass
        return sorted(rows)

    def _write_session_meta(self, graph: Optional[GraphData]) -> None:
        path = self._session_dir / "session_meta.json"
        remote = self._remote_graph or {}
        payload = {
            "session_id": self.session_id,
            "graph_version": self.graph_version,
            "node_count": (
                graph.num_nodes if graph is not None
                else int(remote.get("node_count", 0))
            ),
            "edge_count": (
                graph.num_edges if graph is not None
                else int(remote.get("edge_count", 0))
            ),
            "edge_entry_count": (
                graph.num_edge_entries if graph is not None
                else int(remote.get("edge_entry_count", remote.get("edge_count", 0)))
            ),
            "unique_edge_count": (
                graph.num_unique_edges if graph is not None
                else int(remote.get("unique_edge_count", remote.get("edge_count", 0)))
            ),
            "num_unique_directed_edges": (
                graph.num_unique_directed_edges if graph is not None
                else int(remote.get("num_unique_directed_edges", 0))
            ),
            "num_unique_undirected_edges": (
                graph.num_unique_undirected_edges if graph is not None
                else int(remote.get("num_unique_undirected_edges", 0))
            ),
            "semantic_type": (
                graph.semantic_type if graph is not None
                else str(remote.get("semantic_type") or "unknown")
            ),
            "storage_type": (
                graph.storage_type if graph is not None
                else str(remote.get("storage_type") or "mixed")
            ),
            "is_symmetric": (
                graph.is_symmetric if graph is not None
                else bool(remote.get("is_symmetric", False))
            ),
            "has_self_loops": (
                graph.has_self_loops if graph is not None
                else bool(remote.get("has_self_loops", False))
            ),
            "has_duplicate_entries": (
                graph.has_duplicate_entries if graph is not None
                else bool(remote.get("has_duplicate_entries", False))
            ),
            "is_directed": (
                graph.directed if graph is not None
                else bool(remote.get("directed", True))
            ),
            "has_weights": (
                graph.has_edge_weight if graph is not None
                else bool(remote.get("has_weights", False))
            ),
            "storage_backend": "nebula" if self._remote_graph is not None else "local",
            "remote_graph": self._remote_graph,
            "uploads": {
                name: {
                    "file_token": rec.file_token,
                    "original_filename": rec.original_filename,
                    "saved_path": rec.saved_path,
                }
                for name, rec in self._uploads_by_name.items()
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
