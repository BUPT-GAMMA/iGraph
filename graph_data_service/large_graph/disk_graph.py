"""Read-only access to a partitioned snapshot without materialising the graph."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .snapshot import _normalise_node, _stable_partition


class PartitionedDiskGraph:
    """Small cursor API over node-owned SQLite adjacency partitions.

    Added for the complete large-graph algorithm path.  A process opens only
    the shard needed by the current vertex; callers receive iterators instead
    of a global node list, edge list, or adjacency dictionary.
    """

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = Path(manifest_path).resolve()
        self.root = self.manifest_path.parent
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("state") != "published":
            raise ValueError("快照尚未发布")
        if self.manifest.get("adjacency_format") != "igraph-sharded-sqlite-adjacency-v1":
            raise ValueError("快照缺少大图算法所需的分区邻接索引，请重新准备该图版本")
        self.partitions = int(self.manifest["partitions"])
        self.directed = bool(self.manifest.get("directed", True))
        self.node_count = int(self.manifest.get("node_count", 0))
        self.edge_count = int(self.manifest.get("edge_count", 0))
        self.has_weights = bool(self.manifest.get("has_weights", False))
        quality = self.manifest.get("quality", {})
        self.has_self_loops = int(quality.get("self_loops_seen", 0)) > int(
            quality.get("self_loops_skipped", 0)
        )
        self._adjacency_paths = [
            self.root / str(path) for path in self.manifest["adjacency_files"]
        ]
        self._edge_paths = [self.root / str(path) for path in self.manifest["edge_files"]]
        order_file = str(self.manifest.get("node_order_file") or "").strip()
        self._node_order_path = self.root / order_file if order_file else None
        self._connections: dict[int, sqlite3.Connection] = {}

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()

    def __enter__(self) -> "PartitionedDiskGraph":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _connection(self, node_or_partition: str | int) -> sqlite3.Connection:
        partition = (
            int(node_or_partition)
            if isinstance(node_or_partition, int)
            else _stable_partition(str(node_or_partition), self.partitions)
        )
        connection = self._connections.get(partition)
        if connection is None:
            uri = f"file:{self._adjacency_paths[partition]}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            # GROUP BY / ORDER BY operations used by undirected projections
            # may spill for a high-degree node; keep that spill file-backed.
            connection.execute("PRAGMA temp_store=FILE")
            self._connections[partition] = connection
        return connection

    def resolve_node(self, value: str | None) -> str | None:
        """Resolve either a canonical id or the upload-time display spelling."""
        raw = str(value or "").strip()
        if not raw:
            return None
        mode = str(
            self.manifest.get("quality", {}).get("policy", {}).get(
                "node_normalization", "nfkc_casefold"
            )
        )
        canonical, _ = _normalise_node(raw, mode)
        if self.has_node(canonical):
            return canonical
        # Exact-normalisation snapshots can still be addressed by display name.
        for node_id, name in self.iter_nodes():
            if name == raw:
                return node_id
        return None

    def has_node(self, node: str) -> bool:
        row = self._connection(node).execute(
            "SELECT 1 FROM nodes WHERE id=? LIMIT 1", (str(node),)
        ).fetchone()
        return row is not None

    def display_name(self, node: str) -> str:
        row = self._connection(node).execute(
            "SELECT name FROM nodes WHERE id=?", (str(node),)
        ).fetchone()
        return str(row[0]) if row else str(node)

    def iter_nodes(self) -> Iterator[tuple[str, str]]:
        if self._node_order_path is not None and self._node_order_path.is_file():
            with self._node_order_path.open("r", encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    yield str(row["id"]), str(row.get("name") or row["id"])
            return
        for partition in range(self.partitions):
            yield from self._connection(partition).execute(
                "SELECT id,name FROM nodes ORDER BY id"
            )

    def iter_node_ids(self) -> Iterator[str]:
        for node_id, _ in self.iter_nodes():
            yield str(node_id)

    def iter_edges(self) -> Iterator[tuple[str, str, float, str]]:
        # Canonical edge JSONL remains the authoritative, exactly-once stream.
        for path in self._edge_paths:
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    yield (
                        str(row["source"]),
                        str(row["target"]),
                        float(row.get("weight", 1.0)),
                        str(row.get("relation", "")),
                    )

    def iter_out_edges(
        self, node: str, *, reverse: bool = False
    ) -> Iterator[tuple[str, float]]:
        order = "DESC" if reverse else "ASC"
        if not self.directed and reverse:
            # Preserve the reverse of the historical undirected iteration
            # order (out rows followed by in rows) without materialising a
            # high-degree vertex's complete adjacency list.
            cursor = self._connection(node).execute(
                f"SELECT source,weight FROM in_edges WHERE target=? "
                f"ORDER BY source {order}",
                (str(node),),
            )
            for source, weight in cursor:
                if str(source) != str(node):
                    yield str(source), float(weight)
        cursor = self._connection(node).execute(
            f"SELECT target,weight FROM out_edges WHERE source=? "
            f"ORDER BY target {order}",
            (str(node),),
        )
        for target, weight in cursor:
            yield str(target), float(weight)
        if not self.directed and not reverse:
            cursor = self._connection(node).execute(
                f"SELECT source,weight FROM in_edges WHERE target=? "
                f"ORDER BY source {order}",
                (str(node),),
            )
            for source, weight in cursor:
                if str(source) != str(node):
                    yield str(source), float(weight)

    def iter_in_edges(self, node: str) -> Iterator[tuple[str, float]]:
        cursor = self._connection(node).execute(
            "SELECT source,weight FROM in_edges WHERE target=? ORDER BY source",
            (str(node),),
        )
        for source, weight in cursor:
            yield str(source), float(weight)
        if not self.directed:
            cursor = self._connection(node).execute(
                "SELECT target,weight FROM out_edges WHERE source=? ORDER BY target",
                (str(node),),
            )
            for target, weight in cursor:
                if str(target) != str(node):
                    yield str(target), float(weight)

    def iter_neighbors(
        self, node: str, *, direction: str = "out", undirected: bool = False
    ) -> Iterator[tuple[str, float]]:
        if undirected or direction == "both":
            # Opposite directed edges can expose the same neighbor twice.
            # Let SQLite's disk-backed GROUP BY deduplicate the current
            # vertex.  A Python dict here could still exhaust memory for a
            # single billion-degree hub even though the full graph is external.
            cursor = self._connection(node).execute(
                """
                SELECT neighbor,MIN(weight) FROM (
                    SELECT target AS neighbor,weight
                    FROM out_edges WHERE source=?
                    UNION ALL
                    SELECT source AS neighbor,weight
                    FROM in_edges WHERE target=?
                )
                GROUP BY neighbor ORDER BY neighbor
                """,
                (str(node), str(node)),
            )
            for neighbor, weight in cursor:
                yield str(neighbor), float(weight)
            return
        if direction == "in":
            yield from self.iter_in_edges(node)
        else:
            yield from self.iter_out_edges(node)

    def edge_weight(self, source: str, target: str, *, undirected: bool = False) -> float | None:
        row = self._connection(source).execute(
            "SELECT weight FROM out_edges WHERE source=? AND target=? LIMIT 1",
            (str(source), str(target)),
        ).fetchone()
        if row is not None:
            return float(row[0])
        if undirected:
            row = self._connection(target).execute(
                "SELECT weight FROM out_edges WHERE source=? AND target=? LIMIT 1",
                (str(target), str(source)),
            ).fetchone()
            if row is not None:
                return float(row[0])
        return None

    def degree(self, node: str, *, direction: str = "total") -> int:
        connection = self._connection(node)
        out_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM out_edges WHERE source=?", (str(node),)
            ).fetchone()[0]
        )
        in_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM in_edges WHERE target=?", (str(node),)
            ).fetchone()[0]
        )
        if not self.directed:
            return out_count + in_count
        if direction == "out":
            return out_count
        if direction == "in":
            return in_count
        return out_count + in_count
