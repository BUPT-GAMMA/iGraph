"""Session-scoped in-memory lifecycle for graph-classification datasets."""

from __future__ import annotations

import os
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable

from graph_data_service.session import GraphSession

from .graph_classification import (
    GraphClassificationDatasetBundle,
    ProgressCallback,
    load_graph_classification_dataset,
)


DatasetLoader = Callable[..., GraphClassificationDatasetBundle]


@dataclass
class _InflightLoad:
    key: tuple[str, str]
    generation: int
    future: Future[GraphClassificationDatasetBundle]


class GraphMLDatasetStore:
    """Keep at most one validated multi-graph bundle per runtime session.

    The store deliberately does not rebuild bundles from the persisted binding.
    A process restart therefore requires an explicit load call, which avoids
    unexpected deserialization during train/infer.
    """

    def __init__(
        self,
        *,
        loader: DatasetLoader = load_graph_classification_dataset,
        max_resident_datasets: int | None = None,
    ) -> None:
        configured_limit = (
            max_resident_datasets
            if max_resident_datasets is not None
            else _positive_env_int("GRAPH_ML_MAX_RESIDENT_DATASETS", 2)
        )
        if isinstance(configured_limit, bool) or int(configured_limit) < 1:
            raise ValueError("max_resident_datasets 必须是正整数")
        self._loader = loader
        self._max_resident_datasets = int(configured_limit)
        self._bundles: dict[str, GraphClassificationDatasetBundle] = {}
        self._inflight: dict[str, _InflightLoad] = {}
        self._generations: dict[str, int] = {}
        self._lock = RLock()

    @property
    def resident_count(self) -> int:
        with self._lock:
            return len(self._bundles)

    def current_dataset_id(self, session_id: str) -> str | None:
        session_key = _require_session_id(session_id)
        with self._lock:
            bundle = self._bundles.get(session_key)
            return bundle.dataset_id if bundle is not None else None

    def load(
        self,
        session_id: str,
        session: GraphSession,
        filename: str,
        dataset_profile: str = "auto",
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> GraphClassificationDatasetBundle:
        """Load and atomically bind a bundle, coalescing concurrent requests."""
        session_key = _require_session_id(session_id)
        _require_matching_session(session_key, session)
        clean_filename = str(filename or "").strip()
        profile = str(dataset_profile or "auto").strip().lower()
        request_key = (clean_filename, profile)

        while True:
            wait_for: Future[GraphClassificationDatasetBundle] | None = None
            owner: _InflightLoad | None = None
            with self._lock:
                current = self._bundles.get(session_key)
                if current is not None and self._matches_registered_upload(
                    current,
                    session,
                    clean_filename,
                    profile,
                ):
                    self._persist_binding(session, current)
                    return current

                inflight = self._inflight.get(session_key)
                if inflight is not None:
                    wait_for = inflight.future
                    same_request = inflight.key == request_key
                else:
                    if (
                        session_key not in self._bundles
                        and self._occupied_slots_locked()
                        >= self._max_resident_datasets
                    ):
                        raise ValueError(
                            "图分类数据集常驻数量已达到上限 "
                            f"({self._max_resident_datasets})，请先释放其他会话后重试"
                        )
                    generation = self._generations.get(session_key, 0)
                    owner = _InflightLoad(
                        key=request_key,
                        generation=generation,
                        future=Future(),
                    )
                    self._inflight[session_key] = owner
                    same_request = False

            if owner is not None:
                return self._load_as_owner(
                    session_key,
                    session,
                    clean_filename,
                    profile,
                    owner,
                    progress_callback,
                )

            assert wait_for is not None
            try:
                completed = wait_for.result()
            except Exception:
                if same_request:
                    raise
                continue
            if same_request:
                return completed

    def bind(
        self,
        session_id: str,
        dataset_id: str,
    ) -> GraphClassificationDatasetBundle:
        """Bind only the bundle already resident in this runtime session."""
        return self.resolve_for_session(session_id, dataset_id)

    def resolve_for_session(
        self,
        session_id: str,
        dataset_id: str | None = None,
    ) -> GraphClassificationDatasetBundle:
        """Resolve only the bundle currently owned by the specified session."""
        session_key = _require_session_id(session_id)
        requested_id = str(dataset_id or "").strip()
        with self._lock:
            bundle = self._bundles.get(session_key)
            if bundle is None:
                raise ValueError(
                    "当前会话内存中不存在图分类数据集，请重新调用 "
                    "load_graph_classification_dataset"
                )
            if requested_id and requested_id != bundle.dataset_id:
                raise ValueError(
                    f"dataset_id 未绑定到当前会话: {requested_id}"
                )
            return bundle

    def release_session(self, session_id: str) -> bool:
        """Release only memory references; uploaded files and bindings remain."""
        session_key = _require_session_id(session_id)
        with self._lock:
            self._generations[session_key] = self._generations.get(session_key, 0) + 1
            return self._bundles.pop(session_key, None) is not None

    def _load_as_owner(
        self,
        session_key: str,
        session: GraphSession,
        filename: str,
        profile: str,
        inflight: _InflightLoad,
        progress_callback: ProgressCallback | None,
    ) -> GraphClassificationDatasetBundle:
        try:
            bundle = self._loader(
                session,
                filename,
                profile,
                progress_callback=progress_callback,
            )
            with self._lock:
                if self._generations.get(session_key, 0) != inflight.generation:
                    raise ValueError("会话已释放，图分类数据集加载结果未绑定")
                self._persist_binding(session, bundle)
                self._bundles[session_key] = bundle
                if self._inflight.get(session_key) is inflight:
                    self._inflight.pop(session_key, None)
                inflight.future.set_result(bundle)
            return bundle
        except BaseException as exc:
            with self._lock:
                if self._inflight.get(session_key) is inflight:
                    self._inflight.pop(session_key, None)
                if not inflight.future.done():
                    inflight.future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(session_key) is inflight:
                    self._inflight.pop(session_key, None)

    def _occupied_slots_locked(self) -> int:
        new_session_loads = sum(
            1 for session_key in self._inflight if session_key not in self._bundles
        )
        return len(self._bundles) + new_session_loads

    @staticmethod
    def _matches_registered_upload(
        bundle: GraphClassificationDatasetBundle,
        session: GraphSession,
        filename: str,
        profile: str,
    ) -> bool:
        record = session.resolve_upload_by_name(filename)
        if record is None:
            return False
        path = Path(str(record.saved_path or "")).resolve(strict=False)
        try:
            stat = path.stat()
        except OSError:
            return False
        source_profile_matches = profile == "auto" or profile == bundle.name
        return (
            source_profile_matches
            and str(record.original_filename or "").strip() == bundle.source_filename
            and str(path) == str(Path(bundle.source_path).resolve(strict=False))
            and int(bundle.metadata.get("source_size_bytes", -1)) == stat.st_size
            and int(bundle.metadata.get("source_mtime_ns", -1)) == stat.st_mtime_ns
        )

    @staticmethod
    def _persist_binding(
        session: GraphSession,
        bundle: GraphClassificationDatasetBundle,
    ) -> None:
        session.bind_graph_ml_dataset(
            dataset_id=bundle.dataset_id,
            source_filename=bundle.source_filename,
            fingerprint=bundle.fingerprint,
            dataset_profile=bundle.name,
        )


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _require_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not value:
        raise ValueError("session_id 不能为空")
    return value


def _require_matching_session(session_key: str, session: GraphSession) -> None:
    expected_key = (
        f"{session.user_id}:{session.session_id}"
        if session.user_id
        else session.session_id
    )
    if session_key != expected_key:
        raise ValueError("图分类数据集会话身份不匹配，已拒绝加载")
