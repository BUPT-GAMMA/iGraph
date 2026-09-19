"""SQLite-backed graph catalog, durable jobs, snapshots and artifacts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .integrity import validate_artifact_manifest, validate_snapshot_manifest


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equality coercion."""
    try:
        return _json_dump(left) == _json_dump(right)
    except (TypeError, ValueError, OverflowError):
        return False


class ScopeDeletionInProgress(RuntimeError):
    """A session scope is fenced while its workers and files are removed."""


class SnapshotUnavailable(RuntimeError):
    """A compute enqueue lost its immutable snapshot before commit."""


def _degree_result_parameters(
    algorithm: str, requested: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the effective metadata emitted by the degree fast path."""
    if algorithm not in {"degree", "in_degree", "out_degree", "degree_centrality"}:
        return None
    raw_top_k = requested.get("top_k", 20)
    try:
        top_k = 200 if str(raw_top_k).strip().lower() == "all" else int(raw_top_k)
    except (TypeError, ValueError):
        return None
    return {**requested, "top_k": max(1, min(top_k, 200))}


def validate_artifact_job_binding(
    job: dict[str, Any], artifact: dict[str, Any]
) -> None:
    """Bind an artifact result to the exact compute request that produced it.

    Manifest validation proves that catalog fields describe the files on disk.
    This independent check proves that those fields also describe the claimed
    durable job, including its immutable snapshot and requested parameters.
    It is used before publication and again when serving historical rows.
    """
    if not isinstance(job, dict) or not isinstance(artifact, dict):
        raise ValueError("Artifact 与任务绑定记录类型错误")
    if job.get("kind") != "compute":
        raise ValueError("Artifact 只能由 compute 任务发布")
    request = job.get("request")
    if not isinstance(request, dict):
        raise ValueError("Artifact 对应任务请求无效")
    snapshot = request.get("snapshot")
    metadata = artifact.get("metadata")
    if not isinstance(snapshot, dict) or not isinstance(metadata, dict):
        raise ValueError("Artifact 缺少快照或计算元数据")

    algorithm = request.get("algorithm")
    if (
        not isinstance(algorithm, str)
        or not algorithm
        or artifact.get("algorithm") != algorithm
        or metadata.get("algorithm") != algorithm
    ):
        raise ValueError("Artifact 算法与任务请求不一致")

    try:
        job_version = int(job.get("graph_version"))
        snapshot_version = int(snapshot.get("graph_version"))
        metadata_version = int(metadata.get("graph_version"))
        snapshot_partitions = int(snapshot.get("partitions"))
        metadata_partitions = int(metadata.get("partitions"))
        requested_workers = int(request.get("workers"))
        actual_workers = int(metadata.get("workers"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Artifact 图版本、分区或 Worker 元数据无效") from exc
    if any(
        isinstance(value, bool)
        for value in (
            job.get("graph_version"),
            snapshot.get("graph_version"),
            metadata.get("graph_version"),
            snapshot.get("partitions"),
            metadata.get("partitions"),
            request.get("workers"),
            metadata.get("workers"),
        )
    ):
        raise ValueError("Artifact 图版本、分区或 Worker 元数据无效")

    graph_id = job.get("graph_id")
    snapshot_binding = request.get("snapshot_binding")
    shared_server_snapshot = bool(
        isinstance(snapshot_binding, dict)
        and snapshot_binding.get("kind") == "server_dataset_snapshot"
        and snapshot_binding.get("source_snapshot_id")
        == snapshot.get("snapshot_id")
        and snapshot_binding.get("source_user_id") == snapshot.get("user_id")
        and snapshot_binding.get("source_session_id")
        == snapshot.get("session_id")
        and snapshot_binding.get("source_graph_id") == snapshot.get("graph_id")
        and int(snapshot_binding.get("source_graph_version") or 0)
        == snapshot_version
    )
    snapshot_matches_target = bool(
        snapshot.get("graph_id") == graph_id and snapshot_version == job_version
    )
    if (
        artifact.get("graph_id", graph_id) != graph_id
        or not (snapshot_matches_target or shared_server_snapshot)
        or metadata.get("graph_id") != graph_id
        or artifact.get("graph_version", job_version) != job_version
        or metadata_version != job_version
        or metadata.get("snapshot_id") != snapshot.get("snapshot_id")
        or metadata_partitions != snapshot_partitions
        or requested_workers < 1
        or actual_workers < 1
        or actual_workers > requested_workers
        or actual_workers > snapshot_partitions
    ):
        raise ValueError("Artifact 与任务图版本、快照或执行规模不一致")

    expected_policy_id = snapshot.get("policy_id")
    if not expected_policy_id and isinstance(snapshot.get("quality"), dict):
        expected_policy_id = snapshot["quality"].get("policy_id")
    if (
        expected_policy_id
        and metadata.get("data_quality_policy_id") != expected_policy_id
    ):
        raise ValueError("Artifact 数据质量策略与任务快照不一致")

    requested_parameters = request.get("parameters")
    result_parameters = metadata.get("parameters")
    if not isinstance(requested_parameters, dict) or not isinstance(
        result_parameters, dict
    ):
        raise ValueError("Artifact 参数元数据无效")
    allowed_parameters = [requested_parameters]
    effective_degree = _degree_result_parameters(algorithm, requested_parameters)
    if effective_degree is not None:
        allowed_parameters.append(effective_degree)
    if not any(
        _same_json_value(result_parameters, expected)
        for expected in allowed_parameters
    ):
        raise ValueError("Artifact 参数与任务请求不一致")

    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("Artifact ID 无效")
    if artifact.get("job_id", job.get("job_id")) != job.get("job_id"):
        raise ValueError("Artifact 与任务 ID 不一致")
    for field in ("user_id", "session_id"):
        if field in artifact and artifact.get(field) != job.get(field):
            raise ValueError(f"Artifact 与任务 {field} 不一致")
    artifact_path = artifact.get("path")
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError("Artifact 清单路径无效")
    base_scope = request.get("user_scope")
    lease_id = job.get("lease_id")
    if (
        not isinstance(base_scope, str)
        or not base_scope
        or not isinstance(lease_id, str)
        or not lease_id
    ):
        raise ValueError("Artifact 对应任务缺少文件系统租约身份")
    expected_path_suffix = (
        f"{base_scope}__{lease_id}",
        str(graph_id),
        f"v{job_version:08d}",
        artifact_id,
        "manifest.json",
    )
    if Path(artifact_path).parts[-5:] != expected_path_suffix:
        raise ValueError("Artifact 清单路径与任务租约或图版本不一致")
    summary = artifact.get("summary")
    preview = artifact.get("preview")
    # ``summary.algorithm`` describes the selected kernel/sub-mode for some
    # algorithm families (for example shortest_path -> yen_k_shortest), so the
    # authoritative tool-name binding is metadata.algorithm above.
    if not isinstance(summary, dict) or summary.get("success") is not True:
        raise ValueError("Artifact 摘要无效或计算未成功")
    if not isinstance(preview, list) or len(preview) > 20:
        raise ValueError("Artifact 预览不是有界列表")

    # Once published, both denormalized control-plane copies must remain
    # identical. During publication the running job still has empty fields.
    if job.get("status") == "done":
        if job.get("artifact_id") != artifact_id:
            raise ValueError("Artifact 与已完成任务引用不一致")
        if not _same_json_value(job.get("summary"), summary) or not _same_json_value(
            job.get("preview"), preview
        ):
            raise ValueError("Artifact 与已完成任务摘要或预览不一致")


class LargeGraphCatalog:
    """Single source of truth for version-bound large-graph state.

    SQLite is used instead of process memory so status survives API restarts.
    Each method opens a short-lived connection; this is safe across the API,
    worker thread and a replacement process after a crash.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._init_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_versions (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    snapshot_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id, graph_id, graph_version)
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    node_count INTEGER NOT NULL,
                    edge_count INTEGER NOT NULL,
                    partitions INTEGER NOT NULL,
                    directed INTEGER NOT NULL,
                    has_weights INTEGER NOT NULL,
                    format TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_id, session_id, graph_id, graph_version)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL,
                    message TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    preview_json TEXT NOT NULL DEFAULT '[]',
                    artifact_id TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_pid INTEGER,
                    worker_identity_json TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    lease_id TEXT,
                    heartbeat_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_large_jobs_queue
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_large_jobs_scope
                    ON jobs(user_id, session_id, created_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    graph_id TEXT NOT NULL,
                    graph_version INTEGER NOT NULL,
                    algorithm TEXT NOT NULL,
                    path TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );

                CREATE TABLE IF NOT EXISTS attempt_cleanup_queue (
                    job_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, lease_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS deleting_scopes (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, session_id)
                );
                """
            )
            # Forward-compatible migration for catalogs created by the first
            # development version of this feature.
            job_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "heartbeat_at" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT")
            if "attempt" not in job_columns:
                conn.execute(
                    "ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
                )
            if "lease_id" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN lease_id TEXT")
            if "worker_identity_json" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN worker_identity_json TEXT")

    @staticmethod
    def _job_id() -> str:
        return f"lg_{uuid.uuid4().hex[:16]}"

    def reserve_preparation(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        source: dict[str, Any],
        policy_id: str,
        policy: dict[str, Any],
        request: dict[str, Any],
        graph_version: int | None = None,
    ) -> tuple[str, int]:
        """Atomically reserve a version and enqueue its preparation job."""
        now = _now_iso()
        job_id = self._job_id()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleting = conn.execute(
                "SELECT 1 FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
            if deleting is not None:
                raise ScopeDeletionInProgress("会话正在删除，不能提交新的大图任务")
            if graph_version is None:
                row = conn.execute(
                    """SELECT COALESCE(MAX(graph_version), 0) AS current
                       FROM graph_versions
                       WHERE user_id=? AND session_id=? AND graph_id=?""",
                    (user_id, session_id, graph_id),
                ).fetchone()
                graph_version = int(row["current"]) + 1
            else:
                graph_version = int(graph_version)
                if graph_version <= 0:
                    raise ValueError("graph_version 必须为正整数")
            conn.execute(
                """INSERT INTO graph_versions(
                       user_id,session_id,graph_id,graph_version,status,source_json,
                       policy_id,policy_json,quality_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id, session_id, graph_id, graph_version, "preparing",
                    _json_dump(source), policy_id, _json_dump(policy), "{}", now, now,
                ),
            )
            job_request = dict(request)
            job_request["graph_version"] = graph_version
            conn.execute(
                """INSERT INTO jobs(
                       job_id,user_id,session_id,graph_id,graph_version,kind,status,
                       stage,progress,message,request_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'prepare','queued','queued',0,?,?,?,?)""",
                (
                    job_id, user_id, session_id, graph_id, graph_version,
                    "等待本地大图 Worker", _json_dump(job_request), now, now,
                ),
            )
        return job_id, graph_version

    def get_prepare_job_for_version(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
    ) -> dict[str, Any] | None:
        """Return the durable prepare job bound to one logical version."""

        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE user_id=? AND session_id=? AND graph_id=?
                     AND graph_version=? AND kind='prepare'
                   ORDER BY created_at DESC,job_id DESC LIMIT 1""",
                (user_id, session_id, graph_id, int(graph_version)),
            ).fetchone()
        return self._decode_job(row) if row is not None else None

    def attach_published_server_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
        source_user_id: str,
        source_session_id: str,
        source_graph_id: str,
        source_graph_version: int,
        server_dataset_id: str,
        revision: str,
    ) -> dict[str, Any]:
        """Create a session-local logical version backed by an immutable cache.

        Only the graph-version row is duplicated.  The large snapshot shards
        remain owned by the server-dataset scope and are therefore neither
        copied during activation nor deleted with a consuming session.
        """

        now = _now_iso()
        target_version = int(graph_version)
        if target_version <= 0:
            raise ValueError("graph_version 必须为正整数")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleting = conn.execute(
                "SELECT 1 FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
            if deleting is not None:
                raise ScopeDeletionInProgress("会话正在删除，不能绑定服务器快照")
            source = conn.execute(
                """SELECT s.*,gv.source_json AS origin_source_json,
                          gv.policy_id AS origin_policy_id,
                          gv.policy_json AS origin_policy_json,
                          gv.quality_json AS origin_quality_json
                   FROM graph_versions gv
                   JOIN snapshots s ON s.snapshot_id=gv.snapshot_id
                   WHERE gv.user_id=? AND gv.session_id=? AND gv.graph_id=?
                     AND gv.graph_version=? AND gv.status='published'
                     AND s.state='published'""",
                (
                    source_user_id,
                    source_session_id,
                    source_graph_id,
                    int(source_graph_version),
                ),
            ).fetchone()
            if source is None:
                raise SnapshotUnavailable("服务器数据源预处理快照不存在")
            origin = _json_load(source["origin_source_json"], {})
            if (
                origin.get("kind") != "server_dataset"
                or origin.get("server_dataset_id") != server_dataset_id
                or origin.get("revision") != revision
            ):
                raise SnapshotUnavailable("服务器数据源预处理快照版本不匹配")

            existing = conn.execute(
                """SELECT * FROM graph_versions
                   WHERE user_id=? AND session_id=? AND graph_id=?
                     AND graph_version=?""",
                (user_id, session_id, graph_id, target_version),
            ).fetchone()
            alias_source = {
                "kind": "server_dataset_snapshot",
                "server_dataset_id": server_dataset_id,
                "revision": revision,
                "source_snapshot_id": source["snapshot_id"],
                "source_user_id": source_user_id,
                "source_session_id": source_session_id,
                "source_graph_id": source_graph_id,
                "source_graph_version": int(source_graph_version),
            }
            if existing is not None:
                existing_source = _json_load(existing["source_json"], {})
                if (
                    existing["status"] == "published"
                    and existing["snapshot_id"] == source["snapshot_id"]
                    and existing_source == alias_source
                ):
                    result = dict(source)
                    result["target_graph_version"] = target_version
                    result["created"] = False
                    return result
                raise ValueError("该 graph_version 已绑定其他图版本")

            conn.execute(
                """INSERT INTO graph_versions(
                       user_id,session_id,graph_id,graph_version,status,
                       source_json,policy_id,policy_json,quality_json,snapshot_id,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    session_id,
                    graph_id,
                    target_version,
                    "published",
                    _json_dump(alias_source),
                    source["origin_policy_id"],
                    source["origin_policy_json"],
                    source["origin_quality_json"],
                    source["snapshot_id"],
                    now,
                    now,
                ),
            )
            result = dict(source)
            result["target_graph_version"] = target_version
            result["created"] = True
            return result

    def enqueue_compute(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
        request: dict[str, Any],
        minimum_derived_version: int | None = None,
    ) -> tuple[str, int | None]:
        now = _now_iso()
        job_id = self._job_id()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleting = conn.execute(
                "SELECT 1 FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
            if deleting is not None:
                raise ScopeDeletionInProgress("会话正在删除，不能提交新的大图任务")
            # ``submit_compute`` validates the manifest before reaching the
            # catalog.  A different API replica may delete the session in that
            # interval, so bind the job to the still-published snapshot again
            # under the same write transaction as the INSERT.
            expected_snapshot = request.get("snapshot")
            expected_snapshot = (
                expected_snapshot if isinstance(expected_snapshot, dict) else {}
            )
            published = conn.execute(
                """SELECT s.snapshot_id,s.manifest_path,gv.source_json,
                          gv.policy_id,gv.policy_json
                   FROM graph_versions gv
                   JOIN snapshots s ON s.snapshot_id=gv.snapshot_id
                   WHERE gv.user_id=? AND gv.session_id=? AND gv.graph_id=?
                     AND gv.graph_version=? AND gv.status='published'
                     AND s.state='published'""",
                (user_id, session_id, graph_id, int(graph_version)),
            ).fetchone()
            if (
                published is None
                or str(expected_snapshot.get("snapshot_id") or "")
                != str(published["snapshot_id"])
                or str(expected_snapshot.get("manifest_path") or "")
                != str(published["manifest_path"])
            ):
                raise SnapshotUnavailable(
                    "图版本的已发布分区快照已不可用，请重新查询版本后提交"
                )
            job_request = dict(request)
            parameters = job_request.get("parameters")
            create_derived = bool(
                job_request.get("algorithm") == "subgraph_extraction"
                and isinstance(parameters, dict)
                and parameters.get("create_version", True)
            )
            derived_version: int | None = None
            if create_derived:
                current = conn.execute(
                    """SELECT COALESCE(MAX(graph_version),0) AS current
                       FROM graph_versions
                       WHERE user_id=? AND session_id=? AND graph_id=?""",
                    (user_id, session_id, graph_id),
                ).fetchone()
                requested_floor = (
                    int(minimum_derived_version)
                    if minimum_derived_version is not None
                    else 1
                )
                if requested_floor <= 0:
                    raise ValueError("minimum_derived_version 必须为正整数")
                derived_version = max(int(current["current"]) + 1, requested_floor)
                derived_source = {
                    "kind": "derived_subgraph",
                    "source_graph_id": graph_id,
                    "source_graph_version": int(graph_version),
                    "source_snapshot_id": str(published["snapshot_id"]),
                    "compute_job_id": job_id,
                    "method": str(parameters.get("method") or ""),
                }
                conn.execute(
                    """INSERT INTO graph_versions(
                           user_id,session_id,graph_id,graph_version,status,
                           source_json,policy_id,policy_json,quality_json,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user_id, session_id, graph_id, derived_version,
                        "preparing", _json_dump(derived_source),
                        str(published["policy_id"]), str(published["policy_json"]),
                        "{}", now, now,
                    ),
                )
                job_request["derived_target"] = {
                    "graph_version": derived_version,
                    "source_graph_version": int(graph_version),
                }
            conn.execute(
                """INSERT INTO jobs(
                       job_id,user_id,session_id,graph_id,graph_version,kind,status,
                       stage,progress,message,request_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'compute','queued','queued',0,?,?,?,?)""",
                (
                    job_id, user_id, session_id, graph_id, int(graph_version),
                    "等待本地大图 Worker", _json_dump(job_request), now, now,
                ),
            )
        return job_id, derived_version

    def recover_interrupted_jobs(
        self,
        stale_after_seconds: float = 30.0,
        *,
        recoverable_leases: set[tuple[str, str]] | None = None,
    ) -> int:
        """Resolve interrupted work without reviving a requested cancellation.

        A queued cancellation has no owner process and can be completed
        immediately.  A running/cancelling job, however, remains owned until
        its heartbeat lease is stale.  This distinction prevents an idle API
        or Worker replica from declaring another live process cancelled while
        that process is still writing its attempt-private files.

        Only an ordinary stale ``running`` job with no cancellation marker is
        eligible for retry.
        """
        now = _now_iso()
        cutoff = (
            datetime.now(timezone.utc).astimezone()
            - timedelta(seconds=max(0.0, float(stale_after_seconds)))
        ).isoformat(timespec="seconds")
        owner_stale_clause = (
            "1=1"
            if stale_after_seconds <= 0
            else "(heartbeat_at IS NULL OR heartbeat_at<?)"
        )
        # A row with a recorded OS worker is never recovered merely because
        # its heartbeat is old. The runtime must first prove that exact lease's
        # process is gone (or safely terminate its verified process identity)
        # and explicitly authorize the job/lease pair below.
        verified = sorted(recoverable_leases or set())
        verified_clause = "worker_pid IS NULL"
        verified_params: list[Any] = []
        if verified:
            verified_clause += " OR " + " OR ".join(
                "(job_id=? AND lease_id=?)" for _ in verified
            )
            for verified_job_id, verified_lease_id in verified:
                verified_params.extend((verified_job_id, verified_lease_id))
        owner_filter = f"({owner_stale_clause}) AND ({verified_clause})"
        owner_filter_params: list[Any] = []
        if stale_after_seconds > 0:
            owner_filter_params.append(cutoff)
        owner_filter_params.extend(verified_params)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Preserve the physical identity before requeue/cancellation can
            # replace or retire the lease. Cleanup is a durable, idempotent
            # follow-up operation so another Worker can finish it after an
            # additional process crash.
            conn.execute(
                f"""INSERT OR IGNORE INTO attempt_cleanup_queue(
                        job_id,lease_id,created_at
                    )
                    SELECT job_id,lease_id,? FROM jobs
                    WHERE status IN ('running','cancelling')
                      AND lease_id IS NOT NULL
                      AND {owner_filter}""",
                (now, *owner_filter_params),
            )
            # Legacy/interrupted callers may have persisted the cancellation
            # marker before changing queued -> cancelled.  There is no owner
            # for a queued row, so this transition is always safe.
            queued_cancelled = conn.execute(
                """UPDATE jobs
                   SET status='cancelled',stage='cancelled',
                       message='恢复时完成未决取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=COALESCE(finished_at,?),updated_at=?
                   WHERE status='queued' AND cancel_requested=1""",
                (now, now),
            )
            owner_cancelled = conn.execute(
                f"""UPDATE jobs
                    SET status='cancelled',stage='cancelled',
                        message='Worker 租约过期后完成未决取消请求',
                        cancel_requested=1,worker_pid=NULL,
                        worker_identity_json=NULL,heartbeat_at=NULL,
                        finished_at=COALESCE(finished_at,?),updated_at=?
                    WHERE status IN ('running','cancelling')
                      AND (status='cancelling' OR cancel_requested=1)
                      AND {owner_filter}""",
                (now, now, *owner_filter_params),
            )
            self._sync_cancelled_prepare_versions(conn, now)
            requeued = conn.execute(
                f"""UPDATE jobs
                   SET status='queued', stage='recovered', progress=0,
                       message='Worker 租约过期后重新排队', worker_pid=NULL,
                       worker_identity_json=NULL,lease_id=NULL,
                       heartbeat_at=NULL,updated_at=?
                   WHERE status='running' AND cancel_requested=0
                     AND {owner_filter}""",
                (now, *owner_filter_params),
            )
            return (
                int(queued_cancelled.rowcount)
                + int(owner_cancelled.rowcount)
                + int(requeued.rowcount)
            )

    def claim_next_job(self) -> dict[str, Any] | None:
        """Claim the oldest job; BEGIN IMMEDIATE enforces one heavy job globally."""
        now = _now_iso()
        lease_id = f"lease_{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            running = conn.execute(
                "SELECT 1 FROM jobs WHERE status IN ('running','cancelling') LIMIT 1"
            ).fetchone()
            if running is not None:
                return None
            row = conn.execute(
                """SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0
                   ORDER BY created_at, job_id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                """UPDATE jobs
                   SET status='running',stage='starting',message='Worker 已领取任务',
                       started_at=COALESCE(started_at,?),heartbeat_at=?,updated_at=?,
                       attempt=attempt+1,lease_id=?,worker_pid=NULL,
                       worker_identity_json=NULL
                   WHERE job_id=? AND status='queued'""",
                (now, now, now, lease_id, row["job_id"]),
            )
            if changed.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._decode_job(claimed)

    def set_worker_pid(
        self,
        job_id: str,
        pid: int | None,
        *,
        lease_id: str | None = None,
        identity: dict[str, Any] | None = None,
    ) -> bool:
        lease_clause = " AND lease_id=?" if lease_id is not None else ""
        identity_json = _json_dump(identity) if identity is not None else None
        params: tuple[Any, ...] = (
            (pid, identity_json, _now_iso(), _now_iso(), job_id, lease_id)
            if lease_id is not None
            else (pid, identity_json, _now_iso(), _now_iso(), job_id)
        )
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE jobs SET worker_pid=?,worker_identity_json=?,"
                "heartbeat_at=?,updated_at=? "
                f"WHERE job_id=? AND status='running' AND cancel_requested=0{lease_clause}",
                params,
            )
        return changed.rowcount == 1

    def stale_interrupted_jobs(
        self, stale_after_seconds: float = 30.0
    ) -> list[dict[str, Any]]:
        """Return stale leases whose recorded OS workers need verification."""
        cutoff = (
            datetime.now(timezone.utc).astimezone()
            - timedelta(seconds=max(0.0, float(stale_after_seconds)))
        ).isoformat(timespec="seconds")
        stale_clause = (
            "1=1"
            if stale_after_seconds <= 0
            else "(heartbeat_at IS NULL OR heartbeat_at<?)"
        )
        params: tuple[Any, ...] = () if stale_after_seconds <= 0 else (cutoff,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM jobs
                    WHERE status IN ('running','cancelling')
                      AND worker_pid IS NOT NULL
                      AND {stale_clause}
                    ORDER BY created_at,job_id""",
                params,
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def mark_recovery_blocked(
        self, job_id: str, lease_id: str, message: str
    ) -> None:
        """Expose a safe refusal without retiring the worker's lease."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET stage='recovery_blocked',message=?,updated_at=?
                   WHERE job_id=? AND lease_id=?
                     AND status IN ('running','cancelling')""",
                (str(message), _now_iso(), job_id, lease_id),
            )

    def heartbeat(self, job_id: str, *, lease_id: str | None = None) -> None:
        now = _now_iso()
        lease_clause = " AND lease_id=?" if lease_id is not None else ""
        params: tuple[Any, ...] = (
            (now, now, job_id, lease_id)
            if lease_id is not None
            else (now, now, job_id)
        )
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET heartbeat_at=?,updated_at=?
                   WHERE job_id=? AND status IN ('running','cancelling')"""
                + lease_clause,
                params,
            )

    def update_progress(
        self, job_id: str, *, stage: str, progress: float, message: str,
        detail: dict | None = None, lease_id: str | None = None,
    ) -> None:
        now = _now_iso()
        # Progress detail is bounded by the worker contract and is stored as a
        # summary, never as partition data or complete node results.
        summary = _json_dump(detail or {})
        lease_clause = " AND lease_id=?" if lease_id is not None else ""
        params: tuple[Any, ...] = (
            (
                stage, max(0.0, min(1.0, float(progress))), message, summary,
                now, now, job_id, lease_id,
            )
            if lease_id is not None
            else (
                stage, max(0.0, min(1.0, float(progress))), message, summary,
                now, now, job_id,
            )
        )
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET stage=?,progress=?,message=?,summary_json=?,
                   heartbeat_at=?,updated_at=?
                   WHERE job_id=? AND status IN ('running','cancelling')"""
                + lease_clause,
                params,
            )

    def cancellation_requested(self, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def request_cancel(self, job_id: str, *, user_id: str, session_id: str) -> bool:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT status,kind FROM jobs
                   WHERE job_id=? AND user_id=? AND session_id=?""",
                (job_id, user_id, session_id),
            ).fetchone()
            if row is None or row["status"] in {"done", "failed", "cancelled"}:
                return False
            if row["status"] == "queued":
                conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       cancel_requested=1,message='已在执行前取消',heartbeat_at=NULL,
                       finished_at=?,updated_at=?
                       WHERE job_id=? AND status='queued'""",
                    (now, now, job_id),
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
            else:
                conn.execute(
                    """UPDATE jobs SET status='cancelling',cancel_requested=1,
                       message='正在终止底层执行进程',updated_at=?
                       WHERE job_id=? AND status IN ('running','cancelling')""",
                    (now, job_id),
                )
            return True

    def request_cancel_scope(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """Atomically request cancellation for every unfinished session job."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT OR IGNORE INTO deleting_scopes(user_id,session_id,created_at)
                   VALUES(?,?,?)""",
                (user_id, session_id, now),
            )
            rows = conn.execute(
                """SELECT job_id,status,kind,worker_pid,lease_id
                   FROM jobs WHERE user_id=? AND session_id=?
                     AND status IN ('queued','running','cancelling')""",
                (user_id, session_id),
            ).fetchall()
            conn.execute(
                """UPDATE jobs SET status='cancelled',stage='cancelled',
                   cancel_requested=1,message='会话删除前已取消',worker_pid=NULL,
                   worker_identity_json=NULL,lease_id=NULL,heartbeat_at=NULL,
                   finished_at=COALESCE(finished_at,?),updated_at=?
                   WHERE user_id=? AND session_id=? AND status='queued'""",
                (now, now, user_id, session_id),
            )
            conn.execute(
                """UPDATE jobs SET status='cancelling',cancel_requested=1,
                   message='会话删除，正在终止底层执行进程',updated_at=?
                   WHERE user_id=? AND session_id=?
                     AND status IN ('running','cancelling')""",
                (now, user_id, session_id),
            )
            self._sync_cancelled_prepare_versions(conn, now)
        return [dict(row) for row in rows]

    def active_scope_jobs(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT job_id,status,kind,worker_pid,lease_id
                   FROM jobs WHERE user_id=? AND session_id=?
                     AND (
                         status IN ('running','cancelling')
                         OR EXISTS (
                             SELECT 1 FROM attempt_cleanup_queue q
                             WHERE q.job_id=jobs.job_id
                         )
                     )
                   ORDER BY created_at,job_id""",
                (user_id, session_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def scope_objects_for_deletion(
        self, *, user_id: str, session_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Return exact managed object references while retaining the fence.

        The service removes these directories before purging the rows.  This
        ordering keeps submissions fenced for the whole filesystem cleanup and
        leaves enough catalog state for a retry after a process/filesystem
        failure.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleting = conn.execute(
                "SELECT 1 FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
            if deleting is None:
                raise RuntimeError("会话未进入删除状态")
            active = conn.execute(
                """SELECT 1 FROM jobs WHERE user_id=? AND session_id=?
                   AND (
                       status IN ('queued','running','cancelling')
                       OR EXISTS (
                           SELECT 1 FROM attempt_cleanup_queue q
                           WHERE q.job_id=jobs.job_id
                       )
                   ) LIMIT 1""",
                (user_id, session_id),
            ).fetchone()
            if active is not None:
                raise RuntimeError("会话仍有未终止的大图任务")
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """SELECT artifact_id,path FROM artifacts
                       WHERE user_id=? AND session_id=?""",
                    (user_id, session_id),
                ).fetchall()
            ]
            snapshots = [
                dict(row)
                for row in conn.execute(
                    """SELECT snapshot_id,manifest_path FROM snapshots
                       WHERE user_id=? AND session_id=?""",
                    (user_id, session_id),
                ).fetchall()
            ]
        return {"artifacts": artifacts, "snapshots": snapshots}

    def purge_scope(
        self, *, user_id: str, session_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Delete terminal catalog state and return exact managed file refs.

        Filesystem deletion is intentionally left to the service, where each
        manifest can be constrained to the configured snapshot/artifact root.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleting = conn.execute(
                "SELECT 1 FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            ).fetchone()
            if deleting is None:
                raise RuntimeError("会话未进入删除状态")
            active = conn.execute(
                """SELECT 1 FROM jobs WHERE user_id=? AND session_id=?
                   AND (
                       status IN ('queued','running','cancelling')
                       OR EXISTS (
                           SELECT 1 FROM attempt_cleanup_queue q
                           WHERE q.job_id=jobs.job_id
                       )
                   ) LIMIT 1""",
                (user_id, session_id),
            ).fetchone()
            if active is not None:
                raise RuntimeError("会话仍有未终止的大图任务")
            artifacts = [
                dict(row)
                for row in conn.execute(
                    """SELECT artifact_id,path FROM artifacts
                       WHERE user_id=? AND session_id=?""",
                    (user_id, session_id),
                ).fetchall()
            ]
            snapshots = [
                dict(row)
                for row in conn.execute(
                    """SELECT snapshot_id,manifest_path FROM snapshots
                       WHERE user_id=? AND session_id=?""",
                    (user_id, session_id),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM artifacts WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
            conn.execute(
                "DELETE FROM jobs WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
            conn.execute(
                "DELETE FROM snapshots WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
            conn.execute(
                "DELETE FROM graph_versions WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
            conn.execute(
                "DELETE FROM deleting_scopes WHERE user_id=? AND session_id=?",
                (user_id, session_id),
            )
        return {"artifacts": artifacts, "snapshots": snapshots}

    def finish_cancelled(
        self,
        job_id: str,
        message: str = "任务已取消",
        *,
        lease_id: str | None = None,
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,lease_id FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            lease_clause = " AND lease_id=?" if lease_id is not None else ""
            params: tuple[Any, ...] = (
                (message, now, now, job_id, lease_id)
                if lease_id is not None
                else (message, now, now, job_id)
            )
            changed = conn.execute(
                """UPDATE jobs SET status='cancelled',stage='cancelled',message=?,
                   cancel_requested=1,worker_pid=NULL,
                   worker_identity_json=NULL,heartbeat_at=NULL,
                   finished_at=COALESCE(finished_at,?),updated_at=?
                   WHERE job_id=? AND status IN ('queued','running','cancelling')"""
                + lease_clause,
                params,
            )
            if changed.rowcount == 1 and job is not None:
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
            self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)

    def finish_failed(
        self, job_id: str, error: str, *, lease_id: str | None = None
    ) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,cancel_requested,lease_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] in {"done", "failed", "cancelled"}:
                return
            if lease_id is not None and job["lease_id"] != lease_id:
                return
            if job["status"] == "cancelling" or job["cancel_requested"]:
                conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='失败收尾时完成未决取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE job_id=?""",
                    (now, now, job_id),
                )
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return
            changed = conn.execute(
                """UPDATE jobs SET status='failed',stage='failed',message=?,error=?,
                   worker_pid=NULL,worker_identity_json=NULL,
                   heartbeat_at=NULL,finished_at=?,updated_at=?
                   WHERE job_id=? AND status='running' AND cancel_requested=0""",
                (str(error), str(error), now, now, job_id),
            )
            if changed.rowcount == 1:
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
            conn.execute(
                """UPDATE graph_versions SET status='failed',updated_at=?
                   WHERE (user_id,session_id,graph_id,graph_version) IN
                         (SELECT user_id,session_id,graph_id,graph_version FROM jobs
                          WHERE job_id=? AND kind='prepare' AND status='failed')
                     AND status='preparing'""",
                (now, job_id),
            )
            self._sync_failed_derived_version(conn, now, job_id=job_id)

    def finish_timeout(
        self,
        job_id: str,
        *,
        max_job_seconds: float,
        elapsed_seconds: float,
        lease_id: str | None = None,
    ) -> str | None:
        """Lease-fence a wall-time expiry and durably schedule its cleanup.

        ``cancel_requested`` is evaluated while holding the same write lock as
        the terminal transition.  A cancellation that was already persisted
        therefore remains a cancellation rather than being relabelled as a
        timeout.  The return value tells the runtime whether this lease owned
        the transition and may clean its attempt-private filesystem scope.
        """

        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,cancel_requested,lease_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] in {"done", "failed", "cancelled"}:
                return None
            if lease_id is not None and job["lease_id"] != lease_id:
                return None
            if job["status"] == "cancelling" or job["cancel_requested"]:
                changed = conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='作业超时收尾时完成未决取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE job_id=? AND status IN ('running','cancelling')""",
                    (now, now, job_id),
                )
                if changed.rowcount != 1:
                    return None
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return "cancelled"

            bounded_limit = max(0.0, float(max_job_seconds))
            bounded_elapsed = max(0.0, float(elapsed_seconds))
            error = (
                "LARGE_GRAPH_JOB_TIMEOUT: "
                f"底层执行进程超过最大 wall-time {bounded_limit:g} 秒"
            )
            detail = {
                "code": "LARGE_GRAPH_JOB_TIMEOUT",
                "max_job_seconds": bounded_limit,
                "elapsed_seconds": round(bounded_elapsed, 3),
            }
            changed = conn.execute(
                """UPDATE jobs SET status='failed',stage='timeout',message=?,error=?,
                   summary_json=?,worker_pid=NULL,worker_identity_json=NULL,
                   heartbeat_at=NULL,finished_at=?,updated_at=?
                   WHERE job_id=? AND status='running' AND cancel_requested=0""",
                (
                    error,
                    error,
                    _json_dump(detail),
                    now,
                    now,
                    job_id,
                ),
            )
            if changed.rowcount != 1:
                return None
            self._queue_attempt_cleanup(
                conn, job_id, job["lease_id"], created_at=now
            )
            conn.execute(
                """UPDATE graph_versions SET status='failed',updated_at=?
                   WHERE (user_id,session_id,graph_id,graph_version) IN
                         (SELECT user_id,session_id,graph_id,graph_version FROM jobs
                          WHERE job_id=? AND kind='prepare' AND status='failed')
                     AND status='preparing'""",
                (now, job_id),
            )
            self._sync_failed_derived_version(conn, now, job_id=job_id)
            return "failed"

    def finish_resource_rejected(
        self, job_id: str, reason: str, observed: dict[str, int],
        *, lease_id: str | None = None,
    ) -> None:
        """Fail a claimed job whose execution-time resource check is denied."""
        now = _now_iso()
        error = f"INSUFFICIENT_RESOURCES: {reason}"
        detail = {
            "code": "INSUFFICIENT_RESOURCES",
            "resource_estimate": {key: int(value) for key, value in observed.items()},
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,cancel_requested,lease_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None or job["status"] in {"done", "failed", "cancelled"}:
                return
            if lease_id is not None and job["lease_id"] != lease_id:
                return
            if job["status"] == "cancelling" or job["cancel_requested"]:
                conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='资源准入期间收到取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE job_id=?""",
                    (now, now, job_id),
                )
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return
            conn.execute(
                """UPDATE jobs SET status='failed',stage='resource_rejected',
                   message=?,error=?,summary_json=?,worker_pid=NULL,
                   worker_identity_json=NULL,heartbeat_at=NULL,
                   finished_at=?,updated_at=?
                   WHERE job_id=? AND status='running' AND cancel_requested=0""",
                (error, error, _json_dump(detail), now, now, job_id),
            )
            conn.execute(
                """UPDATE graph_versions SET status='failed',updated_at=?
                   WHERE (user_id,session_id,graph_id,graph_version) IN
                         (SELECT user_id,session_id,graph_id,graph_version FROM jobs
                          WHERE job_id=? AND kind='prepare' AND status='failed')
                     AND status='preparing'""",
                (now, job_id),
            )
            self._sync_failed_derived_version(conn, now, job_id=job_id)

    def publish_snapshot(
        self, job_id: str, result: dict[str, Any], *, lease_id: str | None = None
    ) -> bool:
        """Publish snapshot and graph version in one catalog transaction."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"未知大图任务: {job_id}")
            if lease_id is not None and job["lease_id"] != lease_id:
                return False
            if job["cancel_requested"]:
                conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='发布前收到取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=?,updated_at=?
                       WHERE job_id=?""",
                    (now, now, job_id),
                )
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return False
            if job["status"] != "running":
                return False
            conn.execute(
                """INSERT INTO snapshots(
                       snapshot_id,user_id,session_id,graph_id,graph_version,state,
                       manifest_path,node_count,edge_count,partitions,directed,
                       has_weights,format,created_at
                   ) VALUES(?,?,?,?,?,'published',?,?,?,?,?,?,?,?)""",
                (
                    result["snapshot_id"], job["user_id"], job["session_id"],
                    job["graph_id"], job["graph_version"], result["manifest_path"],
                    int(result["node_count"]), int(result["edge_count"]),
                    int(result["partitions"]), int(bool(result["directed"])),
                    int(bool(result.get("has_weights"))), str(result.get("format") or "jsonl-v1"), now,
                ),
            )
            conn.execute(
                """UPDATE graph_versions SET status='published',snapshot_id=?,
                   quality_json=?,updated_at=?
                   WHERE user_id=? AND session_id=? AND graph_id=? AND graph_version=?""",
                (
                    result["snapshot_id"], _json_dump(result.get("quality") or {}), now,
                    job["user_id"], job["session_id"], job["graph_id"], job["graph_version"],
                ),
            )
            conn.execute(
                """UPDATE jobs SET status='done',stage='published',progress=1,
                   message='分区快照已原子发布',summary_json=?,worker_pid=NULL,
                   worker_identity_json=NULL,heartbeat_at=NULL,
                   finished_at=?,updated_at=? WHERE job_id=?""",
                (_json_dump(result.get("summary") or {}), now, now, job_id),
            )
        return True

    def publish_artifact(
        self, job_id: str, result: dict[str, Any], *, lease_id: str | None = None
    ) -> bool:
        """Store bounded summary separately from the full partition artifact."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"未知大图任务: {job_id}")
            if lease_id is not None and job["lease_id"] != lease_id:
                return False
            if job["cancel_requested"]:
                conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='发布前收到取消请求',worker_pid=NULL,
                       worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=?,updated_at=?
                       WHERE job_id=?""",
                    (now, now, job_id),
                )
                self._queue_attempt_cleanup(
                    conn, job_id, job["lease_id"], created_at=now
                )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return False
            if job["status"] != "running":
                return False
            decoded_job = dict(job)
            decoded_job["request"] = _json_load(decoded_job.pop("request_json"), {})
            decoded_job["summary"] = _json_load(
                decoded_job.pop("summary_json"), {}
            )
            decoded_job["preview"] = _json_load(
                decoded_job.pop("preview_json"), []
            )
            # Fill catalog-owned identity fields before validation.  A worker
            # may omit them from its result message, but it may never override
            # the durable job's tenant, graph, version or job identity.
            artifact = dict(result)
            expected_identity = {
                "job_id": job_id,
                "user_id": job["user_id"],
                "session_id": job["session_id"],
                "graph_id": job["graph_id"],
                "graph_version": int(job["graph_version"]),
            }
            for field, expected in expected_identity.items():
                if field in artifact and not _same_json_value(
                    artifact[field], expected
                ):
                    raise ValueError(f"Artifact 与任务 {field} 不一致")
                artifact[field] = expected
            validate_artifact_job_binding(decoded_job, artifact)
            # Physical rename happens before this transaction.  Do not make a
            # result catalog-visible until its immutable manifest, canonical
            # object path and every advertised partition are all verified
            # below this service's managed artifact root.
            validate_artifact_manifest(
                artifact,
                artifact_root=self.db_path.parent / "artifacts",
                expected_scope=(
                    f"{decoded_job['request']['user_scope']}__{job['lease_id']}"
                ),
            )
            derived_target = decoded_job["request"].get("derived_target")
            derived_snapshot = artifact.get("derived_snapshot")
            if (derived_target is None) != (derived_snapshot is None):
                raise ValueError("子图 Artifact 与派生版本预留不一致")
            if derived_target is not None:
                if (
                    not isinstance(derived_target, dict)
                    or not isinstance(derived_snapshot, dict)
                    or artifact.get("algorithm") != "subgraph_extraction"
                ):
                    raise ValueError("派生快照结果类型或算法不一致")
                target_version = int(derived_target["graph_version"])
                source_snapshot = decoded_job["request"].get("snapshot")
                if (
                    not isinstance(source_snapshot, dict)
                    or derived_snapshot.get("source_snapshot_id")
                    != source_snapshot.get("snapshot_id")
                    or int(derived_snapshot.get("source_graph_version", -1))
                    != int(job["graph_version"])
                    or int(derived_target.get("source_graph_version", -1))
                    != int(job["graph_version"])
                ):
                    raise ValueError("派生快照与源版本绑定不一致")
                reserved = conn.execute(
                    """SELECT * FROM graph_versions
                       WHERE user_id=? AND session_id=? AND graph_id=?
                         AND graph_version=? AND status='preparing'""",
                    (
                        job["user_id"], job["session_id"], job["graph_id"],
                        target_version,
                    ),
                ).fetchone()
                if reserved is None:
                    raise ValueError("派生图版本预留已失效")
                snapshot_record = {
                    **derived_snapshot,
                    "user_id": job["user_id"],
                    "session_id": job["session_id"],
                    "graph_id": job["graph_id"],
                    "graph_version": target_version,
                    "state": "published",
                    "policy_id": reserved["policy_id"],
                    "policy": _json_load(reserved["policy_json"], {}),
                    "quality": derived_snapshot.get("quality") or {},
                }
                validate_snapshot_manifest(
                    snapshot_record,
                    snapshot_root=self.db_path.parent / "snapshots",
                    graph_id=job["graph_id"],
                    graph_version=target_version,
                    expected_user_id=job["user_id"],
                    expected_session_id=job["session_id"],
                    expected_scope=(
                        f"{decoded_job['request']['user_scope']}__{job['lease_id']}"
                    ),
                )
                if (
                    int(artifact["summary"].get("new_graph_version", -1))
                    != target_version
                    or artifact["summary"].get("derived_snapshot_id")
                    != derived_snapshot.get("snapshot_id")
                    or artifact["metadata"].get("derived_snapshot_id")
                    != derived_snapshot.get("snapshot_id")
                    or int(artifact["metadata"].get("derived_graph_version", -1))
                    != target_version
                ):
                    raise ValueError("Artifact 摘要与派生快照身份不一致")
                conn.execute(
                    """INSERT INTO snapshots(
                           snapshot_id,user_id,session_id,graph_id,graph_version,
                           state,manifest_path,node_count,edge_count,partitions,
                           directed,has_weights,format,created_at
                       ) VALUES(?,?,?,?,?,'published',?,?,?,?,?,?,?,?)""",
                    (
                        derived_snapshot["snapshot_id"], job["user_id"],
                        job["session_id"], job["graph_id"], target_version,
                        derived_snapshot["manifest_path"],
                        int(derived_snapshot["node_count"]),
                        int(derived_snapshot["edge_count"]),
                        int(derived_snapshot["partitions"]),
                        int(bool(derived_snapshot["directed"])),
                        int(bool(derived_snapshot.get("has_weights"))),
                        str(derived_snapshot.get("format") or "jsonl-v1"), now,
                    ),
                )
                changed = conn.execute(
                    """UPDATE graph_versions SET status='published',snapshot_id=?,
                           quality_json=?,updated_at=?
                       WHERE user_id=? AND session_id=? AND graph_id=?
                         AND graph_version=? AND status='preparing'""",
                    (
                        derived_snapshot["snapshot_id"],
                        _json_dump(derived_snapshot.get("quality") or {}), now,
                        job["user_id"], job["session_id"], job["graph_id"],
                        target_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("派生图版本发布竞争失败")
            conn.execute(
                """INSERT INTO artifacts(
                       artifact_id,job_id,user_id,session_id,graph_id,graph_version,
                       algorithm,path,summary_json,preview_json,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact["artifact_id"], job_id, job["user_id"], job["session_id"],
                    job["graph_id"], job["graph_version"], artifact["algorithm"],
                    artifact["path"], _json_dump(artifact.get("summary") or {}),
                    _json_dump(artifact.get("preview") or []),
                    _json_dump(artifact.get("metadata") or {}), now,
                ),
            )
            conn.execute(
                """UPDATE jobs SET status='done',stage='completed',progress=1,
                   message='并行计算完成',summary_json=?,preview_json=?,artifact_id=?,
                   worker_pid=NULL,worker_identity_json=NULL,
                   heartbeat_at=NULL,finished_at=?,updated_at=? WHERE job_id=?""",
                (
                    _json_dump(artifact.get("summary") or {}),
                    _json_dump(artifact.get("preview") or []), artifact["artifact_id"],
                    now, now, job_id,
                ),
            )
        return True

    def requeue_running_job(
        self, job_id: str, message: str, *, lease_id: str | None = None
    ) -> bool:
        """Lease-fenced handoff for shutdown; report whether it took effect."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,cancel_requested,lease_id FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return False
            if lease_id is not None and row["lease_id"] != lease_id:
                return False
            if row["status"] == "cancelling" or row["cancel_requested"]:
                changed = conn.execute(
                    """UPDATE jobs SET status='cancelled',stage='cancelled',
                       message='服务停止时完成未决取消请求',cancel_requested=1,
                       worker_pid=NULL,worker_identity_json=NULL,heartbeat_at=NULL,
                       finished_at=COALESCE(finished_at,?),updated_at=?
                       WHERE job_id=? AND status IN ('queued','running','cancelling')""",
                    (now, now, job_id),
                )
                if changed.rowcount == 1:
                    self._queue_attempt_cleanup(
                        conn, job_id, row["lease_id"], created_at=now
                    )
                self._sync_cancelled_prepare_versions(conn, now, job_id=job_id)
                return changed.rowcount == 1
            changed = conn.execute(
                """UPDATE jobs SET status='queued',stage='recovered',progress=0,
                   message=?,worker_pid=NULL,worker_identity_json=NULL,
                   lease_id=NULL,heartbeat_at=NULL,updated_at=?
                   WHERE job_id=? AND status='running' AND cancel_requested=0""",
                (message, now, job_id),
            )
            if changed.rowcount == 1:
                self._queue_attempt_cleanup(
                    conn, job_id, row["lease_id"], created_at=now
                )
            return changed.rowcount == 1

    @staticmethod
    def _queue_attempt_cleanup(
        conn: sqlite3.Connection,
        job_id: str,
        lease_id: Any,
        *,
        created_at: str,
    ) -> None:
        if not isinstance(lease_id, str) or not lease_id:
            return
        conn.execute(
            """INSERT OR IGNORE INTO attempt_cleanup_queue(
                   job_id,lease_id,created_at
               ) VALUES(?,?,?)""",
            (job_id, lease_id, created_at),
        )

    def pending_attempt_cleanups(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return durable, lease-specific filesystem cleanup work."""
        bounded_limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT j.*,q.lease_id AS cleanup_lease_id
                   FROM attempt_cleanup_queue q
                   JOIN jobs j ON j.job_id=q.job_id
                   ORDER BY q.created_at,q.job_id,q.lease_id
                   LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        attempts: list[dict[str, Any]] = []
        for row in rows:
            attempt = self._decode_job(row)
            attempt["lease_id"] = attempt.pop("cleanup_lease_id")
            attempts.append(attempt)
        return attempts

    def acknowledge_attempt_cleanup(self, job_id: str, lease_id: str) -> bool:
        """Remove one cleanup record only after its exact lease is clean."""
        with self._connect() as conn:
            changed = conn.execute(
                """DELETE FROM attempt_cleanup_queue
                   WHERE job_id=? AND lease_id=?""",
                (job_id, lease_id),
            )
        return changed.rowcount == 1

    @staticmethod
    def _sync_cancelled_prepare_versions(
        conn: sqlite3.Connection, now: str, *, job_id: str | None = None
    ) -> None:
        """Keep a cancelled prepare job and its reserved version consistent."""
        job_filter = "AND j.job_id=?" if job_id is not None else ""
        params: tuple[Any, ...] = (now, job_id) if job_id is not None else (now,)
        conn.execute(
            f"""UPDATE graph_versions AS gv SET status='cancelled',updated_at=?
                WHERE gv.status='preparing' AND EXISTS (
                    SELECT 1 FROM jobs AS j
                    WHERE j.user_id=gv.user_id AND j.session_id=gv.session_id
                      AND j.graph_id=gv.graph_id
                      AND j.graph_version=gv.graph_version
                      AND j.kind='prepare' AND j.status='cancelled'
                      {job_filter}
                )""",
            params,
        )
        rows = conn.execute(
            """SELECT user_id,session_id,graph_id,request_json FROM jobs AS j
               WHERE j.kind='compute' AND j.status='cancelled' """
            + ("AND j.job_id=?" if job_id is not None else ""),
            ((job_id,) if job_id is not None else ()),
        ).fetchall()
        for row in rows:
            request = _json_load(row["request_json"], {})
            target = request.get("derived_target") if isinstance(request, dict) else None
            if not isinstance(target, dict):
                continue
            try:
                target_version = int(target["graph_version"])
            except (KeyError, TypeError, ValueError):
                continue
            conn.execute(
                """UPDATE graph_versions SET status='cancelled',updated_at=?
                   WHERE user_id=? AND session_id=? AND graph_id=?
                     AND graph_version=? AND status='preparing'""",
                (
                    now, row["user_id"], row["session_id"], row["graph_id"],
                    target_version,
                ),
            )

    @staticmethod
    def _sync_failed_derived_version(
        conn: sqlite3.Connection, now: str, *, job_id: str
    ) -> None:
        row = conn.execute(
            """SELECT user_id,session_id,graph_id,request_json FROM jobs
               WHERE job_id=? AND kind='compute' AND status='failed'""",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        request = _json_load(row["request_json"], {})
        target = request.get("derived_target") if isinstance(request, dict) else None
        if not isinstance(target, dict):
            return
        try:
            target_version = int(target["graph_version"])
        except (KeyError, TypeError, ValueError):
            return
        conn.execute(
            """UPDATE graph_versions SET status='failed',updated_at=?
               WHERE user_id=? AND session_id=? AND graph_id=?
                 AND graph_version=? AND status='preparing'""",
            (
                now, row["user_id"], row["session_id"], row["graph_id"],
                target_version,
            ),
        )

    def execution_jobs(
        self, *, user_id: str, session_id: str, job_id: str | None = None,
        run_id: str | None = None, generation: int | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Resolve durable work by its original execution owner, including after restart."""
        clauses = ["user_id=?", "session_id=?"]
        values: list[Any] = [user_id, session_id]
        for key, value in (("job_id", job_id), ("run_id", run_id), ("generation", generation)):
            if value is not None:
                clauses.append(f"json_extract(request_json, '$.execution.{key}')=?")
                values.append(value)
        if active_only:
            clauses.append("status IN ('queued','running','cancelling')")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at,job_id",
                values,
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def get_job(self, job_id: str, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs WHERE job_id=? AND user_id=? AND session_id=?""",
                (job_id, user_id, session_id),
            ).fetchone()
        return self._decode_job(row) if row is not None else None

    def get_job_result(
        self, job_id: str, *, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        job = self.get_job(job_id, user_id=user_id, session_id=session_id)
        if job is None:
            return None
        # The full artifact is never read here; callers receive a path/reference
        # plus bounded summary and preview only.
        payload = {
            "job_id": job_id,
            "kind": job["kind"],
            "status": job["status"],
            "graph_id": job["graph_id"],
            "graph_version": job["graph_version"],
            "summary": job["summary"],
            "preview": job["preview"],
            "artifact_id": job.get("artifact_id"),
            "error": job.get("error"),
        }
        if job["kind"] == "prepare":
            payload.update(
                {
                    "representation": "partitioned_snapshot",
                    "num_nodes": int(job["summary"].get("node_count", 0)),
                    "num_edges": int(job["summary"].get("edge_count", 0)),
                    "current_graph_replaced": False,
                    "message": (
                        "分区快照已发布；请使用 run_large_graph_algorithm 并显式传入 "
                        "graph_id 与 graph_version。"
                    ),
                }
            )
        else:
            payload["representation"] = "partitioned_artifact"
            if "new_graph_version" in job["summary"]:
                payload["new_graph_version"] = int(
                    job["summary"]["new_graph_version"]
                )
                payload["current_graph_replaced"] = bool(
                    job["summary"].get("current_graph_replaced", False)
                )
                payload["derived_version_published"] = bool(
                    job["summary"].get("derived_version_published", True)
                )
        return payload

    def list_versions(self, *, user_id: str, session_id: str, graph_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT gv.*,s.manifest_path,s.node_count,s.edge_count,s.partitions,
                          s.directed,s.has_weights,s.format
                   FROM graph_versions gv LEFT JOIN snapshots s
                     ON s.snapshot_id=gv.snapshot_id
                   WHERE gv.user_id=? AND gv.session_id=? AND gv.graph_id=?
                   ORDER BY gv.graph_version""",
                (user_id, session_id, graph_id),
            ).fetchall()
        return [self._decode_version(row) for row in rows]

    def get_published_snapshot(
        self, *, user_id: str, session_id: str, graph_id: str, graph_version: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT gv.source_json AS version_source_json,
                          gv.policy_id,gv.policy_json,gv.quality_json,s.*
                   FROM graph_versions gv JOIN snapshots s ON s.snapshot_id=gv.snapshot_id
                   WHERE gv.user_id=? AND gv.session_id=? AND gv.graph_id=?
                     AND gv.graph_version=? AND gv.status='published' AND s.state='published'""",
                (user_id, session_id, graph_id, int(graph_version)),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["directed"] = bool(result["directed"])
        result["has_weights"] = bool(result["has_weights"])
        result["version_source"] = _json_load(
            result.pop("version_source_json"), {}
        )
        result["policy"] = _json_load(result.pop("policy_json"), {})
        result["quality"] = _json_load(result.pop("quality_json"), {})
        return result

    def get_artifact(
        self, artifact_id: str, *, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT a.* FROM artifacts AS a
                   JOIN jobs AS j ON j.job_id=a.job_id
                   WHERE a.artifact_id=? AND a.user_id=? AND a.session_id=?
                     AND j.user_id=a.user_id AND j.session_id=a.session_id
                     AND j.graph_id=a.graph_id
                     AND j.graph_version=a.graph_version
                     AND j.kind='compute' AND j.status='done'
                     AND j.artifact_id=a.artifact_id""",
                (artifact_id, user_id, session_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = _json_load(result.pop("summary_json"), {})
        result["preview"] = _json_load(result.pop("preview_json"), [])
        result["metadata"] = _json_load(result.pop("metadata_json"), {})
        return result

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["request"] = _json_load(result.pop("request_json"), {})
        result["summary"] = _json_load(result.pop("summary_json"), {})
        result["preview"] = _json_load(result.pop("preview_json"), [])
        result["worker_identity"] = _json_load(
            result.pop("worker_identity_json", None), None
        )
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    @staticmethod
    def _decode_version(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source"] = _json_load(result.pop("source_json"), {})
        result["policy"] = _json_load(result.pop("policy_json"), {})
        result["quality"] = _json_load(result.pop("quality_json"), {})
        if result.get("directed") is not None:
            result["directed"] = bool(result["directed"])
        if result.get("has_weights") is not None:
            result["has_weights"] = bool(result["has_weights"])
        return result
