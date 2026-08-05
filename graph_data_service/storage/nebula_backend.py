"""NebulaGraph hybrid-storage repository.

NPZ/GraphData remains the local source for snapshots and GraphML.  This
repository materialises a versioned GraphData into NebulaGraph and exposes a
small, session-scoped set of native graph queries.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import numpy as np

from graph_data_service.graph_data import GraphData


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX_VID_RE = re.compile(r"[0-9a-f]{64}")

GRAPH_TAG = "igraph_graph"
ENTITY_TAG = "igraph_entity"
RELATION_EDGE = "igraph_relation"
ENTITY_GRAPH_INDEX = "idx_igraph_entity_graph_key"
RELATION_GRAPH_INDEX = "idx_igraph_relation_graph_key"
_IMPORTER_NULL = "__IGRAPH_NULL_7f8f6c9b__"

_GRAPH_FIELD_TYPES = {
    "scope_key": "string",
    "graph_key": "string",
    "user_id": "string",
    "session_id": "string",
    "graph_version": "int",
    "node_count": "int",
    "edge_count": "int",
    "directed": "bool",
    "graph_name": "string",
    "graph_desc": "string",
    "graph_label_json": "string",
    "synced_at": "string",
}
_ENTITY_FIELD_TYPES = {
    "graph_key": "string",
    "local_node_id": "int",
    "node_name": "string",
    "node_type": "string",
    "node_label_json": "string",
    "node_text": "string",
}
_RELATION_FIELD_TYPES = {
    "graph_key": "string",
    "local_edge_id": "int",
    "edge_name": "string",
    "edge_type": "string",
    "edge_label_json": "string",
    "weight": "double",
    "edge_text": "string",
    "confidence": "double",
}


class NebulaRepositoryError(RuntimeError):
    """Stable, user-facing repository error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "backend": "nebula",
            "code": self.code,
            "error": self.message,
        }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _parse_hosts(raw: str) -> tuple[tuple[str, int], ...]:
    rows: list[tuple[str, int]] = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"NEBULA_HOSTS 条目缺少端口: {item}")
        host, port_text = item.rsplit(":", 1)
        host = host.strip().strip("[]")
        if not host:
            raise ValueError(f"NEBULA_HOSTS 主机为空: {item}")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"NEBULA_HOSTS 端口无效: {item}") from exc
        if port <= 0 or port > 65535:
            raise ValueError(f"NEBULA_HOSTS 端口超出范围: {item}")
        rows.append((host, port))
    return tuple(rows)


@dataclass(frozen=True)
class NebulaConfig:
    enabled: bool
    hosts: tuple[tuple[str, int], ...]
    user: str
    password: str
    space: str
    pool_size: int = 10
    batch_size: int = 256
    schema_wait_seconds: int = 30
    max_retries: int = 3
    partition_num: int = 10
    replica_factor: int = 1
    import_method: str = "auto"
    importer_threshold: int = 100_000
    importer_image: str = "docker.io/vesoft/nebula-importer:v4"
    importer_binary: str = ""
    importer_staging_dir: str = "data/nebula_importer"
    importer_batch_size: int = 1000
    importer_reader_concurrency: int = 4
    importer_concurrency: int = 16
    importer_timeout_seconds: int = 21_600
    config_error: str = ""

    @classmethod
    def from_env(cls) -> "NebulaConfig":
        enabled = _env_bool("NEBULA_ENABLED", False)
        config_error = ""
        try:
            hosts = _parse_hosts(os.getenv("NEBULA_HOSTS", ""))
        except ValueError as exc:
            hosts = ()
            config_error = str(exc)
        return cls(
            enabled=enabled,
            hosts=hosts,
            user=os.getenv("NEBULA_USER", "").strip(),
            password=os.getenv("NEBULA_PASSWORD", ""),
            space=os.getenv("NEBULA_SPACE", "igraph").strip(),
            pool_size=_env_int("NEBULA_POOL_SIZE", 10),
            batch_size=_env_int("NEBULA_BATCH_SIZE", 256),
            schema_wait_seconds=_env_int("NEBULA_SCHEMA_WAIT_SECONDS", 30),
            max_retries=_env_int("NEBULA_MAX_RETRIES", 3),
            partition_num=10,
            replica_factor=1,
            import_method=os.getenv("NEBULA_IMPORT_METHOD", "auto").strip().lower(),
            importer_threshold=_env_int("NEBULA_IMPORTER_THRESHOLD", 100_000),
            importer_image=os.getenv(
                "NEBULA_IMPORTER_IMAGE", "docker.io/vesoft/nebula-importer:v4"
            ).strip(),
            importer_binary=os.getenv("NEBULA_IMPORTER_BINARY", "").strip(),
            importer_staging_dir=os.getenv(
                "NEBULA_IMPORTER_STAGING_DIR", "data/nebula_importer"
            ).strip(),
            importer_batch_size=_env_int("NEBULA_IMPORTER_BATCH_SIZE", 1000),
            importer_reader_concurrency=_env_int(
                "NEBULA_IMPORTER_READER_CONCURRENCY", 4
            ),
            importer_concurrency=_env_int("NEBULA_IMPORTER_CONCURRENCY", 16),
            importer_timeout_seconds=_env_int(
                "NEBULA_IMPORTER_TIMEOUT_SECONDS", 21_600
            ),
            config_error=config_error,
        )

    def validate(self) -> None:
        if self.config_error:
            raise NebulaRepositoryError("NEBULA_UNAVAILABLE", self.config_error)
        if not self.hosts:
            raise NebulaRepositoryError("NEBULA_UNAVAILABLE", "NEBULA_HOSTS 不能为空")
        if not self.user:
            raise NebulaRepositoryError("NEBULA_UNAVAILABLE", "NEBULA_USER 不能为空")
        if not self.password:
            raise NebulaRepositoryError("NEBULA_UNAVAILABLE", "NEBULA_PASSWORD 不能为空")
        if not _IDENTIFIER_RE.fullmatch(self.space):
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE",
                "NEBULA_SPACE 只能包含字母、数字和下划线，且不能以数字开头",
            )
        if self.replica_factor % 2 == 0:
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE", "NebulaGraph replica_factor 必须为奇数"
            )
        if self.import_method not in {"auto", "client", "importer"}:
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE",
                "NEBULA_IMPORT_METHOD 必须为 auto、client 或 importer",
            )
        if self.import_method != "client" and not (
            self.importer_binary or self.importer_image
        ):
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE",
                "Importer 模式需要配置 NEBULA_IMPORTER_BINARY 或 NEBULA_IMPORTER_IMAGE",
            )


def scope_key(user_id: str, session_id: str) -> str:
    user = str(user_id or "_anonymous")
    return hashlib.sha256(f"scope\0{user}\0{session_id}".encode("utf-8")).hexdigest()


def graph_key_for(scope: str, graph_version: int) -> str:
    return hashlib.sha256(f"graph\0{scope}\0{int(graph_version)}".encode("utf-8")).hexdigest()


def node_vid(graph_key: str, node_id: int) -> str:
    return hashlib.sha256(f"node\0{graph_key}\0{int(node_id)}".encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, np.ndarray):
        return [_jsonable(v) for v in value.tolist()]
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    cast = getattr(value, "cast", None)
    if callable(cast):
        try:
            cast_value = cast()
            if cast_value is not value:
                return _jsonable(cast_value)
        except Exception:
            pass
    return str(value)


def _ngql_literal(value: Any) -> str:
    value = _jsonable(value)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else "NULL"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(str(value), ensure_ascii=False)


def _json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def _count_non_finite(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, np.ndarray):
        if np.issubdtype(value.dtype, np.number):
            return int(np.count_nonzero(~np.isfinite(value)))
        return sum(_count_non_finite(item) for item in value.flat)
    if isinstance(value, np.generic):
        return _count_non_finite(value.item())
    if isinstance(value, float):
        return 0 if math.isfinite(value) else 1
    if isinstance(value, dict):
        return sum(_count_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(_count_non_finite(item) for item in value)
    return 0


def _array_entry(array: Optional[np.ndarray], index: int) -> Any:
    if array is None:
        return None
    return _jsonable(array[index])


def _string_entry(values: Any, index: int, default: str = "") -> str:
    if values is None:
        return default
    value = values[index]
    if isinstance(value, np.generic):
        value = value.item()
    if value is None:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return str(value)


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), max(1, size)):
        yield values[start : start + max(1, size)]


def _row_get(row: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in row:
        return row[key]
    target = key.lower()
    for existing, value in row.items():
        if str(existing).lower() == target:
            return value
    return default


class NebulaGraphRepository:
    """Connection pool, schema bootstrap, GraphData sync, and fixed nGQL queries."""

    def __init__(
        self,
        config: Optional[NebulaConfig] = None,
        *,
        connection_pool: Any = None,
    ) -> None:
        self.config = config or NebulaConfig.from_env()
        self._pool = connection_pool
        self._pool_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise NebulaRepositoryError(
                "NEBULA_DISABLED", "NebulaGraph 未启用，请设置 NEBULA_ENABLED=1"
            )
        self.config.validate()

    def _ensure_pool(self) -> Any:
        self._require_enabled()
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:
                return self._pool
            try:
                from nebula3.Config import Config as ClientConfig
                from nebula3.gclient.net import ConnectionPool
            except ImportError as exc:
                raise NebulaRepositoryError(
                    "NEBULA_UNAVAILABLE",
                    "缺少 nebula3-python，请安装 requirements.txt 中的依赖",
                ) from exc
            client_config = ClientConfig()
            client_config.max_connection_pool_size = self.config.pool_size
            pool = ConnectionPool()
            try:
                ok = pool.init(list(self.config.hosts), client_config)
            except Exception as exc:
                raise NebulaRepositoryError(
                    "NEBULA_UNAVAILABLE", f"连接 NebulaGraph 失败: {exc}"
                ) from exc
            if not ok:
                raise NebulaRepositoryError(
                    "NEBULA_UNAVAILABLE", "NebulaGraph 连接池初始化失败"
                )
            self._pool = pool
            return pool

    def close(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass

    @staticmethod
    def _result_succeeded(result: Any) -> bool:
        checker = getattr(result, "is_succeeded", None)
        return bool(checker()) if callable(checker) else True

    @staticmethod
    def _result_error(result: Any) -> str:
        getter = getattr(result, "error_msg", None)
        if callable(getter):
            try:
                return str(_jsonable(getter()))
            except Exception:
                return "unknown query error"
        return "unknown query error"

    @staticmethod
    def _result_rows(result: Any) -> list[dict[str, Any]]:
        if result is None:
            return []
        if isinstance(result, list):
            return [dict(row) for row in result if isinstance(row, dict)]
        keys_getter = getattr(result, "keys", None)
        row_size_getter = getattr(result, "row_size", None)
        row_values_getter = getattr(result, "row_values", None)
        if not (
            callable(keys_getter)
            and callable(row_size_getter)
            and callable(row_values_getter)
        ):
            return []
        keys = [str(_jsonable(k)) for k in keys_getter()]
        rows: list[dict[str, Any]] = []
        for index in range(int(row_size_getter())):
            values = row_values_getter(index)
            rows.append(
                {
                    keys[col]: _jsonable(values[col])
                    for col in range(min(len(keys), len(values)))
                }
            )
        return rows

    def _execute_once(self, query: str, *, use_space: bool = True) -> Any:
        pool = self._ensure_pool()
        try:
            with pool.session_context(self.config.user, self.config.password) as session:
                if use_space:
                    selected = session.execute(f"USE `{self.config.space}`")
                    if not self._result_succeeded(selected):
                        raise NebulaRepositoryError(
                            "QUERY_FAILED",
                            f"选择 NebulaGraph Space 失败: {self._result_error(selected)}",
                        )
                result = session.execute(query)
        except NebulaRepositoryError:
            raise
        except Exception as exc:
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE", f"NebulaGraph 请求失败: {exc}"
            ) from exc
        if not self._result_succeeded(result):
            raise NebulaRepositoryError(
                "QUERY_FAILED", f"nGQL 执行失败: {self._result_error(result)}"
            )
        return result

    def _execute(self, query: str, *, use_space: bool = True) -> Any:
        last_error: Optional[NebulaRepositoryError] = None
        for attempt in range(self.config.max_retries):
            try:
                return self._execute_once(query, use_space=use_space)
            except NebulaRepositoryError as exc:
                last_error = exc
                if exc.code != "NEBULA_UNAVAILABLE" or attempt + 1 >= self.config.max_retries:
                    raise
                self.close()
                time.sleep(min(0.2 * (2**attempt), 1.0))
        assert last_error is not None
        raise last_error

    def _query_rows(self, query: str, *, use_space: bool = True) -> list[dict[str, Any]]:
        return self._result_rows(self._execute(query, use_space=use_space))

    @staticmethod
    def _rows_contain(rows: list[dict[str, Any]], expected: str) -> bool:
        return any(str(value) == expected for row in rows for value in row.values())

    def _wait_for(self, predicate: Callable[[], bool], message: str) -> None:
        deadline = time.monotonic() + self.config.schema_wait_seconds
        last_error: Optional[Exception] = None
        while time.monotonic() <= deadline:
            try:
                if predicate():
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.5)
        suffix = f": {last_error}" if last_error else ""
        raise NebulaRepositoryError("SCHEMA_MISMATCH", f"{message}{suffix}")

    def ensure_schema(self) -> None:
        self._require_enabled()
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            space = self.config.space
            self._execute(
                f"CREATE SPACE IF NOT EXISTS `{space}` "
                f"(partition_num={self.config.partition_num}, "
                f"replica_factor={self.config.replica_factor}, "
                "vid_type=FIXED_STRING(64))",
                use_space=False,
            )
            self._wait_for(
                lambda: self._rows_contain(
                    self._query_rows("SHOW SPACES", use_space=False), space
                )
                and self._space_is_usable(),
                f"等待 Space `{space}` 创建超时",
            )
            self._verify_space_vid_type(space)

            self._execute(
                f"CREATE TAG IF NOT EXISTS {GRAPH_TAG}("
                "scope_key string, graph_key string, user_id string, session_id string, "
                "graph_version int, node_count int, edge_count int, directed bool, "
                "graph_name string, graph_desc string, graph_label_json string, "
                "synced_at string)"
            )
            self._execute(
                f"CREATE TAG IF NOT EXISTS {ENTITY_TAG}("
                "graph_key string, local_node_id int, node_name string, node_type string, "
                "node_label_json string, node_text string)"
            )
            self._execute(
                f"CREATE EDGE IF NOT EXISTS {RELATION_EDGE}("
                "graph_key string, local_edge_id int, edge_name string, edge_type string, "
                "edge_label_json string, weight double, edge_text string, confidence double)"
            )
            self._wait_for(
                lambda: all(
                    self._rows_contain(self._query_rows("SHOW TAGS"), name)
                    for name in (GRAPH_TAG, ENTITY_TAG)
                )
                and self._rows_contain(self._query_rows("SHOW EDGES"), RELATION_EDGE),
                "等待 NebulaGraph TAG/EDGE Schema 生效超时",
            )

            self._verify_schema_fields(
                f"DESCRIBE TAG {GRAPH_TAG}",
                _GRAPH_FIELD_TYPES,
            )
            self._verify_schema_fields(
                f"DESCRIBE TAG {ENTITY_TAG}",
                _ENTITY_FIELD_TYPES,
            )
            self._verify_schema_fields(
                f"DESCRIBE EDGE {RELATION_EDGE}",
                _RELATION_FIELD_TYPES,
            )

            self._execute(
                f"CREATE TAG INDEX IF NOT EXISTS {ENTITY_GRAPH_INDEX} "
                f"ON {ENTITY_TAG}(graph_key(64))"
            )
            self._execute(
                f"CREATE EDGE INDEX IF NOT EXISTS {RELATION_GRAPH_INDEX} "
                f"ON {RELATION_EDGE}(graph_key(64))"
            )
            self._wait_for(
                lambda: self._rows_contain(
                    self._query_rows("SHOW TAG INDEXES"), ENTITY_GRAPH_INDEX
                )
                and self._rows_contain(
                    self._query_rows("SHOW EDGE INDEXES"), RELATION_GRAPH_INDEX
                ),
                "等待 NebulaGraph 索引 Schema 生效超时",
            )
            self._wait_for(
                self._indexes_are_queryable,
                "等待 NebulaGraph 索引可查询超时",
            )
            self._schema_ready = True

    def _space_is_usable(self) -> bool:
        try:
            self._execute("RETURN 1 AS ok")
            return True
        except NebulaRepositoryError:
            return False

    def _indexes_are_queryable(self) -> bool:
        probe = _ngql_literal("__igraph_schema_probe__")
        try:
            self._query_rows(
                f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {probe} "
                "YIELD id(vertex) AS vid"
            )
            self._query_rows(
                f"LOOKUP ON {RELATION_EDGE} WHERE {RELATION_EDGE}.graph_key == {probe} "
                "YIELD src(edge) AS src"
            )
            return True
        except NebulaRepositoryError:
            return False

    def _verify_space_vid_type(self, space: str) -> None:
        rows = self._query_rows(f"SHOW CREATE SPACE `{space}`", use_space=False)
        definition = "\n".join(str(value) for row in rows for value in row.values())
        if not re.search(r"vid_type\s*=\s*FIXED_STRING\s*\(\s*64\s*\)", definition, re.I):
            raise NebulaRepositoryError(
                "SCHEMA_MISMATCH",
                f"现有 Space `{space}` 的 vid_type 不是 FIXED_STRING(64)",
            )

    @staticmethod
    def _type_matches(actual: str, expected: str) -> bool:
        normalized = re.sub(r"\s+", "", str(actual)).lower()
        if expected == "int":
            return normalized in {"int", "int64"}
        return normalized == expected

    def _verify_schema_fields(self, query: str, required: dict[str, str]) -> None:
        rows = self._query_rows(query)
        fields = {
            str(_row_get(row, "Field", next(iter(row.values()), ""))): str(
                _row_get(row, "Type", "")
            )
            for row in rows
        }
        missing = set(required) - set(fields)
        if missing:
            raise NebulaRepositoryError(
                "SCHEMA_MISMATCH",
                f"现有 NebulaGraph Schema 缺少字段: {', '.join(sorted(missing))}",
            )
        incompatible = [
            f"{name}({fields[name]} != {expected})"
            for name, expected in required.items()
            if not self._type_matches(fields[name], expected)
        ]
        if incompatible:
            raise NebulaRepositoryError(
                "SCHEMA_MISMATCH",
                "现有 NebulaGraph Schema 字段类型不兼容: " + ", ".join(incompatible),
            )

    def status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "success": True,
            "backend": "nebula",
            "enabled": self.enabled,
            "configured": bool(
                self.config.hosts
                and self.config.user
                and self.config.password
                and self.config.space
                and not self.config.config_error
            ),
            "configured_hosts": [f"{host}:{port}" for host, port in self.config.hosts],
            "space": self.config.space,
            "pool_size": self.config.pool_size,
            "batch_size": self.config.batch_size,
            "import_method": self.config.import_method,
            "importer_threshold": self.config.importer_threshold,
            "importer_runtime": (
                "binary" if self.config.importer_binary else "docker"
            ),
            "importer_image": (
                "" if self.config.importer_binary else self.config.importer_image
            ),
            "importer_batch_size": self.config.importer_batch_size,
            "importer_reader_concurrency": self.config.importer_reader_concurrency,
            "importer_concurrency": self.config.importer_concurrency,
            "schema_wait_seconds": self.config.schema_wait_seconds,
            "max_retries": self.config.max_retries,
            "connected": False,
            "space_exists": False,
            "schema_ready": False,
        }
        if not self.enabled:
            return base
        try:
            self.config.validate()
            self._execute("RETURN 1 AS ok", use_space=False)
            base["connected"] = True
            spaces = self._query_rows("SHOW SPACES", use_space=False)
            base["space_exists"] = self._rows_contain(spaces, self.config.space)
            if base["space_exists"]:
                tags = self._query_rows("SHOW TAGS")
                edges = self._query_rows("SHOW EDGES")
                tag_indexes = self._query_rows("SHOW TAG INDEXES")
                edge_indexes = self._query_rows("SHOW EDGE INDEXES")
                components_ready = all(
                    self._rows_contain(tags, name) for name in (GRAPH_TAG, ENTITY_TAG)
                ) and self._rows_contain(edges, RELATION_EDGE) and self._rows_contain(
                    tag_indexes, ENTITY_GRAPH_INDEX
                ) and self._rows_contain(edge_indexes, RELATION_GRAPH_INDEX)
                base["schema_components_ready"] = components_ready
                if components_ready:
                    try:
                        self._verify_space_vid_type(self.config.space)
                        self._verify_schema_fields(
                            f"DESCRIBE TAG {GRAPH_TAG}", _GRAPH_FIELD_TYPES
                        )
                        self._verify_schema_fields(
                            f"DESCRIBE TAG {ENTITY_TAG}", _ENTITY_FIELD_TYPES
                        )
                        self._verify_schema_fields(
                            f"DESCRIBE EDGE {RELATION_EDGE}", _RELATION_FIELD_TYPES
                        )
                        base["indexes_queryable"] = self._indexes_are_queryable()
                        base["schema_ready"] = bool(base["indexes_queryable"])
                        self._schema_ready = bool(base["schema_ready"])
                    except NebulaRepositoryError as exc:
                        base.update(exc.to_dict())
                        base["enabled"] = True
                        base["connected"] = True
                        base["schema_ready"] = False
            try:
                host_rows = self._query_rows("SHOW HOSTS", use_space=False)
                versions = [
                    str(value)
                    for row in host_rows
                    for key, value in row.items()
                    if str(key).lower() == "version" and value
                ]
                if versions:
                    base["server_versions"] = sorted(set(versions))
            except NebulaRepositoryError:
                pass
            return base
        except NebulaRepositoryError as exc:
            base.update(exc.to_dict())
            base["enabled"] = True
            base["connected"] = False
            return base

    def _active_metadata(self, scope: str) -> Optional[dict[str, Any]]:
        rows = self._query_rows(
            f"FETCH PROP ON {GRAPH_TAG} {_ngql_literal(scope)} "
            "YIELD properties(vertex).scope_key AS scope_key, "
            "properties(vertex).graph_key AS graph_key, "
            "properties(vertex).graph_version AS graph_version, "
            "properties(vertex).node_count AS node_count, "
            "properties(vertex).edge_count AS edge_count, "
            "properties(vertex).directed AS directed, "
            "properties(vertex).graph_name AS graph_name, "
            "properties(vertex).graph_desc AS graph_desc, "
            "properties(vertex).graph_label_json AS graph_label_json, "
            "properties(vertex).synced_at AS synced_at"
        )
        return rows[0] if rows else None

    def active_metadata(self, user_id: str, session_id: str) -> Optional[dict[str, Any]]:
        """Return active graph metadata for a tenant-scoped session."""
        self.ensure_schema()
        return self._active_metadata(scope_key(user_id, session_id))

    def graph_metadata(self, graph_key: str) -> Optional[dict[str, Any]]:
        self.ensure_schema()
        rows = self._query_rows(
            f"FETCH PROP ON {GRAPH_TAG} {_ngql_literal(graph_key)} "
            "YIELD properties(vertex).scope_key AS scope_key, "
            "properties(vertex).graph_key AS graph_key, "
            "properties(vertex).graph_version AS graph_version, "
            "properties(vertex).node_count AS node_count, "
            "properties(vertex).edge_count AS edge_count, "
            "properties(vertex).directed AS directed, "
            "properties(vertex).graph_name AS graph_name, "
            "properties(vertex).graph_desc AS graph_desc, "
            "properties(vertex).graph_label_json AS graph_label_json, "
            "properties(vertex).synced_at AS synced_at"
        )
        return rows[0] if rows else None

    def version_metadata(
        self, user_id: str, session_id: str, graph_version: int
    ) -> Optional[dict[str, Any]]:
        key = graph_key_for(scope_key(user_id, session_id), graph_version)
        return self.graph_metadata(key)


    def activate_graph_key(
        self,
        user_id: str,
        session_id: str,
        graph_key: str,
        *,
        graph_version_override: Optional[int] = None,
    ) -> dict[str, Any]:
        meta = self.graph_metadata(graph_key)
        if meta is None:
            raise NebulaRepositoryError(
                "QUERY_FAILED", f"NebulaGraph 图引用 {graph_key} 不存在"
            )
        scope = scope_key(user_id, session_id)
        active_version = (
            int(graph_version_override)
            if graph_version_override is not None
            else int(_row_get(meta, "graph_version", 0) or 0)
        )
        values = (
            scope,
            graph_key,
            str(user_id or "_anonymous"),
            session_id,
            active_version,
            int(_row_get(meta, "node_count", 0) or 0),
            int(_row_get(meta, "edge_count", 0) or 0),
            bool(_row_get(meta, "directed", True)),
            _row_get(meta, "graph_name"),
            _row_get(meta, "graph_desc"),
            _row_get(meta, "graph_label_json"),
            _row_get(meta, "synced_at") or datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        self._execute(
            f"INSERT VERTEX {GRAPH_TAG}(scope_key,graph_key,user_id,session_id,graph_version,"
            "node_count,edge_count,directed,graph_name,graph_desc,graph_label_json,synced_at) "
            f"VALUES {_ngql_literal(scope)}:({','.join(_ngql_literal(v) for v in values)})"
        )
        activated = dict(meta)
        activated["graph_version"] = active_version
        return activated

    def activate_version(
        self, user_id: str, session_id: str, graph_version: int
    ) -> dict[str, Any]:
        key = graph_key_for(scope_key(user_id, session_id), graph_version)
        return self.activate_graph_key(user_id, session_id, key)

    def clear_active(self, user_id: str, session_id: str) -> None:
        self.ensure_schema()
        self._execute(f"DELETE VERTEX {_ngql_literal(scope_key(user_id, session_id))}")

    def delete_graph_key(self, graph_key: str) -> int:
        """Delete one immutable graph version by its server-issued key."""
        self.ensure_schema()
        return self._delete_graph_key(graph_key)

    def delete_version(self, user_id: str, session_id: str, graph_version: int) -> int:
        self.ensure_schema()
        return self._delete_graph_key(
            graph_key_for(scope_key(user_id, session_id), graph_version)
        )

    def session_status(self, user_id: str, session_id: str, graph_version: int) -> dict[str, Any]:
        base = self.status()
        base.update(
            {
                "session_id": session_id,
                "graph_version": int(graph_version),
                "synced": False,
                "stale": False,
            }
        )
        if not base.get("connected") or not base.get("schema_ready"):
            return base
        try:
            meta = self._active_metadata(scope_key(user_id, session_id))
            if meta is None:
                return base
            synced_version = int(_row_get(meta, "graph_version", 0) or 0)
            base.update(
                {
                    "synced_graph_version": synced_version,
                    "synced_node_count": int(_row_get(meta, "node_count", 0) or 0),
                    "synced_edge_count": int(_row_get(meta, "edge_count", 0) or 0),
                    "synced_at": _row_get(meta, "synced_at", ""),
                    "synced": synced_version == int(graph_version),
                    "stale": synced_version != int(graph_version),
                }
            )
            return base
        except NebulaRepositoryError as exc:
            base.update(exc.to_dict())
            return base

    @staticmethod
    def _features_omitted(graph: GraphData) -> list[str]:
        rows: list[str] = []
        if graph.node_feat is not None:
            rows.append("node_feat")
        edge_feat = graph.edge_feat
        if edge_feat is not None:
            arr = np.asarray(edge_feat)
            is_confidence = arr.ndim == 1 or (arr.ndim == 2 and arr.shape[1] == 1)
            if not is_confidence:
                rows.append("edge_feat")
        if graph.train_mask is not None:
            rows.append("train_mask")
        if graph.val_mask is not None:
            rows.append("val_mask")
        if graph.test_mask is not None:
            rows.append("test_mask")
        return rows

    @staticmethod
    def _edge_confidence(graph: GraphData, index: int) -> Optional[float]:
        if graph.edge_feat is None:
            return None
        arr = np.asarray(graph.edge_feat)
        if arr.ndim == 1:
            value = float(arr[index])
        elif arr.ndim == 2 and arr.shape[1] == 1:
            value = float(arr[index, 0])
        else:
            return None
        return value if math.isfinite(value) else None

    def _should_use_importer(self, graph: GraphData) -> bool:
        if self.config.import_method == "client":
            return False
        if self.config.import_method == "importer":
            return True
        return (graph.num_nodes + graph.num_edges) >= self.config.importer_threshold

    @staticmethod
    def _importer_cell(value: Any) -> Any:
        value = _jsonable(value)
        if value is None:
            return _IMPORTER_NULL
        if isinstance(value, float) and not math.isfinite(value):
            return _IMPORTER_NULL
        return value

    @staticmethod
    def _importer_props(
        rows: list[tuple[str, str, int, bool]],
    ) -> list[dict[str, Any]]:
        props: list[dict[str, Any]] = []
        for name, type_name, index, nullable in rows:
            prop: dict[str, Any] = {
                "name": name,
                "type": type_name,
                "index": index,
            }
            if nullable:
                prop.update({"nullable": True, "nullValue": _IMPORTER_NULL})
            props.append(prop)
        return props

    def _write_importer_files(
        self,
        graph: GraphData,
        graph_key: str,
        *,
        containerized: bool,
        progress_callback=None,
    ) -> tuple[Path, Path]:
        staging_root = Path(self.config.importer_staging_dir).expanduser().resolve()
        staging_root.mkdir(parents=True, exist_ok=True)
        job_dir = Path(
            tempfile.mkdtemp(prefix=f"{graph_key[:12]}-", dir=str(staging_root))
        )
        os.chmod(job_dir, 0o700)
        nodes_path = job_dir / "nodes.csv"
        edges_path = job_dir / "edges.csv"
        config_path = job_dir / "importer.yaml"

        def report(done: int, total: int, low: float, high: float, message: str) -> None:
            if not progress_callback:
                return
            stride = max(1, total // 100)
            if done == total or done % stride == 0:
                ratio = 1.0 if total == 0 else done / total
                progress_callback(
                    {
                        "progress": low + (high - low) * ratio,
                        "message": message.format(done=done, total=total),
                    }
                )

        # GraphData.id2node intentionally returns a defensive copy. Reading it
        # inside the per-node loop copies the complete mapping N times and turns
        # CSV generation into O(N^2) work for large graphs. Snapshot all graph
        # properties once so this export remains linear in nodes plus edges.
        id2node = graph.id2node
        node_type = graph.node_type
        node_label = graph.node_label
        node_text = graph.node_text
        edge_weight = graph.edge_weight
        edge_names = graph.edge_names
        edge_type = graph.edge_type
        edge_label = graph.edge_label
        edge_text = graph.edge_text

        try:
            with nodes_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                for node_id in range(graph.num_nodes):
                    writer.writerow(
                        [
                            node_vid(graph_key, node_id),
                            graph_key,
                            node_id,
                            id2node.get(node_id, str(node_id)),
                            _string_entry(node_type, node_id, "entity") or "entity",
                            self._importer_cell(
                                _json_text(_array_entry(node_label, node_id))
                            ),
                            self._importer_cell(
                                _string_entry(node_text, node_id, "") or None
                            ),
                        ]
                    )
                    report(
                        node_id + 1,
                        graph.num_nodes,
                        0.06,
                        0.12,
                        "正在生成节点 CSV：{done}/{total}",
                    )

            src_arr, dst_arr = graph.edge_index
            with edges_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                for edge_id in range(graph.num_edges):
                    src = int(src_arr[edge_id])
                    dst = int(dst_arr[edge_id])
                    weight = (
                        float(edge_weight[edge_id])
                        if edge_weight is not None
                        else 1.0
                    )
                    writer.writerow(
                        [
                            node_vid(graph_key, src),
                            node_vid(graph_key, dst),
                            edge_id,
                            graph_key,
                            edge_id,
                            self._importer_cell(
                                _string_entry(edge_names, edge_id, "") or None
                            ),
                            _string_entry(
                                edge_type, edge_id, "related_to"
                            )
                            or "related_to",
                            self._importer_cell(
                                _json_text(_array_entry(edge_label, edge_id))
                            ),
                            self._importer_cell(weight),
                            self._importer_cell(
                                _string_entry(edge_text, edge_id, "") or None
                            ),
                            self._importer_cell(
                                self._edge_confidence(graph, edge_id)
                            ),
                        ]
                    )
                    report(
                        edge_id + 1,
                        graph.num_edges,
                        0.12,
                        0.20,
                        "正在生成边 CSV：{done}/{total}",
                    )

            source_root = Path("/igraph-import") if containerized else job_dir
            importer_config = {
                "client": {
                    "version": "v3",
                    "address": ",".join(
                        f"{host}:{port}" for host, port in self.config.hosts
                    ),
                    "user": self.config.user,
                    "password": self.config.password,
                    "concurrencyPerAddress": min(
                        self.config.importer_concurrency, 32
                    ),
                    "retry": self.config.max_retries,
                    "retryInitialInterval": "1s",
                },
                "manager": {
                    "spaceName": self.config.space,
                    "batch": self.config.importer_batch_size,
                    "readerConcurrency": self.config.importer_reader_concurrency,
                    "importerConcurrency": self.config.importer_concurrency,
                    "statsInterval": "5s",
                },
                "log": {"level": "INFO", "console": True},
                "sources": [
                    {
                        "path": str(source_root / nodes_path.name),
                        "batch": self.config.importer_batch_size,
                        "csv": {
                            "delimiter": ",",
                            "withHeader": False,
                            "lazyQuotes": False,
                        },
                        "tags": [
                            {
                                "name": ENTITY_TAG,
                                "id": {"type": "STRING", "index": 0},
                                "ignoreExistedIndex": False,
                                "props": self._importer_props(
                                    [
                                        ("graph_key", "STRING", 1, False),
                                        ("local_node_id", "INT", 2, False),
                                        ("node_name", "STRING", 3, False),
                                        ("node_type", "STRING", 4, False),
                                        ("node_label_json", "STRING", 5, True),
                                        ("node_text", "STRING", 6, True),
                                    ]
                                ),
                            }
                        ],
                    },
                    {
                        "path": str(source_root / edges_path.name),
                        "batch": self.config.importer_batch_size,
                        "csv": {
                            "delimiter": ",",
                            "withHeader": False,
                            "lazyQuotes": False,
                        },
                        "edges": [
                            {
                                "name": RELATION_EDGE,
                                "src": {"id": {"type": "STRING", "index": 0}},
                                "dst": {"id": {"type": "STRING", "index": 1}},
                                "rank": {"index": 2},
                                "ignoreExistedIndex": False,
                                "props": self._importer_props(
                                    [
                                        ("graph_key", "STRING", 3, False),
                                        ("local_edge_id", "INT", 4, False),
                                        ("edge_name", "STRING", 5, True),
                                        ("edge_type", "STRING", 6, False),
                                        ("edge_label_json", "STRING", 7, True),
                                        ("weight", "DOUBLE", 8, True),
                                        ("edge_text", "STRING", 9, True),
                                        ("confidence", "DOUBLE", 10, True),
                                    ]
                                ),
                            }
                        ],
                    },
                ],
            }
            config_path.write_text(
                json.dumps(importer_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(config_path, 0o600)
            return job_dir, config_path
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def _importer_command(self, job_dir: Path, config_path: Path) -> list[str]:
        if self.config.importer_binary:
            executable = shutil.which(self.config.importer_binary)
            if executable is None:
                candidate = Path(self.config.importer_binary).expanduser()
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    executable = str(candidate.resolve())
            if executable is None:
                raise NebulaRepositoryError(
                    "NEBULA_UNAVAILABLE",
                    "找不到 NEBULA_IMPORTER_BINARY 指定的可执行文件",
                )
            return [executable, "--config", str(config_path)]

        docker = shutil.which("docker")
        if docker is None:
            raise NebulaRepositoryError(
                "NEBULA_UNAVAILABLE",
                "大图导入需要 Docker 或 NEBULA_IMPORTER_BINARY，但当前找不到 docker",
            )
        return [
            docker,
            "run",
            "--rm",
            "--network=host",
            "-v",
            f"{job_dir}:/igraph-import:ro",
            self.config.importer_image,
            "--config",
            "/igraph-import/importer.yaml",
        ]

    def _run_importer(
        self,
        graph: GraphData,
        graph_key: str,
        *,
        progress_callback=None,
    ) -> None:
        containerized = not bool(self.config.importer_binary)
        job_dir, config_path = self._write_importer_files(
            graph,
            graph_key,
            containerized=containerized,
            progress_callback=progress_callback,
        )
        try:
            command = self._importer_command(job_dir, config_path)
            if progress_callback:
                progress_callback(
                    {
                        "progress": 0.20,
                        "message": "CSV 已生成，正在启动 NebulaGraph Importer",
                    }
                )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(job_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                raise NebulaRepositoryError(
                    "NEBULA_UNAVAILABLE", f"启动 NebulaGraph Importer 失败: {exc}"
                ) from exc

            last_lines: list[str] = []
            last_percent = -1
            failed_records = 0
            deadline = time.monotonic() + self.config.importer_timeout_seconds
            assert process.stdout is not None
            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if line:
                        safe_line = line.replace(self.config.password, "***")
                        last_lines.append(safe_line)
                        last_lines = last_lines[-20:]
                        failed_values = [
                            int(value)
                            for value in re.findall(r"Failed:\s*(\d+)", safe_line)
                        ]
                        if failed_values:
                            failed_records = max(failed_records, *failed_values)
                        matches = re.findall(r"(\d+(?:\.\d+)?)%", safe_line)
                        if matches and progress_callback:
                            percent = min(100, max(0, int(float(matches[-1]))))
                            if percent > last_percent:
                                last_percent = percent
                                progress_callback(
                                    {
                                        "progress": 0.20 + 0.70 * (percent / 100),
                                        "message": f"NebulaGraph Importer 导入进度 {percent}%",
                                    }
                                )
                    if time.monotonic() > deadline:
                        process.terminate()
                        raise subprocess.TimeoutExpired(
                            command, self.config.importer_timeout_seconds
                        )
                return_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=10)
                raise NebulaRepositoryError(
                    "QUERY_FAILED",
                    f"NebulaGraph Importer 超过 {self.config.importer_timeout_seconds} 秒未完成",
                ) from exc

            if return_code != 0 or failed_records:
                detail = last_lines[-1] if last_lines else "无可用输出"
                raise NebulaRepositoryError(
                    "QUERY_FAILED",
                    "NebulaGraph Importer 导入失败"
                    f"（exit={return_code}, failed_records={failed_records}）：{detail}",
                )
            if progress_callback:
                progress_callback(
                    {"progress": 0.90, "message": "NebulaGraph Importer 导入完成"}
                )
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _insert_nodes(self, graph: GraphData, graph_key: str, progress_callback=None) -> None:
        total_batches = max(1, math.ceil(graph.num_nodes / self.config.batch_size))
        batch: list[str] = []
        batch_index = 0
        id2node = graph.id2node
        node_type = graph.node_type
        node_label = graph.node_label
        node_text = graph.node_text

        def flush() -> None:
            nonlocal batch, batch_index
            if not batch:
                return
            self._execute(
                f"INSERT VERTEX {ENTITY_TAG}(graph_key,local_node_id,node_name,node_type,"
                "node_label_json,node_text) VALUES " + ",".join(batch)
            )
            batch_index += 1
            if progress_callback:
                progress_callback(
                    {
                        "progress": 0.1 + 0.4 * (batch_index / total_batches),
                        "message": f"已写入节点批次 {batch_index}/{total_batches}",
                    }
                )
            batch = []

        for node_id in range(graph.num_nodes):
            values = (
                graph_key,
                node_id,
                id2node.get(node_id, str(node_id)),
                _string_entry(node_type, node_id, "entity") or "entity",
                _json_text(_array_entry(node_label, node_id)),
                _string_entry(node_text, node_id, "") or None,
            )
            batch.append(
                f"{_ngql_literal(node_vid(graph_key, node_id))}:"
                f"({','.join(_ngql_literal(v) for v in values)})"
            )
            if len(batch) >= self.config.batch_size:
                flush()
        flush()

    def _insert_edges(self, graph: GraphData, graph_key: str, progress_callback=None) -> None:
        total_batches = max(1, math.ceil(graph.num_edges / self.config.batch_size))
        batch: list[str] = []
        batch_index = 0
        edge_weight = graph.edge_weight
        edge_names = graph.edge_names
        edge_type = graph.edge_type
        edge_label = graph.edge_label
        edge_text = graph.edge_text

        def flush() -> None:
            nonlocal batch, batch_index
            if not batch:
                return
            self._execute(
                f"INSERT EDGE {RELATION_EDGE}(graph_key,local_edge_id,edge_name,edge_type,"
                "edge_label_json,weight,edge_text,confidence) VALUES " + ",".join(batch)
            )
            batch_index += 1
            if progress_callback:
                progress_callback(
                    {
                        "progress": 0.5 + 0.4 * (batch_index / total_batches),
                        "message": f"已写入边批次 {batch_index}/{total_batches}",
                    }
                )
            batch = []

        src_arr, dst_arr = graph.edge_index
        for edge_id in range(graph.num_edges):
            src = int(src_arr[edge_id])
            dst = int(dst_arr[edge_id])
            weight = (
                float(edge_weight[edge_id])
                if edge_weight is not None
                else 1.0
            )
            if not math.isfinite(weight):
                weight = None
            values = (
                graph_key,
                edge_id,
                _string_entry(edge_names, edge_id, "") or None,
                _string_entry(edge_type, edge_id, "related_to") or "related_to",
                _json_text(_array_entry(edge_label, edge_id)),
                weight,
                _string_entry(edge_text, edge_id, "") or None,
                self._edge_confidence(graph, edge_id),
            )
            batch.append(
                f"{_ngql_literal(node_vid(graph_key, src))}->"
                f"{_ngql_literal(node_vid(graph_key, dst))}@{edge_id}:"
                f"({','.join(_ngql_literal(v) for v in values)})"
            )
            if len(batch) >= self.config.batch_size:
                flush()
        flush()

    def _publish_metadata(
        self,
        graph: GraphData,
        *,
        user_id: str,
        session_id: str,
        graph_version: int,
        scope: str,
        graph_key: str,
    ) -> None:
        values = (
            scope,
            graph_key,
            str(user_id or "_anonymous"),
            session_id,
            int(graph_version),
            graph.num_nodes,
            graph.num_edges,
            bool(graph.directed),
            graph.name or None,
            graph.graph_desc,
            _json_text(graph.graph_label),
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        value_text = ','.join(_ngql_literal(v) for v in values)
        # Keep immutable per-version metadata at graph_key and update the
        # session-scoped vertex as the active pointer. This makes version
        # switching O(1) without copying the topology.
        self._execute(
            f"INSERT VERTEX {GRAPH_TAG}(scope_key,graph_key,user_id,session_id,graph_version,"
            "node_count,edge_count,directed,graph_name,graph_desc,graph_label_json,synced_at) "
            f"VALUES {_ngql_literal(graph_key)}:({value_text}),"
            f"{_ngql_literal(scope)}:({value_text})"
        )

    def _delete_graph_key(self, graph_key: str) -> int:
        rows = self._query_rows(
            f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {_ngql_literal(graph_key)} "
            "YIELD id(vertex) AS vid"
        )
        vids = [str(_row_get(row, "vid")) for row in rows if _row_get(row, "vid")]
        for batch in _chunks(vids, self.config.batch_size):
            self._execute(
                "DELETE VERTEX " + ",".join(_ngql_literal(vid) for vid in batch) + " WITH EDGE"
            )
        # Version metadata uses graph_key as its VID. The active scope pointer
        # is intentionally separate and is only changed by publish/activate.
        self._execute(f"DELETE VERTEX {_ngql_literal(graph_key)}")
        return len(vids)

    def sync_graph(
        self,
        graph: GraphData,
        *,
        user_id: str,
        session_id: str,
        graph_version: int,
        current_version_getter: Optional[Callable[[], int]] = None,
        expected_current_version: Optional[int] = None,
        progress_callback=None,
        retain_previous: bool = False,
    ) -> dict[str, Any]:
        self.ensure_schema()
        if progress_callback:
            progress_callback({"progress": 0.05, "message": "NebulaGraph Schema 已就绪"})
        scope = scope_key(user_id, session_id)
        staged_key = graph_key_for(scope, graph_version)
        previous = self._active_metadata(scope)
        previous_key = str(_row_get(previous or {}, "graph_key", "") or "")
        warnings: list[str] = []
        edge_feat = np.asarray(graph.edge_feat) if graph.edge_feat is not None else None
        mapped_values = [graph.graph_label, graph.node_label, graph.edge_label, graph.edge_weight]
        if edge_feat is not None and (
            edge_feat.ndim == 1 or (edge_feat.ndim == 2 and edge_feat.shape[1] == 1)
        ):
            mapped_values.append(edge_feat)
        non_finite_count = sum(_count_non_finite(value) for value in mapped_values)
        if non_finite_count:
            warnings.append(
                f"{non_finite_count} 个 NaN/Inf 属性值已转换为 NebulaGraph NULL"
            )
        import_method = "importer" if self._should_use_importer(graph) else "client"
        try:
            if import_method == "importer":
                self._run_importer(
                    graph,
                    staged_key,
                    progress_callback=progress_callback,
                )
            else:
                self._insert_nodes(graph, staged_key, progress_callback=progress_callback)
                self._insert_edges(graph, staged_key, progress_callback=progress_callback)
            expected_version = (
                int(graph_version)
                if expected_current_version is None
                else int(expected_current_version)
            )
            if current_version_getter is not None and int(
                current_version_getter()
            ) != expected_version:
                try:
                    self._delete_graph_key(staged_key)
                except Exception:
                    pass
                raise NebulaRepositoryError(
                    "STALE_DURING_SYNC", "同步期间当前图版本发生变化，未发布本次同步结果"
                )
            self._publish_metadata(
                graph,
                user_id=user_id,
                session_id=session_id,
                graph_version=graph_version,
                scope=scope,
                graph_key=staged_key,
            )
        except Exception as exc:
            try:
                if previous_key != staged_key:
                    self._delete_graph_key(staged_key)
            except Exception:
                pass
            if isinstance(exc, NebulaRepositoryError):
                raise
            raise NebulaRepositoryError(
                "QUERY_FAILED", f"NebulaGraph 图导入失败: {exc}"
            ) from exc

        if previous_key and previous_key != staged_key and not retain_previous:
            try:
                self._delete_graph_key(previous_key)
            except Exception as exc:
                warnings.append(f"旧 NebulaGraph 图版本清理失败: {exc}")
        if progress_callback:
            progress_callback({"progress": 1.0, "message": "NebulaGraph 同步完成"})
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": int(graph_version),
            "graph_key": staged_key,
            "node_count": graph.num_nodes,
            "edge_count": graph.num_edges,
            "import_method": import_method,
            "features_omitted": self._features_omitted(graph),
            "warnings": warnings,
        }

    def _require_synced(
        self, user_id: str, session_id: str, graph_version: int
    ) -> dict[str, Any]:
        self._require_enabled()
        if not self._schema_ready:
            health = self.status()
            if not health.get("connected"):
                raise NebulaRepositoryError(
                    str(health.get("code") or "NEBULA_UNAVAILABLE"),
                    str(health.get("error") or "NebulaGraph 当前不可用"),
                )
            if not health.get("schema_ready"):
                if health.get("code") == "SCHEMA_MISMATCH":
                    raise NebulaRepositoryError(
                        "SCHEMA_MISMATCH",
                        str(health.get("error") or "NebulaGraph Schema 不兼容"),
                    )
                raise NebulaRepositoryError(
                    "NOT_SYNCED", "NebulaGraph Schema 尚未初始化，请先同步当前图"
                )
        meta = self._active_metadata(scope_key(user_id, session_id))
        if meta is None:
            raise NebulaRepositoryError(
                "NOT_SYNCED", "当前会话图尚未同步到 NebulaGraph"
            )
        synced_version = int(_row_get(meta, "graph_version", 0) or 0)
        if synced_version != int(graph_version):
            raise NebulaRepositoryError(
                "STALE_SYNC",
                f"NebulaGraph 图版本为 {synced_version}，当前图版本为 {graph_version}，请重新同步",
            )
        return meta

    @staticmethod
    def _resolve_node(graph: GraphData, node_name: str) -> int:
        try:
            return graph.name_to_id(node_name)
        except KeyError as exc:
            raise NebulaRepositoryError(
                "QUERY_FAILED", f"节点 '{node_name}' 不存在于当前图中"
            ) from exc

    @staticmethod
    def _decode_json_field(value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return json.loads(str(value))
        except (TypeError, ValueError):
            return value

    def _primary_meta(
        self,
        user_id: str,
        session_id: str,
        graph_version: Optional[int] = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        meta = self._active_metadata(scope_key(user_id, session_id))
        if meta is None:
            raise NebulaRepositoryError("NOT_SYNCED", "当前会话在 NebulaGraph 中没有活动图")
        if graph_version is not None:
            active_version = int(_row_get(meta, "graph_version", 0) or 0)
            if active_version != int(graph_version):
                raise NebulaRepositoryError(
                    "STALE_SYNC",
                    f"NebulaGraph 图版本为 {active_version}，请求版本为 {graph_version}",
                )
        return meta

    def _find_node_record(self, graph_key: str, node_name: str) -> dict[str, Any]:
        literal_key = _ngql_literal(graph_key)
        literal_name = _ngql_literal(str(node_name))
        output = (
            "YIELD id(vertex) AS vid, "
            "properties(vertex).local_node_id AS local_node_id, "
            "properties(vertex).node_name AS node_name, "
            "properties(vertex).node_type AS node_type, "
            "properties(vertex).node_label_json AS node_label_json, "
            "properties(vertex).node_text AS node_text"
        )
        rows = self._query_rows(
            f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {literal_key} "
            f"AND {ENTITY_TAG}.node_name == {literal_name} {output} | LIMIT 1"
        )
        if not rows:
            try:
                local_id = int(str(node_name))
            except (TypeError, ValueError):
                local_id = None
            if local_id is not None:
                rows = self._query_rows(
                    f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {literal_key} "
                    f"AND {ENTITY_TAG}.local_node_id == {local_id} {output} | LIMIT 1"
                )
        if not rows:
            raise NebulaRepositoryError(
                "QUERY_FAILED", f"节点 '{node_name}' 不存在于当前 NebulaGraph 图中"
            )
        return rows[0]

    def _fetch_node_records(self, vids: Iterable[str]) -> list[dict[str, Any]]:
        values = [str(vid) for vid in vids if vid]
        rows: list[dict[str, Any]] = []
        for batch in _chunks(values, self.config.batch_size):
            rows.extend(
                self._query_rows(
                    f"FETCH PROP ON {ENTITY_TAG} "
                    + ",".join(_ngql_literal(vid) for vid in batch)
                    + " YIELD id(vertex) AS vid, "
                    "properties(vertex).local_node_id AS local_node_id, "
                    "properties(vertex).node_name AS node_name, "
                    "properties(vertex).node_type AS node_type, "
                    "properties(vertex).node_label_json AS node_label_json, "
                    "properties(vertex).node_text AS node_text"
                )
            )
        return rows

    def _discover_node_vids(
        self,
        graph_key: str,
        *,
        seed_names: Optional[list[str]] = None,
        hops: int = 0,
        direction: str = "both",
        max_nodes: int = 50_000,
        allow_truncate: bool = False,
        expand_to_limit: bool = False,
    ) -> list[str]:
        max_nodes = max(1, int(max_nodes))
        if seed_names:
            seeds = [self._find_node_record(graph_key, name) for name in seed_names]
            seed_vids = [str(_row_get(row, "vid")) for row in seeds]
            if expand_to_limit:
                # Match the local visualization behaviour: highlighted/path
                # nodes are BFS seeds, not the complete visualization. Expand
                # one layer at a time until the display cap is reached.
                vids = list(dict.fromkeys(seed_vids))
                if len(vids) >= max_nodes:
                    if len(vids) > max_nodes and not allow_truncate:
                        raise NebulaRepositoryError(
                            "SUBGRAPH_TOO_LARGE",
                            f"可视化种子节点数超过上限 {max_nodes}",
                        )
                    return vids[:max_nodes]
                seen = set(vids)
                frontier = list(vids)
                clause = self._direction_clause(direction)
                while frontier and len(vids) < max_nodes:
                    next_frontier: list[str] = []
                    remaining = max_nodes - len(vids)
                    for batch in _chunks(frontier, self.config.batch_size):
                        rows = self._query_rows(
                            "GO 1 STEP FROM "
                            + ",".join(_ngql_literal(vid) for vid in batch)
                            + f" OVER {RELATION_EDGE}{clause} "
                            "YIELD DISTINCT id($$) AS vid "
                            f"| LIMIT {max_nodes + 1}"
                        )
                        for row in rows:
                            vid = str(_row_get(row, "vid", "") or "")
                            if not vid or vid in seen:
                                continue
                            seen.add(vid)
                            vids.append(vid)
                            next_frontier.append(vid)
                            if len(vids) >= max_nodes:
                                break
                        remaining = max_nodes - len(vids)
                        if remaining <= 0:
                            break
                    frontier = next_frontier
                return vids
            if hops <= 0:
                return list(dict.fromkeys(seed_vids))
            clause = self._direction_clause(direction)
            rows = self._query_rows(
                f"GO 1 TO {max(1, int(hops))} STEPS FROM "
                + ",".join(_ngql_literal(vid) for vid in seed_vids)
                + f" OVER {RELATION_EDGE}{clause} "
                "YIELD DISTINCT id($$) AS vid "
                f"| LIMIT {max_nodes + 1}"
            )
            vids = list(dict.fromkeys(seed_vids + [str(_row_get(row, "vid")) for row in rows]))
        else:
            rows = self._query_rows(
                f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {_ngql_literal(graph_key)} "
                f"YIELD id(vertex) AS vid | LIMIT {max_nodes + 1}"
            )
            vids = [str(_row_get(row, "vid")) for row in rows]
        if len(vids) > max_nodes:
            if allow_truncate:
                return vids[:max_nodes]
            raise NebulaRepositoryError(
                "SUBGRAPH_TOO_LARGE",
                f"待物化子图节点数超过上限 {max_nodes}，请缩小 hops 或指定节点/类型范围",
            )
        return vids

    def _fetch_internal_edges(
        self,
        graph_key: str,
        node_vids: list[str],
        *,
        max_edges: int = 200_000,
    ) -> list[dict[str, Any]]:
        allowed = set(node_vids)
        seen: set[tuple[str, str, int]] = set()
        rows: list[dict[str, Any]] = []
        for batch in _chunks(node_vids, self.config.batch_size):
            query_rows = self._query_rows(
                "GO FROM " + ",".join(_ngql_literal(vid) for vid in batch)
                + f" OVER {RELATION_EDGE} BIDIRECT "
                "YIELD src(edge) AS src_vid, dst(edge) AS dst_vid, rank(edge) AS edge_rank, "
                f"{RELATION_EDGE}.local_edge_id AS local_edge_id, "
                f"{RELATION_EDGE}.edge_name AS edge_name, "
                f"{RELATION_EDGE}.edge_type AS edge_type, "
                f"{RELATION_EDGE}.edge_label_json AS edge_label_json, "
                f"{RELATION_EDGE}.weight AS weight, "
                f"{RELATION_EDGE}.edge_text AS edge_text, "
                f"{RELATION_EDGE}.confidence AS confidence"
            )
            for row in query_rows:
                src = str(_row_get(row, "src_vid", ""))
                dst = str(_row_get(row, "dst_vid", ""))
                rank = int(_row_get(row, "edge_rank", 0) or 0)
                if src not in allowed or dst not in allowed:
                    continue
                key = (src, dst, rank)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
                if len(rows) > int(max_edges):
                    raise NebulaRepositoryError(
                        "SUBGRAPH_TOO_LARGE",
                        f"待物化子图边数超过上限 {max_edges}，请缩小查询范围",
                    )
        return rows

    @staticmethod
    def _optional_object_array(values: list[Any]) -> Optional[np.ndarray]:
        return np.asarray(values, dtype=object) if any(value is not None for value in values) else None

    def visualization_seed_name_primary(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_version: Optional[int] = None,
    ) -> Optional[str]:
        """Choose a node incident to an edge for a connected visualization."""
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        rows = self._query_rows(
            f"LOOKUP ON {RELATION_EDGE} WHERE "
            f"{RELATION_EDGE}.graph_key == {_ngql_literal(graph_key)} "
            "YIELD src(edge) AS vid | LIMIT 1"
        )
        if not rows:
            rows = self._query_rows(
                f"LOOKUP ON {ENTITY_TAG} WHERE "
                f"{ENTITY_TAG}.graph_key == {_ngql_literal(graph_key)} "
                "YIELD id(vertex) AS vid | LIMIT 1"
            )
        if not rows:
            return None
        vid = str(_row_get(rows[0], "vid", "") or "")
        if not vid:
            return None
        node_rows = self._fetch_node_records([vid])
        if not node_rows:
            return None
        return str(
            _row_get(
                node_rows[0],
                "node_name",
                _row_get(node_rows[0], "local_node_id", vid),
            )
        )

    def materialize_subgraph(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_version: Optional[int] = None,
        seed_names: Optional[list[str]] = None,
        hops: int = 0,
        direction: str = "both",
        max_nodes: int = 50_000,
        max_edges: int = 200_000,
        allow_truncate: bool = False,
        expand_to_limit: bool = False,
    ) -> GraphData:
        """Materialise a bounded Nebula topology as a local GraphData."""
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        vids = self._discover_node_vids(
            graph_key,
            seed_names=seed_names,
            hops=hops,
            direction=direction,
            max_nodes=max_nodes,
            allow_truncate=allow_truncate,
            expand_to_limit=expand_to_limit,
        )
        node_rows = self._fetch_node_records(vids)
        by_vid = {str(_row_get(row, "vid")): row for row in node_rows}
        ordered_vids = [vid for vid in vids if vid in by_vid]
        local_by_vid = {vid: index for index, vid in enumerate(ordered_vids)}
        id2node = {
            index: str(_row_get(by_vid[vid], "node_name", _row_get(by_vid[vid], "local_node_id", index)))
            for index, vid in enumerate(ordered_vids)
        }
        node2id = {name: index for index, name in id2node.items()}
        node_types = [str(_row_get(by_vid[vid], "node_type", "entity") or "entity") for vid in ordered_vids]
        node_labels = [self._decode_json_field(_row_get(by_vid[vid], "node_label_json")) for vid in ordered_vids]
        node_texts = [str(_row_get(by_vid[vid], "node_text", "") or "") for vid in ordered_vids]

        edge_rows = self._fetch_internal_edges(graph_key, ordered_vids, max_edges=max_edges)
        pairs: list[tuple[int, int]] = []
        weights: list[float] = []
        edge_names: list[str] = []
        edge_types: list[str] = []
        edge_labels: list[Any] = []
        edge_texts: list[str] = []
        confidences: list[Optional[float]] = []
        for row in edge_rows:
            src = str(_row_get(row, "src_vid", ""))
            dst = str(_row_get(row, "dst_vid", ""))
            if src not in local_by_vid or dst not in local_by_vid:
                continue
            pairs.append((local_by_vid[src], local_by_vid[dst]))
            raw_weight = _row_get(row, "weight", 1.0)
            try:
                weights.append(float(raw_weight) if raw_weight is not None else 1.0)
            except (TypeError, ValueError):
                weights.append(1.0)
            edge_names.append(str(_row_get(row, "edge_name", "") or ""))
            edge_types.append(str(_row_get(row, "edge_type", "related_to") or "related_to"))
            edge_labels.append(self._decode_json_field(_row_get(row, "edge_label_json")))
            edge_texts.append(str(_row_get(row, "edge_text", "") or ""))
            confidence = _row_get(row, "confidence")
            try:
                confidences.append(float(confidence) if confidence is not None else None)
            except (TypeError, ValueError):
                confidences.append(None)

        edge_index = (
            np.asarray(pairs, dtype=np.int64).T
            if pairs else np.empty((2, 0), dtype=np.int64)
        )
        confidence_array = None
        if any(value is not None for value in confidences):
            confidence_array = np.asarray(
                [np.nan if value is None else value for value in confidences],
                dtype=np.float32,
            )[:, None]
        return GraphData(
            num_nodes=len(ordered_vids),
            edge_index=edge_index,
            id2node=id2node or None,
            node2id=node2id or None,
            edge_names=edge_names or None,
            edge_weight=np.asarray(weights, dtype=np.float32) if weights else None,
            edge_feat=confidence_array,
            graph_desc=str(_row_get(meta, "graph_desc", "") or "") or None,
            directed=bool(_row_get(meta, "directed", True)),
            name=str(_row_get(meta, "graph_name", "") or ""),
            node_label=self._optional_object_array(node_labels),
            node_type=np.asarray(node_types, dtype=object) if node_types else None,
            edge_label=self._optional_object_array(edge_labels),
            edge_type=np.asarray(edge_types, dtype=object) if edge_types else None,
            graph_label=self._decode_json_field(_row_get(meta, "graph_label_json")),
            node_text=node_texts or None,
            edge_text=edge_texts or None,
        )

    def graph_info_primary(
        self, *, user_id: str, session_id: str, graph_version: Optional[int] = None
    ) -> dict[str, Any]:
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        preview_rows = self._query_rows(
            f"LOOKUP ON {ENTITY_TAG} WHERE {ENTITY_TAG}.graph_key == {_ngql_literal(graph_key)} "
            "YIELD properties(vertex).node_name AS node_name | LIMIT 10"
        )
        preview_names = [str(_row_get(row, "node_name")) for row in preview_rows]
        preview = self.materialize_subgraph(
            user_id=user_id,
            session_id=session_id,
            graph_version=int(_row_get(meta, "graph_version", 0)),
            seed_names=preview_names,
            hops=0,
            max_nodes=10,
            max_edges=30,
        )
        src, dst = preview.edge_index
        edges = [
            {
                "src": preview.id2node.get(int(src[i]), str(int(src[i]))),
                "dst": preview.id2node.get(int(dst[i]), str(int(dst[i]))),
                "weight": float(preview.edge_weight[i]) if preview.edge_weight is not None else 1.0,
            }
            for i in range(preview.num_edges)
        ]
        return {
            "loaded": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": int(_row_get(meta, "graph_version", 0)),
            "num_nodes": int(_row_get(meta, "node_count", 0) or 0),
            "num_edges": int(_row_get(meta, "edge_count", 0) or 0),
            "directed": bool(_row_get(meta, "directed", True)),
            "has_node_names": True,
            "has_edge_weight": True,
            "nodes": [preview.id2node[i] for i in range(preview.num_nodes)],
            "edges": edges,
            "preview": {
                "sample_node_count": preview.num_nodes,
                "sample_edge_count": preview.num_edges,
                "node_truncated": int(_row_get(meta, "node_count", 0) or 0) > preview.num_nodes,
                "edge_truncated": int(_row_get(meta, "edge_count", 0) or 0) > preview.num_edges,
                "sample_strategy": "nebula_graph_key_limit",
            },
            "graph_info": {
                "num_nodes": int(_row_get(meta, "node_count", 0) or 0),
                "num_edges": int(_row_get(meta, "edge_count", 0) or 0),
                "directed": bool(_row_get(meta, "directed", True)),
                "has_node_names": True,
                "has_edge_weight": True,
                "has_node_feat": False,
                "has_edge_feat": False,
                "features_omitted": ["node_feat", "multi_dimensional_edge_feat", "split_masks"],
            },
            "graph_ml_readiness": {
                "backend": "nebula",
                "requires_materialization": True,
            },
        }

    def get_neighbors_primary(
        self,
        *,
        user_id: str,
        session_id: str,
        node_name: str,
        graph_version: Optional[int] = None,
        direction: str = "out",
        limit: int = 20,
    ) -> dict[str, Any]:
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        node = self._find_node_record(graph_key, node_name)
        vid = str(_row_get(node, "vid"))
        limit = max(1, min(int(limit), 200))
        clause = self._direction_clause(direction)
        rows = self._query_rows(
            f"GO FROM {_ngql_literal(vid)} OVER {RELATION_EDGE}{clause} "
            "YIELD id($$) AS neighbor_vid, properties($$).local_node_id AS local_node_id, "
            "properties($$).node_name AS node_name, properties($$).node_type AS node_type, "
            f"{RELATION_EDGE}.edge_type AS edge_type, "
            f"{RELATION_EDGE}.edge_label_json AS edge_label_json, "
            f"{RELATION_EDGE}.weight AS weight, {RELATION_EDGE}.edge_text AS edge_text "
            f"| LIMIT {limit}"
        )
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": int(_row_get(meta, "graph_version", 0)),
            "node_name": str(_row_get(node, "node_name", node_name)),
            "direction": direction,
            "neighbors": [
                {
                    "vid": _row_get(row, "neighbor_vid"),
                    "local_node_id": _row_get(row, "local_node_id"),
                    "node_name": _row_get(row, "node_name"),
                    "node_type": _row_get(row, "node_type"),
                    "edge_type": _row_get(row, "edge_type"),
                    "edge_label": self._decode_json_field(_row_get(row, "edge_label_json")),
                    "weight": _row_get(row, "weight"),
                    "edge_text": _row_get(row, "edge_text"),
                }
                for row in rows
            ],
            "count": len(rows),
            "limit": limit,
        }

    def get_node_info_primary(
        self,
        *,
        user_id: str,
        session_id: str,
        node_name: str,
        graph_version: Optional[int] = None,
    ) -> dict[str, Any]:
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        node = self._find_node_record(graph_key, node_name)
        out_rows = self.get_neighbors_primary(
            user_id=user_id, session_id=session_id, node_name=node_name,
            graph_version=graph_version, direction="out", limit=200,
        )["neighbors"]
        in_rows = self.get_neighbors_primary(
            user_id=user_id, session_id=session_id, node_name=node_name,
            graph_version=graph_version, direction="in", limit=200,
        )["neighbors"]
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": int(_row_get(meta, "graph_version", 0)),
            "node_name": str(_row_get(node, "node_name", node_name)),
            "node_id": int(_row_get(node, "local_node_id", 0) or 0),
            "node_type": _row_get(node, "node_type", "entity"),
            "node_label": self._decode_json_field(_row_get(node, "node_label_json")),
            "text": _row_get(node, "node_text"),
            "out_degree": len(out_rows),
            "in_degree": len(in_rows),
            "degree": len(out_rows) + len(in_rows),
            "successors": [row.get("node_name") for row in out_rows],
            "predecessors": [row.get("node_name") for row in in_rows],
            "degree_truncated": len(out_rows) >= 200 or len(in_rows) >= 200,
        }

    def get_edge_info_primary(
        self,
        *,
        user_id: str,
        session_id: str,
        src_name: str,
        dst_name: str,
        graph_version: Optional[int] = None,
    ) -> dict[str, Any]:
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        src = self._find_node_record(graph_key, src_name)
        dst = self._find_node_record(graph_key, dst_name)
        rows = self._query_rows(
            f"GO FROM {_ngql_literal(str(_row_get(src, 'vid')))} OVER {RELATION_EDGE} "
            f"WHERE id($$) == {_ngql_literal(str(_row_get(dst, 'vid')))} "
            "YIELD id($$) AS dst_vid, rank(edge) AS edge_rank, "
            f"{RELATION_EDGE}.local_edge_id AS local_edge_id, "
            f"{RELATION_EDGE}.edge_name AS edge_name, {RELATION_EDGE}.edge_type AS edge_type, "
            f"{RELATION_EDGE}.edge_label_json AS edge_label_json, "
            f"{RELATION_EDGE}.weight AS weight, {RELATION_EDGE}.edge_text AS edge_text "
            "| LIMIT 200"
        )
        edges = [
            {
                "local_edge_id": _row_get(row, "local_edge_id"),
                "edge_name": _row_get(row, "edge_name"),
                "edge_type": _row_get(row, "edge_type"),
                "edge_label": self._decode_json_field(_row_get(row, "edge_label_json")),
                "weight": _row_get(row, "weight"),
                "edge_text": _row_get(row, "edge_text"),
                "rank": _row_get(row, "edge_rank"),
            }
            for row in rows
        ]
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": int(_row_get(meta, "graph_version", 0)),
            "src_name": str(_row_get(src, "node_name", src_name)),
            "dst_name": str(_row_get(dst, "node_name", dst_name)),
            "exists": bool(edges),
            "edges": edges,
            "edge": edges[0] if edges else None,
            "count": len(edges),
        }

    def check_edge_exists_primary(self, **kwargs) -> dict[str, Any]:
        result = self.get_edge_info_primary(**kwargs)
        return {
            "success": True,
            "backend": "nebula",
            "session_id": result["session_id"],
            "graph_version": result["graph_version"],
            "src_name": result["src_name"],
            "dst_name": result["dst_name"],
            "exists": result["exists"],
            "count": result["count"],
        }

    def shortest_path_primary(
        self,
        *,
        user_id: str,
        session_id: str,
        source: str,
        target: str,
        graph_version: Optional[int] = None,
        direction: str = "out",
        max_steps: int = 10,
    ) -> dict[str, Any]:
        meta = self._primary_meta(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key", ""))
        src = self._find_node_record(graph_key, source)
        dst = self._find_node_record(graph_key, target)
        source_vid = str(_row_get(src, "vid"))
        target_vid = str(_row_get(dst, "vid"))
        max_steps = max(1, min(int(max_steps), 20))
        result = self._execute(
            f"FIND SHORTEST PATH FROM {_ngql_literal(source_vid)} TO {_ngql_literal(target_vid)} "
            f"OVER {RELATION_EDGE}{self._direction_clause(direction)} UPTO {max_steps} STEPS "
            "YIELD path AS p | LIMIT 1"
        )
        raw_rows = self._result_rows(result)
        if not raw_rows:
            return {
                "success": True, "backend": "nebula", "session_id": session_id,
                "graph_version": int(_row_get(meta, "graph_version", 0)),
                "source": source, "target": target, "found": False,
                "path": [], "hop_count": None, "weighted": False,
            }
        raw_path = _row_get(raw_rows[0], "p")
        vids = self._extract_path_vids(raw_path)
        names_by_vid = {
            str(_row_get(row, "vid")): str(_row_get(row, "node_name", _row_get(row, "vid")))
            for row in self._fetch_node_records(vids)
        }
        names = [names_by_vid.get(vid, vid) for vid in vids]
        return {
            "success": True, "backend": "nebula", "session_id": session_id,
            "graph_version": int(_row_get(meta, "graph_version", 0)),
            "source": source, "target": target, "found": True,
            "path": names, "hop_count": max(len(names) - 1, 0), "weighted": False,
            "max_steps": max_steps,
        }

    def get_node_info(
        self,
        graph: GraphData,
        *,
        user_id: str,
        session_id: str,
        graph_version: int,
        node_name: str,
    ) -> dict[str, Any]:
        meta = self._require_synced(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key"))
        node_id = self._resolve_node(graph, node_name)
        vid = node_vid(graph_key, node_id)
        rows = self._query_rows(
            f"FETCH PROP ON {ENTITY_TAG} {_ngql_literal(vid)} "
            "YIELD id(vertex) AS vid, properties(vertex).local_node_id AS local_node_id, "
            "properties(vertex).node_name AS node_name, properties(vertex).node_type AS node_type, "
            "properties(vertex).node_label_json AS node_label_json, "
            "properties(vertex).node_text AS node_text"
        )
        if not rows:
            raise NebulaRepositoryError("QUERY_FAILED", "NebulaGraph 中未找到该节点")
        row = rows[0]
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": graph_version,
            "node": {
                "vid": _row_get(row, "vid", vid),
                "local_node_id": int(_row_get(row, "local_node_id", node_id)),
                "node_name": _row_get(row, "node_name", node_name),
                "node_type": _row_get(row, "node_type", "entity"),
                "node_label": self._decode_json_field(_row_get(row, "node_label_json")),
                "node_text": _row_get(row, "node_text"),
            },
        }

    @staticmethod
    def _direction_clause(direction: str) -> str:
        normalized = str(direction or "out").strip().lower()
        if normalized == "in":
            return " REVERSELY"
        if normalized == "both":
            return " BIDIRECT"
        if normalized != "out":
            raise NebulaRepositoryError(
                "QUERY_FAILED", "direction 必须是 out、in 或 both"
            )
        return ""

    def get_neighbors(
        self,
        graph: GraphData,
        *,
        user_id: str,
        session_id: str,
        graph_version: int,
        node_name: str,
        direction: str = "out",
        limit: int = 20,
    ) -> dict[str, Any]:
        meta = self._require_synced(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key"))
        node_id = self._resolve_node(graph, node_name)
        vid = node_vid(graph_key, node_id)
        limit = max(1, min(int(limit), 200))
        clause = self._direction_clause(direction)
        rows = self._query_rows(
            f"GO FROM {_ngql_literal(vid)} OVER {RELATION_EDGE}{clause} "
            "YIELD id($$) AS neighbor_vid, properties($$).local_node_id AS local_node_id, "
            "properties($$).node_name AS node_name, properties($$).node_type AS node_type, "
            f"{RELATION_EDGE}.edge_type AS edge_type, "
            f"{RELATION_EDGE}.edge_label_json AS edge_label_json, "
            f"{RELATION_EDGE}.weight AS weight, {RELATION_EDGE}.edge_text AS edge_text "
            f"| LIMIT {limit}"
        )
        neighbors = [
            {
                "vid": _row_get(row, "neighbor_vid"),
                "local_node_id": _row_get(row, "local_node_id"),
                "node_name": _row_get(row, "node_name"),
                "node_type": _row_get(row, "node_type"),
                "edge_type": _row_get(row, "edge_type"),
                "edge_label": self._decode_json_field(_row_get(row, "edge_label_json")),
                "weight": _row_get(row, "weight"),
                "edge_text": _row_get(row, "edge_text"),
            }
            for row in rows
        ]
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": graph_version,
            "node_name": node_name,
            "direction": direction,
            "neighbors": neighbors,
            "count": len(neighbors),
            "limit": limit,
        }

    @classmethod
    def _extract_path_vids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _HEX_VID_RE.findall(value)
        if isinstance(value, dict):
            out: list[str] = []
            for item in value.values():
                out.extend(cls._extract_path_vids(item))
            return out
        if isinstance(value, (list, tuple)):
            out = []
            for item in value:
                out.extend(cls._extract_path_vids(item))
            return out
        cast = getattr(value, "cast", None)
        if callable(cast):
            try:
                cast_value = cast()
                if cast_value is not value:
                    return cls._extract_path_vids(cast_value)
            except Exception:
                pass
        nodes = getattr(value, "nodes", None)
        if callable(nodes):
            out = []
            try:
                for node in nodes():
                    for attr in ("get_id", "getId", "id"):
                        getter = getattr(node, attr, None)
                        if callable(getter):
                            out.extend(cls._extract_path_vids(_jsonable(getter())))
                            break
                if out:
                    return out
            except Exception:
                pass
        return _HEX_VID_RE.findall(str(value))

    def shortest_path(
        self,
        graph: GraphData,
        *,
        user_id: str,
        session_id: str,
        graph_version: int,
        source: str,
        target: str,
        direction: str = "out",
        max_steps: int = 10,
    ) -> dict[str, Any]:
        meta = self._require_synced(user_id, session_id, graph_version)
        graph_key = str(_row_get(meta, "graph_key"))
        source_id = self._resolve_node(graph, source)
        target_id = self._resolve_node(graph, target)
        source_vid = node_vid(graph_key, source_id)
        target_vid = node_vid(graph_key, target_id)
        max_steps = max(1, min(int(max_steps), 20))
        clause = self._direction_clause(direction)
        result = self._execute(
            f"FIND SHORTEST PATH FROM {_ngql_literal(source_vid)} TO {_ngql_literal(target_vid)} "
            f"OVER {RELATION_EDGE}{clause} UPTO {max_steps} STEPS YIELD path AS p | LIMIT 1"
        )
        raw_rows = self._result_rows(result)
        if not raw_rows:
            return {
                "success": True,
                "backend": "nebula",
                "session_id": session_id,
                "graph_version": graph_version,
                "source": source,
                "target": target,
                "found": False,
                "path": [],
                "hop_count": None,
                "weighted": False,
            }
        raw_path: Any = _row_get(raw_rows[0], "p")
        keys_getter = getattr(result, "keys", None)
        values_getter = getattr(result, "row_values", None)
        if callable(keys_getter) and callable(values_getter):
            try:
                result_keys = [str(_jsonable(key)) for key in keys_getter()]
                raw_path = values_getter(0)[result_keys.index("p")]
            except (IndexError, ValueError, TypeError):
                pass
        vids = self._extract_path_vids(raw_path)
        id2node = graph.id2node
        vid_names = {
            node_vid(graph_key, node_id): id2node.get(node_id, str(node_id))
            for node_id in range(graph.num_nodes)
        }
        names = [vid_names.get(vid, vid) for vid in vids]
        return {
            "success": True,
            "backend": "nebula",
            "session_id": session_id,
            "graph_version": graph_version,
            "source": source,
            "target": target,
            "found": True,
            "path": names,
            "hop_count": max(len(names) - 1, 0) if names else None,
            "weighted": False,
            "direction": direction,
            "max_steps": max_steps,
            "raw_path": str(raw_path),
        }
