"""Streaming preparation, validation and atomic partitioned snapshots."""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import os
import re
import shutil
import sqlite3
import unicodedata
import uuid
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .contracts import DataQualityPolicy
from .durability import durable_publish_tree, remove_tree_and_fsync
from .integrity import measure_snapshot_storage
from .processes import process_context


ProgressCallback = Callable[[str, float, str, dict[str, Any]], None]


def _configured_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Read an integer setting without letting a malformed env break a job."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


class _AppendHandlePool:
    """Small LRU pool for hash-shuffle outputs.

    A mapper can touch every edge and node partition. Keeping all of those
    files open would require twice the partition count in descriptors per
    process, so reopen evicted files in append mode instead.
    """

    def __init__(self, root: Path, max_open_files: int) -> None:
        self._root = root
        self._max_open_files = max(2, int(max_open_files))
        self._handles: OrderedDict[tuple[str, int], Any] = OrderedDict()

    def get(self, kind: str, partition: int):
        key = (kind, int(partition))
        opened = self._handles.pop(key, None)
        if opened is not None:
            self._handles[key] = opened
            return opened
        if len(self._handles) >= self._max_open_files:
            _, evicted = self._handles.popitem(last=False)
            evicted.close()
        opened = (self._root / f"{kind}-{partition:05d}.jsonl").open(
            "a", encoding="utf-8"
        )
        self._handles[key] = opened
        return opened

    def close(self) -> None:
        while self._handles:
            _, opened = self._handles.popitem(last=False)
            opened.close()


def _stable_partition(value: str, partitions: int) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % partitions


def _normalise_node(value: str, mode: str) -> tuple[str, str]:
    display = str(value or "").strip()
    if mode == "exact":
        return display, display
    folded = unicodedata.normalize("NFKC", display)
    folded = re.sub(r"\s+", " ", folded).strip()
    return folded.casefold(), display


def _split_row(line: str, delimiter: str, input_format: str) -> list[str]:
    if delimiter == "whitespace":
        parts = line.split()
    else:
        actual = {"comma": ",", "tab": "\t"}.get(delimiter, delimiter)
        parts = next(csv.reader([line], delimiter=actual))
        parts = [part.strip() for part in parts]
    if input_format == "triples" and len(parts) >= 3:
        # Canonical internal order is source,target,weight,relation.
        return [parts[0], parts[2], "1", parts[1]]
    return parts


def _parse_edge_line(
    line: str,
    *,
    delimiter: str,
    input_format: str,
    directed: bool,
    policy: dict[str, Any],
    max_node_id_bytes: int,
) -> tuple[dict[str, Any] | None, str | None]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, None
    try:
        parts = _split_row(stripped, delimiter, input_format)
    except (csv.Error, StopIteration) as exc:
        return None, f"字段解析失败: {exc}"
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None, "缺少 source/target"
    if parts[0].strip().lower() in {"source", "src", "from"} and parts[1].strip().lower() in {
        "target", "dst", "to"
    }:
        return None, None

    source_key, source_name = _normalise_node(parts[0], policy["node_normalization"])
    target_key, target_name = _normalise_node(parts[1], policy["node_normalization"])
    if not source_key or not target_key:
        return None, "节点标识规范化后为空"
    if any(
        len(value.encode("utf-8")) > max_node_id_bytes
        for value in (source_key, source_name, target_key, target_name)
    ):
        return None, f"节点标识超过 {max_node_id_bytes} 字节"
    try:
        weight = float(parts[2]) if len(parts) >= 3 and parts[2] != "" else 1.0
    except (TypeError, ValueError):
        return None, "weight 不是有限数值"
    if not math.isfinite(weight):
        return None, "weight 不是有限数值"
    relation = str(parts[3]).strip() if len(parts) >= 4 else ""

    # Preserve first-appearance node order independently from canonicalising
    # an undirected edge's endpoint orientation.
    node_mentions = (
        (source_key, source_name, 0),
        (target_key, target_name, 1),
    )
    if not directed and target_key < source_key:
        source_key, target_key = target_key, source_key
        source_name, target_name = target_name, source_name
    key_parts = [source_key, target_key]
    if policy["edge_key"] == "endpoints_relation":
        key_parts.append(relation)
    # A separator-joined key is ambiguous when a valid node/relation itself
    # contains that separator. JSON array encoding is canonical here and keeps
    # logically distinct edges distinct for sorting and deduplication.
    edge_key = json.dumps(key_parts, ensure_ascii=False, separators=(",", ":"))
    return {
        "key": edge_key,
        "source": source_key,
        "target": target_key,
        "source_name": source_name,
        "target_name": target_name,
        "weight": weight,
        "relation": relation,
        "explicit_weight": input_format != "triples" and len(parts) >= 3 and parts[2] != "",
        "self_loop": source_key == target_key,
        "node_mentions": node_mentions,
    }, None


def _map_source_range(args: tuple[Any, ...]) -> dict[str, Any]:
    """Read one byte range and hash-shuffle records without global lists."""
    (
        source_path, start, end, is_last, map_dir, map_index, partitions,
        delimiter, input_format, directed, policy, max_record_bytes,
        max_node_id_bytes, max_open_files,
    ) = args
    max_record_bytes = max(1024, int(max_record_bytes))
    map_root = Path(map_dir) / f"map-{map_index:05d}"
    map_root.mkdir(parents=True, exist_ok=True)
    output_handles = _AppendHandlePool(map_root, max_open_files)
    counters = {
        "raw_records": 0,
        "valid_records": 0,
        "invalid_records": 0,
        "self_loops_seen": 0,
        "self_loops_skipped": 0,
        "explicit_weight_records": 0,
    }
    content_hash = hashlib.sha256()

    def handle(kind: str, partition: int):
        return output_handles.get(kind, partition)

    try:
        with Path(source_path).open("rb") as source:
            if start:
                source.seek(start - 1)
                if source.read(1) != b"\n":
                    # Skip a partial record in bounded chunks.  A malformed
                    # multi-gigabyte line must not be materialised merely
                    # because a worker range starts in its middle.
                    while True:
                        skipped = source.readline(max_record_bytes + 1)
                        if not skipped or skipped.endswith(b"\n"):
                            break
            else:
                source.seek(0)
            while True:
                position = source.tell()
                if not is_last and position >= end:
                    break
                raw = source.readline(max_record_bytes + 1)
                if not raw:
                    break
                content_hash.update(raw)
                if len(raw) > max_record_bytes:
                    # Drain the remainder without retaining it. Other range
                    # workers independently skip the same partial line.
                    while not raw.endswith(b"\n"):
                        raw = source.readline(max_record_bytes + 1)
                        if not raw:
                            break
                        content_hash.update(raw)
                    counters["raw_records"] += 1
                    counters["invalid_records"] += 1
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    counters["raw_records"] += 1
                    counters["invalid_records"] += 1
                    continue
                if line.strip() and not line.lstrip().startswith("#"):
                    counters["raw_records"] += 1
                record, error = _parse_edge_line(
                    line,
                    delimiter=delimiter,
                    input_format=input_format,
                    directed=directed,
                    policy=policy,
                    max_node_id_bytes=max_node_id_bytes,
                )
                if error:
                    counters["invalid_records"] += 1
                    continue
                if record is None:
                    continue
                # Byte offset provides a deterministic global record order
                # across independently processed source chunks.
                record["ordinal"] = position
                if record["self_loop"]:
                    counters["self_loops_seen"] += 1
                    if policy["self_loops"] == "skip":
                        counters["self_loops_skipped"] += 1
                        continue
                counters["valid_records"] += 1
                counters["explicit_weight_records"] += int(record["explicit_weight"])
                edge_partition = _stable_partition(record["key"], partitions)
                handle("edge", edge_partition).write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                for node_key, node_name, mention_index in record["node_mentions"]:
                    node_partition = _stable_partition(node_key, partitions)
                    handle("node", node_partition).write(
                        json.dumps(
                            {
                                "key": node_key,
                                "name": node_name,
                                "ordinal": position * 2 + int(mention_index),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
    finally:
        output_handles.close()
    return {
        "map_index": int(map_index),
        "map_root": str(map_root),
        "content_digest": content_hash.hexdigest(),
        **counters,
    }


def _stream_sha256(path: Path, *, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    """Return a worker-independent fingerprint for the complete source file.

    Added for version traceability: hashing per-worker chunks and then hashing
    those digests makes the fingerprint depend on the chosen worker count.
    A single bounded-memory streaming pass gives the same identity regardless
    of partitioning or process scheduling.
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _write_sorted_runs(
    input_paths: Iterable[Path],
    run_dir: Path,
    *,
    chunk_rows: int,
    chunk_bytes: int,
) -> list[Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    runs: list[Path] = []
    chunk: list[tuple[str, dict[str, Any]]] = []
    chunk_size = 0

    def flush() -> None:
        nonlocal chunk_size
        if not chunk:
            return
        chunk.sort(key=lambda item: item[0])
        path = run_dir / f"run-{len(runs):06d}.jsonl"
        with path.open("w", encoding="utf-8") as output:
            for key, row in chunk:
                output.write(json.dumps([key, row], ensure_ascii=False, separators=(",", ":")) + "\n")
        runs.append(path)
        chunk.clear()
        chunk_size = 0

    for input_path in input_paths:
        if not input_path.exists():
            continue
        with input_path.open("r", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                row_size = len(line.encode("utf-8")) + 256
                if chunk and chunk_size + row_size > chunk_bytes:
                    flush()
                chunk.append((str(row["key"]), row))
                # Account for encoded data plus a conservative per-object
                # allowance; both a row and byte ceiling bound reducer RSS.
                chunk_size += row_size
                if len(chunk) >= chunk_rows or chunk_size >= chunk_bytes:
                    flush()
    flush()
    return runs


def _merge_fan_in() -> int:
    """Maximum sorted runs opened by one merge pass.

    A single ``heapq.merge(*runs)`` keeps every input file open.  Large
    snapshots can create thousands of runs, so cap the fan-in and compact in
    multiple passes before exposing the final iterator.
    """
    return _configured_int(
        "LARGE_GRAPH_MERGE_FAN_IN", 64, minimum=2, maximum=256
    )


def _iter_json_run(path: Path) -> Iterator[Any]:
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def _bounded_json_merge(
    runs: Iterable[Path],
    merge_root: Path,
    *,
    key: Callable[[Any], Any],
    prefix: str,
) -> Iterator[Any]:
    """Merge sorted JSONL runs with a fixed file-descriptor fan-in.

    Completed input groups are removed only after their replacement run has
    been fully written.  An interrupted preparation therefore remains safely
    disposable as staging data, while a successful pass uses at most
    ``LARGE_GRAPH_MERGE_FAN_IN`` simultaneously open inputs.
    """
    fan_in = _merge_fan_in()
    current = [Path(path) for path in runs]
    pass_index = 0
    merge_root.mkdir(parents=True, exist_ok=True)
    while len(current) > fan_in:
        compacted: list[Path] = []
        for group_index, start in enumerate(range(0, len(current), fan_in)):
            group = current[start : start + fan_in]
            if len(group) == 1:
                compacted.append(group[0])
                continue
            output_path = merge_root / (
                f"{prefix}-merge-{pass_index:03d}-{group_index:06d}.jsonl"
            )
            iterators = [_iter_json_run(path) for path in group]
            with output_path.open("w", encoding="utf-8") as output:
                for row in heapq.merge(*iterators, key=key):
                    output.write(
                        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
            for path in group:
                path.unlink(missing_ok=True)
            compacted.append(output_path)
        current = compacted
        pass_index += 1
    if not current:
        return iter(())
    return heapq.merge(*(_iter_json_run(path) for path in current), key=key)


def _choose_edge(
    current: dict[str, Any], incoming: dict[str, Any], conflict_policy: str
) -> dict[str, Any]:
    current_first = int(current.get("ordinal", 0)) <= int(incoming.get("ordinal", 0))
    if float(current["weight"]) == float(incoming["weight"]):
        return current if current_first else incoming
    if conflict_policy == "reject":
        raise ValueError(f"重复边 {current['key']!r} 的 weight 冲突")
    if conflict_policy == "last":
        return incoming if current_first else current
    if conflict_policy == "max":
        if float(incoming["weight"]) > float(current["weight"]):
            return incoming
        return current
    if conflict_policy == "min":
        if float(incoming["weight"]) < float(current["weight"]):
            return incoming
        return current
    return current if current_first else incoming


def _reduce_edge_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    (
        partition, input_paths_raw, output_path_raw, run_root_raw,
        policy, chunk_rows, chunk_bytes,
    ) = args
    output_path = Path(output_path_raw)
    runs = _write_sorted_runs(
        (Path(path) for path in input_paths_raw),
        Path(run_root_raw) / f"edge-{partition:05d}",
        chunk_rows=chunk_rows,
        chunk_bytes=chunk_bytes,
    )
    unique = duplicates = 0
    current_key: str | None = None
    current: dict[str, Any] | None = None
    merged = _bounded_json_merge(
        runs,
        Path(run_root_raw) / f"edge-{partition:05d}",
        key=lambda item: str(item[0]),
        prefix="edge",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for key, row in merged:
            if current_key is None:
                current_key, current = key, row
                continue
            if key == current_key:
                duplicates += 1
                current = _choose_edge(current, row, policy["weight_conflict"])
                continue
            assert current is not None
            output.write(
                json.dumps(
                    {field: current[field] for field in ("source", "target", "weight", "relation")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            unique += 1
            current_key, current = key, row
        if current is not None:
            output.write(
                json.dumps(
                    {field: current[field] for field in ("source", "target", "weight", "relation")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            unique += 1
    return {"partition": partition, "unique_edges": unique, "duplicate_edges": duplicates}


def _reduce_node_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    (
        partition, input_paths_raw, output_path_raw, run_root_raw,
        chunk_rows, chunk_bytes,
    ) = args
    output_path = Path(output_path_raw)
    runs = _write_sorted_runs(
        (Path(path) for path in input_paths_raw),
        Path(run_root_raw) / f"node-{partition:05d}",
        chunk_rows=chunk_rows,
        chunk_bytes=chunk_bytes,
    )
    unique = duplicate_mentions = 0
    current_key: str | None = None
    current_name = ""
    current_ordinal = 0
    merged = _bounded_json_merge(
        runs,
        Path(run_root_raw) / f"node-{partition:05d}",
        key=lambda item: str(item[0]),
        prefix="node",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for key, row in merged:
            name = str(row.get("name") or key)
            ordinal = int(row.get("ordinal", 0))
            if key == current_key:
                duplicate_mentions += 1
                if ordinal < current_ordinal:
                    current_name = name
                    current_ordinal = ordinal
                continue
            if current_key is not None:
                output.write(
                    json.dumps(
                        {
                            "id": current_key,
                            "name": current_name,
                            "ordinal": current_ordinal,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                unique += 1
            current_key, current_name, current_ordinal = key, name, ordinal
        if current_key is not None:
            output.write(
                json.dumps(
                    {
                        "id": current_key,
                        "name": current_name,
                        "ordinal": current_ordinal,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            unique += 1
    return {"partition": partition, "unique_nodes": unique, "duplicate_node_mentions": duplicate_mentions}


def _write_node_order(
    node_paths: Iterable[Path],
    output_path: Path,
    run_root: Path,
    *,
    chunk_rows: int,
    chunk_bytes: int,
) -> int:
    """External-sort unique node rows by their first source-file appearance."""
    run_root.mkdir(parents=True, exist_ok=True)
    runs: list[Path] = []
    chunk: list[tuple[int, str, str]] = []
    chunk_size = 0

    def flush() -> None:
        nonlocal chunk_size
        if not chunk:
            return
        chunk.sort(key=lambda row: (row[0], row[1]))
        run = run_root / f"node-order-{len(runs):06d}.jsonl"
        with run.open("w", encoding="utf-8") as target:
            for row in chunk:
                target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        runs.append(run)
        chunk.clear()
        chunk_size = 0

    for path in node_paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                row_size = len(line.encode("utf-8")) + 192
                if chunk and chunk_size + row_size > chunk_bytes:
                    flush()
                chunk.append(
                    (
                        int(row.get("ordinal", 0)),
                        str(row["id"]),
                        str(row.get("name") or row["id"]),
                    )
                )
                chunk_size += row_size
                if len(chunk) >= chunk_rows or chunk_size >= chunk_bytes:
                    flush()
    flush()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    merged = _bounded_json_merge(
        runs,
        run_root,
        key=lambda row: (int(row[0]), str(row[1])),
        prefix="node-order",
    )
    with output_path.open("w", encoding="utf-8") as output:
        for ordinal, node_id, name in merged:
            output.write(
                json.dumps(
                    {"id": node_id, "name": name, "ordinal": ordinal},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            count += 1
    return count


# ── 大图邻接索引（新增）────────────────────────────────────────────
#
# 规范化 edge 分区按“边唯一键”归属，适合去重，却不适合遍历。这里再做
# 一次 source/target shuffle，为每个节点归属分区建立独立 SQLite 邻接分片。
# 算法进程只打开目标节点所在的分片，不会在 Python 中构造全图邻接表。
def _map_adjacency_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    edge_path_raw, map_root_raw, map_index, partitions, max_open_files = args
    map_root = Path(map_root_raw) / f"map-{int(map_index):05d}"
    map_root.mkdir(parents=True, exist_ok=True)
    output_handles = _AppendHandlePool(map_root, max_open_files)
    edge_count = 0

    def handle(kind: str, partition: int):
        return output_handles.get(kind, partition)

    try:
        with Path(edge_path_raw).open("r", encoding="utf-8") as source:
            for line in source:
                edge = json.loads(line)
                src, dst = str(edge["source"]), str(edge["target"])
                compact = {
                    "source": src,
                    "target": dst,
                    "weight": float(edge.get("weight", 1.0)),
                    "relation": str(edge.get("relation", "")),
                }
                encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n"
                handle("out", _stable_partition(src, int(partitions))).write(encoded)
                handle("in", _stable_partition(dst, int(partitions))).write(encoded)
                edge_count += 1
    finally:
        output_handles.close()
    return {"map_root": str(map_root), "edge_count": edge_count}


def _build_adjacency_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    partition, node_path_raw, map_roots_raw, output_path_raw = args
    output_path = Path(output_path_raw)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_path)
    # 新建快照时数据库尚不可见，关闭日志同步可显著减少索引构建 I/O；
    # 发布前连接会完整关闭，快照发布仍由目录原子 rename 保证。
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        CREATE TABLE nodes(id TEXT PRIMARY KEY, name TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE out_edges(
            source TEXT NOT NULL, target TEXT NOT NULL,
            weight REAL NOT NULL, relation TEXT NOT NULL
        );
        CREATE TABLE in_edges(
            target TEXT NOT NULL, source TEXT NOT NULL,
            weight REAL NOT NULL, relation TEXT NOT NULL
        );
        """
    )
    node_rows = out_rows = in_rows = 0
    try:
        node_path = Path(node_path_raw)
        if node_path.exists():
            batch: list[tuple[str, str]] = []
            with node_path.open("r", encoding="utf-8") as nodes:
                for line in nodes:
                    row = json.loads(line)
                    batch.append((str(row["id"]), str(row.get("name") or row["id"])))
                    if len(batch) >= 10_000:
                        connection.executemany("INSERT INTO nodes(id,name) VALUES (?,?)", batch)
                        node_rows += len(batch)
                        batch.clear()
            if batch:
                connection.executemany("INSERT INTO nodes(id,name) VALUES (?,?)", batch)
                node_rows += len(batch)

        for kind, sql in (
            ("out", "INSERT INTO out_edges(source,target,weight,relation) VALUES (?,?,?,?)"),
            ("in", "INSERT INTO in_edges(target,source,weight,relation) VALUES (?,?,?,?)"),
        ):
            batch4: list[tuple[str, str, float, str]] = []
            for root_raw in map_roots_raw:
                path = Path(root_raw) / f"{kind}-{int(partition):05d}.jsonl"
                if not path.exists():
                    continue
                with path.open("r", encoding="utf-8") as source:
                    for line in source:
                        row = json.loads(line)
                        if kind == "out":
                            batch4.append((str(row["source"]), str(row["target"]), float(row["weight"]), str(row.get("relation", ""))))
                        else:
                            batch4.append((str(row["target"]), str(row["source"]), float(row["weight"]), str(row.get("relation", ""))))
                        if len(batch4) >= 10_000:
                            connection.executemany(sql, batch4)
                            if kind == "out":
                                out_rows += len(batch4)
                            else:
                                in_rows += len(batch4)
                            batch4.clear()
            if batch4:
                connection.executemany(sql, batch4)
                if kind == "out":
                    out_rows += len(batch4)
                else:
                    in_rows += len(batch4)
        connection.executescript(
            """
            CREATE INDEX out_edges_source_idx ON out_edges(source, target);
            CREATE INDEX in_edges_target_idx ON in_edges(target, source);
            ANALYZE;
            """
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "partition": int(partition),
        "nodes": node_rows,
        "out_edges": out_rows,
        "in_edges": in_rows,
    }


class DerivedSnapshotBuilder:
    """Build an immutable subgraph snapshot from a streamed selection artifact.

    The selection is indexed in a temporary SQLite database.  Canonical nodes
    and edges are then copied from the already-normalised source snapshot, so
    no ``GraphData``/NetworkX object or graph-sized Python container is ever
    created.  Scanning ``node-order.jsonl`` independently preserves isolated
    selected nodes and their original ordering.
    """

    SNAPSHOT_FORMAT = "igraph-partitioned-jsonl-v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def build(
        self,
        *,
        source_manifest_path: str | Path,
        selection_files: Iterable[str | Path],
        user_scope: str,
        graph_id: str,
        graph_version: int,
        workers: int,
        method: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        source_path = Path(source_manifest_path).resolve(strict=True)
        source_root = source_path.parent
        source_manifest = json.loads(source_path.read_text(encoding="utf-8"))
        method = str(method).strip().lower()
        derived_directed = bool(source_manifest["directed"]) and method != "k_truss"
        partitions = max(1, min(int(source_manifest["partitions"]), 128))
        workers = max(1, min(int(workers), partitions, os.cpu_count() or 1))
        max_open_files = _configured_int(
            "LARGE_GRAPH_MAP_OPEN_FILES", 64, minimum=4, maximum=128
        )
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        graph_root = self.root / user_scope / graph_id / f"v{int(graph_version):08d}"
        staging = graph_root / f".{snapshot_id}.staging"
        final = graph_root / snapshot_id
        nodes_root = staging / "nodes"
        edges_root = staging / "edges"
        adjacency_root = staging / "adjacency"
        adjacency_map_root = staging / "adjacency-map"
        selection_db = staging / "selection.sqlite3"
        staging.mkdir(parents=True, exist_ok=False)

        def emit(stage: str, value: float, message: str, detail: dict[str, Any] | None = None) -> None:
            if progress is not None:
                progress(stage, value, message, detail or {})

        connection: sqlite3.Connection | None = None
        try:
            emit("derived_selection", 0.72, "索引子图筛选结果", {})
            connection = sqlite3.connect(selection_db)
            connection.executescript(
                """
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA temp_store=FILE;
                CREATE TABLE selected_nodes(id TEXT PRIMARY KEY) WITHOUT ROWID;
                CREATE TABLE selected_edges(
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    weight REAL NOT NULL,
                    PRIMARY KEY(source,target)
                ) WITHOUT ROWID;
                """
            )
            node_batch: list[tuple[str]] = []
            edge_batch: list[tuple[str, str, float]] = []

            def flush_selection() -> None:
                if node_batch:
                    connection.executemany(
                        "INSERT OR IGNORE INTO selected_nodes(id) VALUES (?)",
                        node_batch,
                    )
                    node_batch.clear()
                if edge_batch:
                    connection.executemany(
                        """INSERT OR IGNORE INTO selected_edges(source,target,weight)
                           VALUES (?,?,?)""",
                        edge_batch,
                    )
                    edge_batch.clear()

            for raw_path in selection_files:
                with Path(raw_path).open("r", encoding="utf-8") as selected:
                    for line in selected:
                        row = json.loads(line)
                        if row.get("kind") == "node":
                            node_batch.append((str(row["node_id"]),))
                        elif row.get("kind") == "edge":
                            source = row.get("source_id")
                            target = row.get("target_id")
                            if source is None or target is None:
                                raise ValueError("子图 Artifact 缺少规范边端点 ID")
                            edge_batch.append(
                                (str(source), str(target), float(row.get("weight", 1.0)))
                            )
                        if len(node_batch) + len(edge_batch) >= 10_000:
                            flush_selection()
            flush_selection()
            connection.commit()
            selected_node_count = int(
                connection.execute("SELECT COUNT(*) FROM selected_nodes").fetchone()[0]
            )
            selected_edge_count = int(
                connection.execute("SELECT COUNT(*) FROM selected_edges").fetchone()[0]
            )
            selection_cache_rows = _configured_int(
                "LARGE_GRAPH_DERIVED_SELECTION_CACHE_ROWS",
                100_000,
                minimum=0,
                maximum=1_000_000,
            )
            selected_node_ids: set[str] | None = None
            selected_edge_ids: set[tuple[str, str]] | None = None
            if selected_node_count <= selection_cache_rows:
                selected_node_ids = {
                    str(row[0])
                    for row in connection.execute("SELECT id FROM selected_nodes")
                }
            if selected_edge_count <= selection_cache_rows:
                selected_edge_ids = {
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT source,target FROM selected_edges"
                    )
                }

            def node_is_selected(node: str) -> bool:
                if selected_node_ids is not None:
                    return str(node) in selected_node_ids
                return connection.execute(
                    "SELECT 1 FROM selected_nodes WHERE id=?", (str(node),)
                ).fetchone() is not None

            def edge_is_selected(source: str, target: str) -> bool:
                if selected_edge_ids is not None:
                    return (str(source), str(target)) in selected_edge_ids
                return connection.execute(
                    "SELECT 1 FROM selected_edges WHERE source=? AND target=?",
                    (str(source), str(target)),
                ).fetchone() is not None

            nodes_root.mkdir(parents=True, exist_ok=True)
            edges_root.mkdir(parents=True, exist_ok=True)
            copied_nodes = 0
            for partition, relative in enumerate(source_manifest["node_files"]):
                output_path = nodes_root / f"part-{partition:05d}.jsonl"
                with (source_root / relative).open("r", encoding="utf-8") as source, output_path.open(
                    "w", encoding="utf-8"
                ) as output:
                    for line in source:
                        row = json.loads(line)
                        if node_is_selected(str(row["id"])):
                            output.write(line)
                            copied_nodes += 1
            if copied_nodes != selected_node_count:
                raise ValueError("子图筛选节点与源快照不一致")

            node_order_path = staging / "node-order.jsonl"
            ordered_nodes = 0
            with (source_root / source_manifest["node_order_file"]).open(
                "r", encoding="utf-8"
            ) as source, node_order_path.open("w", encoding="utf-8") as output:
                for line in source:
                    row = json.loads(line)
                    if node_is_selected(str(row["id"])):
                        output.write(line)
                        ordered_nodes += 1
            if ordered_nodes != selected_node_count:
                raise ValueError("派生快照节点顺序与筛选节点不一致")

            copied_edges = 0
            if method == "k_truss":
                # Materialise empty canonical shards first, then bound the
                # append descriptors exactly like preparation hash-shuffles.
                # A 128-partition derived snapshot must not require 128 edge
                # handles in the worker process.
                for partition in range(partitions):
                    (edges_root / f"part-{partition:05d}.jsonl").touch()
                handles = _AppendHandlePool(edges_root, max_open_files)
                try:
                    for source, target, weight in connection.execute(
                        """SELECT source,target,weight FROM selected_edges
                           ORDER BY source,target"""
                    ):
                        source, target = sorted((str(source), str(target)))
                        edge_key = json.dumps(
                            [source, target], ensure_ascii=False, separators=(",", ":")
                        )
                        partition = _stable_partition(edge_key, partitions)
                        handles.get("part", partition).write(
                            json.dumps(
                                {
                                    "source": source, "target": target,
                                    "weight": float(weight), "relation": "",
                                },
                                ensure_ascii=False, separators=(",", ":"),
                            )
                            + "\n"
                        )
                        copied_edges += 1
                finally:
                    handles.close()
            else:
                for partition, relative in enumerate(source_manifest["edge_files"]):
                    output_path = edges_root / f"part-{partition:05d}.jsonl"
                    with (source_root / relative).open("r", encoding="utf-8") as source, output_path.open(
                        "w", encoding="utf-8"
                    ) as output:
                        for line in source:
                            row = json.loads(line)
                            if edge_is_selected(
                                str(row["source"]), str(row["target"])
                            ):
                                output.write(line)
                                copied_edges += 1
            if copied_edges != selected_edge_count:
                raise ValueError("子图筛选边与源快照不一致")
            connection.close()
            connection = None
            selection_db.unlink(missing_ok=True)

            emit(
                "derived_indexing", 0.82, "构建派生快照邻接索引",
                {"nodes": copied_nodes, "edges": copied_edges},
            )
            canonical_edges = [
                edges_root / f"part-{index:05d}.jsonl" for index in range(partitions)
            ]
            adjacency_maps: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(
                        _map_adjacency_partition,
                        (str(path), str(adjacency_map_root), index, partitions, max_open_files),
                    )
                    for index, path in enumerate(canonical_edges)
                ]
                for future in as_completed(futures):
                    adjacency_maps.append(future.result())
            map_dirs = [row["map_root"] for row in adjacency_maps]
            adjacency_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(
                        _build_adjacency_partition,
                        (
                            partition,
                            str(nodes_root / f"part-{partition:05d}.jsonl"),
                            map_dirs,
                            str(adjacency_root / f"part-{partition:05d}.sqlite3"),
                        ),
                    )
                    for partition in range(partitions)
                ]
                for future in as_completed(futures):
                    adjacency_results.append(future.result())
            if sum(int(row["nodes"]) for row in adjacency_results) != copied_nodes:
                raise ValueError("派生快照邻接节点数不一致")
            if any(
                sum(int(row[field]) for row in adjacency_results) != copied_edges
                for field in ("out_edges", "in_edges")
            ):
                raise ValueError("派生快照邻接边数不一致")

            fingerprint = hashlib.sha256()
            for path in [
                *(nodes_root / f"part-{i:05d}.jsonl" for i in range(partitions)),
                *(edges_root / f"part-{i:05d}.jsonl" for i in range(partitions)),
            ]:
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        fingerprint.update(block)
            source_quality = dict(source_manifest.get("quality") or {})
            quality = {
                "node_count": copied_nodes,
                "edge_count": copied_edges,
                "policy_id": source_quality.get("policy_id"),
                "policy": dict(source_quality.get("policy") or {}),
                "source_content_fingerprint": fingerprint.hexdigest(),
                "derived": True,
                "source_snapshot_id": source_manifest["snapshot_id"],
                "source_graph_version": int(source_manifest["graph_version"]),
                "map_open_files": max_open_files,
            }
            node_files = [f"nodes/part-{i:05d}.jsonl" for i in range(partitions)]
            edge_files = [f"edges/part-{i:05d}.jsonl" for i in range(partitions)]
            adjacency_files = [
                f"adjacency/part-{i:05d}.sqlite3" for i in range(partitions)
            ]
            storage_bytes = measure_snapshot_storage(
                staging,
                node_files=node_files,
                edge_files=edge_files,
                adjacency_files=adjacency_files,
                node_order_file="node-order.jsonl",
            )
            manifest = {
                "format": self.SNAPSHOT_FORMAT,
                "snapshot_id": snapshot_id,
                "graph_id": graph_id,
                "graph_version": int(graph_version),
                "state": "published",
                "directed": derived_directed,
                "has_weights": bool(source_manifest.get("has_weights")),
                "node_count": copied_nodes,
                "edge_count": copied_edges,
                "partitions": partitions,
                "node_files": node_files,
                "node_order_file": "node-order.jsonl",
                "edge_files": edge_files,
                "adjacency_format": "igraph-sharded-sqlite-adjacency-v1",
                "adjacency_files": adjacency_files,
                "storage_bytes": storage_bytes,
                "quality": quality,
                "source_snapshot_id": source_manifest["snapshot_id"],
                "source_graph_version": int(source_manifest["graph_version"]),
            }
            (staging / "manifest.json.tmp").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (staging / "manifest.json.tmp").replace(staging / "manifest.json")
            remove_tree_and_fsync(adjacency_map_root)
            graph_root.mkdir(parents=True, exist_ok=True)
            durable_publish_tree(
                staging,
                final,
                durability_root=self.root.parent,
            )
            emit("derived_publishing", 0.91, "派生快照文件已原子发布", {"snapshot_id": snapshot_id})
            return {
                **manifest,
                "manifest_path": str(final / "manifest.json"),
                "summary": {
                    "snapshot_id": snapshot_id,
                    "graph_id": graph_id,
                    "graph_version": int(graph_version),
                    "node_count": copied_nodes,
                    "edge_count": copied_edges,
                    "partitions": partitions,
                    "directed": derived_directed,
                    "has_weights": bool(source_manifest.get("has_weights")),
                    "storage_bytes": storage_bytes,
                    "quality": quality,
                },
            }
        except BaseException:
            if connection is not None:
                connection.close()
            try:
                remove_tree_and_fsync(staging)
            except (OSError, ValueError):
                # Preserve the build failure; stale private staging trees are
                # reclaimed by the lease/orphan cleanup pass.
                pass
            raise


class PartitionedSnapshotBuilder:
    """Build an immutable snapshot without constructing a complete graph."""

    SNAPSHOT_FORMAT = "igraph-partitioned-jsonl-v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def build(
        self,
        *,
        source_path: str | Path,
        user_scope: str,
        graph_id: str,
        graph_version: int,
        directed: bool,
        partitions: int,
        workers: int,
        delimiter: str,
        input_format: str,
        policy: DataQualityPolicy,
        expected_source: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        source = Path(source_path).resolve()
        stat = source.stat()
        # Detect replacement between API admission and Worker execution.
        if int(expected_source.get("size", stat.st_size)) != stat.st_size:
            raise ValueError("上传文件在任务提交后发生变化，已拒绝构建快照")
        if int(expected_source.get("mtime_ns", stat.st_mtime_ns)) != stat.st_mtime_ns:
            raise ValueError("上传文件在任务提交后发生变化，已拒绝构建快照")
        partitions = max(1, min(int(partitions), 128))
        workers = max(1, min(int(workers), partitions, os.cpu_count() or 1))
        chunk_rows = _configured_int(
            "LARGE_GRAPH_SORT_CHUNK_ROWS", 200_000, minimum=10_000
        )
        chunk_bytes = _configured_int(
            "LARGE_GRAPH_SORT_CHUNK_BYTES", 128 * 1024 * 1024,
            minimum=1024 * 1024,
        )
        max_record_bytes = _configured_int(
            "LARGE_GRAPH_MAX_RECORD_BYTES", 1024 * 1024, minimum=1024
        )
        max_node_id_bytes = _configured_int(
            "LARGE_GRAPH_MAX_NODE_ID_BYTES", 4096, minimum=16
        )
        max_open_files = _configured_int(
            "LARGE_GRAPH_MAP_OPEN_FILES", 64, minimum=4, maximum=128
        )
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        graph_root = self.root / user_scope / graph_id / f"v{int(graph_version):08d}"
        staging = graph_root / f".{snapshot_id}.staging"
        final = graph_root / snapshot_id
        staging.mkdir(parents=True, exist_ok=False)
        map_root = staging / "map"
        run_root = staging / "runs"
        nodes_root = staging / "nodes"
        edges_root = staging / "edges"
        adjacency_root = staging / "adjacency"
        adjacency_map_root = staging / "adjacency-map"

        def emit(stage: str, value: float, message: str, detail: dict[str, Any] | None = None) -> None:
            if progress is not None:
                progress(stage, value, message, detail or {})

        try:
            emit("preparing", 0.05, "开始并行分块读取", {"source_bytes": stat.st_size})
            ranges: list[tuple[int, int, bool]] = []
            chunk_count = max(1, workers * 2)
            span = max(1, math.ceil(stat.st_size / chunk_count))
            for index in range(chunk_count):
                start = index * span
                if start >= stat.st_size and index > 0:
                    break
                end = min(stat.st_size, (index + 1) * span)
                ranges.append((start, end, end >= stat.st_size))
            map_args = [
                (
                    str(source), start, end, is_last, str(map_root), index, partitions,
                    delimiter, input_format, directed, policy.to_dict(),
                    max_record_bytes, max_node_id_bytes, max_open_files,
                )
                for index, (start, end, is_last) in enumerate(ranges)
            ]
            map_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [pool.submit(_map_source_range, args) for args in map_args]
                for completed, future in enumerate(as_completed(futures), 1):
                    map_results.append(future.result())
                    emit(
                        "preparing",
                        0.05 + 0.30 * completed / len(futures),
                        "分块读取与哈希分区进行中",
                        {"chunks_completed": completed, "chunks_total": len(futures)},
                    )

            counters = {
                key: sum(int(row.get(key, 0)) for row in map_results)
                for key in (
                    "raw_records", "valid_records", "invalid_records", "self_loops_seen",
                    "self_loops_skipped", "explicit_weight_records",
                )
            }
            map_results.sort(key=lambda row: int(row["map_index"]))
            # Added: use the source bytes themselves for the published
            # fingerprint.  The parallel map digests remain useful for
            # diagnostics, but their combination changes when worker count or
            # byte ranges change and therefore cannot identify the source.
            source_content_fingerprint = _stream_sha256(source)
            if policy.invalid_records == "reject" and counters["invalid_records"]:
                raise ValueError(f"发现 {counters['invalid_records']} 条无效记录，策略要求拒绝")
            if policy.self_loops == "reject" and counters["self_loops_seen"]:
                raise ValueError(f"发现 {counters['self_loops_seen']} 条自环，策略要求拒绝")

            emit("validating", 0.40, "开始跨分区校验与去重", counters)
            map_dirs = [Path(row["map_root"]) for row in map_results]
            edge_args = []
            node_args = []
            for partition in range(partitions):
                edge_args.append(
                    (
                        partition,
                        [str(root / f"edge-{partition:05d}.jsonl") for root in map_dirs],
                        str(edges_root / f"part-{partition:05d}.jsonl"),
                        str(run_root), policy.to_dict(), chunk_rows, chunk_bytes,
                    )
                )
                node_args.append(
                    (
                        partition,
                        [str(root / f"node-{partition:05d}.jsonl") for root in map_dirs],
                        str(nodes_root / f"part-{partition:05d}.jsonl"),
                        str(run_root), chunk_rows, chunk_bytes,
                    )
                )
            edge_results: list[dict[str, Any]] = []
            node_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                future_kind = {
                    **{pool.submit(_reduce_edge_partition, args): "edge" for args in edge_args},
                    **{pool.submit(_reduce_node_partition, args): "node" for args in node_args},
                }
                total = len(future_kind)
                for completed, future in enumerate(as_completed(future_kind), 1):
                    result = future.result()
                    (edge_results if future_kind[future] == "edge" else node_results).append(result)
                    emit(
                        "validating",
                        0.40 + 0.45 * completed / total,
                        "分区校验与去重进行中",
                        {"partitions_completed": completed, "partition_tasks": total},
                    )

            node_count = sum(row["unique_nodes"] for row in node_results)
            edge_count = sum(row["unique_edges"] for row in edge_results)
            duplicate_edges = sum(row["duplicate_edges"] for row in edge_results)
            node_order_path = staging / "node-order.jsonl"
            ordered_node_count = _write_node_order(
                (
                    nodes_root / f"part-{index:05d}.jsonl"
                    for index in range(partitions)
                ),
                node_order_path,
                run_root / "node-order",
                chunk_rows=chunk_rows,
                chunk_bytes=chunk_bytes,
            )
            if ordered_node_count != node_count:
                raise ValueError("节点顺序索引计数与规范化快照不一致")

            # 新增：把规范化边并行重分区为可随机访问的出/入邻接分片。
            # 这一表示是全部大图遍历算法的共同底座，避免每扩展一个节点
            # 都重扫全量 edge 文件。
            emit("indexing", 0.86, "开始构建分区邻接索引", {"partitions": partitions})
            canonical_edge_files = [
                edges_root / f"part-{index:05d}.jsonl" for index in range(partitions)
            ]
            adjacency_maps: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(
                        _map_adjacency_partition,
                        (
                            str(path), str(adjacency_map_root), index,
                            partitions, max_open_files,
                        ),
                    )
                    for index, path in enumerate(canonical_edge_files)
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    adjacency_maps.append(future.result())
                    emit(
                        "indexing", 0.86 + 0.03 * completed / len(futures),
                        "邻接边重分区进行中",
                        {"partitions_completed": completed, "partitions": len(futures)},
                    )
            adjacency_map_dirs = [row["map_root"] for row in adjacency_maps]
            adjacency_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(
                        _build_adjacency_partition,
                        (
                            partition,
                            str(nodes_root / f"part-{partition:05d}.jsonl"),
                            adjacency_map_dirs,
                            str(adjacency_root / f"part-{partition:05d}.sqlite3"),
                        ),
                    )
                    for partition in range(partitions)
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    adjacency_results.append(future.result())
                    emit(
                        "indexing", 0.89 + 0.04 * completed / len(futures),
                        "邻接索引写入进行中",
                        {"partitions_completed": completed, "partitions": len(futures)},
                    )
            if sum(row["out_edges"] for row in adjacency_results) != edge_count:
                raise ValueError("出邻接索引边数与规范化快照不一致")
            if sum(row["in_edges"] for row in adjacency_results) != edge_count:
                raise ValueError("入邻接索引边数与规范化快照不一致")
            quality = {
                **counters,
                "node_count": node_count,
                "edge_count": edge_count,
                "duplicate_edges_removed": duplicate_edges,
                "duplicate_node_mentions": sum(row["duplicate_node_mentions"] for row in node_results),
                "policy_id": policy.policy_id,
                "policy": policy.to_dict(),
                "source_content_fingerprint": source_content_fingerprint,
                "max_record_bytes": max_record_bytes,
                "max_node_id_bytes": max_node_id_bytes,
                "map_open_files": max_open_files,
            }
            final_stat = source.stat()
            if final_stat.st_size != stat.st_size or final_stat.st_mtime_ns != stat.st_mtime_ns:
                raise ValueError("上传文件在快照构建期间发生变化，已拒绝发布")
            node_files = [
                f"nodes/part-{index:05d}.jsonl" for index in range(partitions)
            ]
            edge_files = [
                f"edges/part-{index:05d}.jsonl" for index in range(partitions)
            ]
            adjacency_files = [
                f"adjacency/part-{index:05d}.sqlite3"
                for index in range(partitions)
            ]
            storage_bytes = measure_snapshot_storage(
                staging,
                node_files=node_files,
                edge_files=edge_files,
                adjacency_files=adjacency_files,
                node_order_file="node-order.jsonl",
            )
            manifest = {
                "format": self.SNAPSHOT_FORMAT,
                "snapshot_id": snapshot_id,
                "graph_id": graph_id,
                "graph_version": int(graph_version),
                "state": "published",
                "directed": bool(directed),
                "has_weights": counters["explicit_weight_records"] > 0,
                "node_count": node_count,
                "edge_count": edge_count,
                "partitions": partitions,
                "node_files": node_files,
                "node_order_file": "node-order.jsonl",
                "edge_files": edge_files,
                # 新增清单字段：算法必须使用分片索引，不允许回退到全图对象。
                "adjacency_format": "igraph-sharded-sqlite-adjacency-v1",
                "adjacency_files": adjacency_files,
                "storage_bytes": storage_bytes,
                "quality": quality,
            }
            manifest_tmp = staging / "manifest.json.tmp"
            manifest_tmp.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest_tmp.replace(staging / "manifest.json")
            # Map/sort files are private staging data and must never become
            # visible as part of the immutable published snapshot.
            remove_tree_and_fsync(map_root)
            remove_tree_and_fsync(run_root)
            remove_tree_and_fsync(adjacency_map_root)
            graph_root.mkdir(parents=True, exist_ok=True)
            durable_publish_tree(
                staging,
                final,
                durability_root=self.root.parent,
            )
            emit("publishing", 0.95, "快照文件已原子发布", {"snapshot_id": snapshot_id})
            return {
                **manifest,
                "manifest_path": str(final / "manifest.json"),
                "quality": quality,
                "summary": {
                    "snapshot_id": snapshot_id,
                    "graph_id": graph_id,
                    "graph_version": int(graph_version),
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "partitions": partitions,
                    "directed": bool(directed),
                    "has_weights": counters["explicit_weight_records"] > 0,
                    "storage_bytes": storage_bytes,
                    "quality": quality,
                },
            }
        except BaseException:
            # A failed/cancelled build never leaves a discoverable manifest.
            try:
                remove_tree_and_fsync(staging)
            except (OSError, ValueError):
                # Preserve the original preparation/cancellation error.
                pass
            raise
