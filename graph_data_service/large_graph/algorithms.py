"""Disk-backed implementations of the small-graph compute catalogue.

This module deliberately does not import GraphData or NetworkX. Topology is
read from node-owned snapshot shards and all graph-sized mutable state lives in
SQLite files under the current job's staging directory.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import queue
import shutil
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

from .compatibility import (
    SMALL_GRAPH_ALGORITHM_NAMES,
)
from .disk_graph import PartitionedDiskGraph
from .grouped_algorithms import GroupedAnalysisMixin
from .processes import process_context
from .snapshot import _stable_partition


ProgressCallback = Callable[[str, float, str, dict[str, Any]], None]


def _centrality_control_value(value: Any) -> float:
    """Round ordinary scores without erasing valid large-graph probabilities.

    Six decimal *places* is adequate on small graphs but turns uniform
    PageRank on more than one million nodes into ``0.0`` in every control-plane
    preview.  Keep the established formatting for ordinary values and switch
    to six significant digits only inside that sub-micro range.  Complete
    artifact rows already retain the unrounded float.
    """
    number = float(value)
    if number == 0.0 or abs(number) >= 1.0e-6:
        return round(number, 6)
    return float(f"{number:.6g}")


def _algorithm_partition_census(args: tuple[Any, ...]) -> dict[str, Any]:
    """Scan disjoint adjacency shards for a hybrid parallel algorithm stage.

    Some graph kernels have an intrinsically ordered reduce phase (for
    example residual-flow augmentation or Dijkstra's priority queue). They
    still benefit from doing their graph-wide input work in parallel. Each
    worker owns a stable subset of adjacency shards and produces bounded
    statistics which the coordinator validates against the immutable
    snapshot before accepting the algorithm result.
    """

    manifest_path_raw, worker_index, worker_count, algorithm = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    node_count = 0
    out_edge_count = 0
    in_edge_count = 0
    negative_edge_count = 0
    min_weight = math.inf
    max_weight = -math.inf
    partitions_scanned = 0
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        for partition in range(graph.partitions):
            if partition % worker_count != worker_index:
                continue
            connection = graph._connection(partition)
            node_count += int(
                connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            )
            out_row = connection.execute(
                """
                SELECT COUNT(*),MIN(weight),MAX(weight),
                       COALESCE(SUM(CASE WHEN weight<0 THEN 1 ELSE 0 END),0)
                FROM out_edges
                """
            ).fetchone()
            in_edge_count += int(
                connection.execute("SELECT COUNT(*) FROM in_edges").fetchone()[0]
            )
            out_edge_count += int(out_row[0])
            if out_row[1] is not None:
                min_weight = min(min_weight, float(out_row[1]))
                max_weight = max(max_weight, float(out_row[2]))
            negative_edge_count += int(out_row[3])
            partitions_scanned += 1
    return {
        "algorithm": str(algorithm),
        "worker": worker_index,
        "process_id": os.getpid(),
        "partitions_scanned": partitions_scanned,
        "node_count": node_count,
        "out_edge_count": out_edge_count,
        "in_edge_count": in_edge_count,
        "negative_edge_count": negative_edge_count,
        "min_weight": None if min_weight == math.inf else min_weight,
        "max_weight": None if max_weight == -math.inf else max_weight,
    }
_LIST_LIMIT = 20
_PATH_LIMIT = 10
_MAX_LIMIT = 200
# A control-plane object must stay small even when one returned path/cycle/
# clique spans millions of vertices.  Complete memberships are written as
# scalar JSONL rows; summaries retain at most this many sequences and members.
_SEQUENCE_PREVIEW_LIMIT = 20
_CONTROL_NODE_CHARS = 256
_DEFAULT_MAX_SAMPLE_SOURCES = 10_000
_BOUNDED_ENUMERATION_COUNTER: Any = None
_BOUNDED_ENUMERATION_LIMIT = 0


def _init_bounded_enumeration_worker(
    shared_counter: Any, result_limit: int
) -> None:
    """Install spawn-safe shared state for one bounded enumeration pool."""

    global _BOUNDED_ENUMERATION_COUNTER, _BOUNDED_ENUMERATION_LIMIT
    _BOUNDED_ENUMERATION_COUNTER = shared_counter
    _BOUNDED_ENUMERATION_LIMIT = int(result_limit)


def brandes_sample_sources_limit() -> int:
    """Return the deployment hard limit for one Brandes source sample.

    This is deliberately read for every request/job rather than at import
    time so a recovered durable job is checked against the worker's current
    deployment policy as well as the policy used when it was enqueued.
    """
    try:
        configured = int(
            os.getenv(
                "LARGE_GRAPH_MAX_SAMPLE_SOURCES",
                str(_DEFAULT_MAX_SAMPLE_SOURCES),
            )
        )
    except (TypeError, ValueError, OverflowError):
        configured = _DEFAULT_MAX_SAMPLE_SOURCES
    return max(1, configured)


def _brandes_sample_cap(parameters: dict[str, Any]) -> int:
    """Parse and enforce the Brandes source cap inside the worker process."""
    raw = parameters.get(
        "sample_sources", os.getenv("LARGE_GRAPH_BRANDES_SOURCES", "256")
    )
    if isinstance(raw, bool):
        raise ValueError("参数 sample_sources 必须为正整数")
    try:
        sample_cap = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("参数 sample_sources 必须为正整数") from exc
    hard_limit = brandes_sample_sources_limit()
    if sample_cap < 1 or sample_cap > hard_limit:
        raise ValueError(
            "参数 sample_sources 必须在 1 到 "
            f"{hard_limit} 之间（LARGE_GRAPH_MAX_SAMPLE_SOURCES）"
        )
    return sample_cap


def _finite_float_parameter(
    parameters: dict[str, Any], name: str, default: float
) -> float:
    """Parse a finite worker-side floating-point algorithm parameter."""
    raw = parameters.get(name, default)
    if isinstance(raw, bool):
        raise ValueError(f"参数 {name} 必须为有限数值")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"参数 {name} 必须为有限数值") from exc
    if not math.isfinite(value):
        raise ValueError(f"参数 {name} 必须为有限数值")
    return value


def _link_prediction_source_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Compute one deterministic source batch in an isolated worker.

    Added for the large-path parallel engine: global link prediction has no
    cross-source state, so each worker can scan its source partition and keep
    only a local top-k heap.  The parent performs the exact top-k merge and
    never collects all O(V²) candidate scores in memory.
    """
    manifest_path_raw, method, worker_index, worker_count, top_k, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    work_root = Path(work_root_raw) / f"link-prediction-{worker_index:05d}"
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(graph, work_root, work_root / "results", workers=1)
        connection = kernel._connection("scores")
        connection.executescript(
            """
            CREATE TABLE neighbors_a(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE neighbors_b(node TEXT PRIMARY KEY) WITHOUT ROWID;
            """
        )
        top: list[tuple[float, str, str]] = []
        source_count = 0
        for source in graph.iter_node_ids():
            if _stable_partition(source, worker_count) != worker_index:
                continue
            source_count += 1
            for target in graph.iter_node_ids():
                if target <= source or kernel._is_adjacent_undirected(source, target):
                    continue
                score = kernel._link_score(connection, method, source, target)
                candidate = (score, source, target)
                if len(top) < top_k:
                    heapq.heappush(top, candidate)
                elif candidate > top[0]:
                    heapq.heapreplace(top, candidate)
        connection.close()
    return {"worker": worker_index, "source_count": source_count, "top": top}


def _global_cut_sink_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Evaluate a deterministic subset of root-to-sink cuts in one process."""
    manifest_path_raw, root, worker_index, worker_count, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    work_root = Path(work_root_raw) / f"global-cut-{worker_index:05d}"
    best_value = math.inf
    best_side_path = work_root / "best-side.jsonl"
    sink_count = 0
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(graph, work_root, work_root / "results", workers=1)
        for sink in graph.iter_node_ids():
            if sink == root or _stable_partition(sink, worker_count) != worker_index:
                continue
            sink_count += 1
            # Every sink is processed serially inside this worker.  Reuse one
            # graph-sized SQLite file; ``_flow_state`` drops and rebuilds its
            # tables for the next sink.  A per-sink filename would retain up
            # to O(V) complete residual graphs until the job staging tree is
            # removed and can exhaust disk long before admission estimates.
            flow, value = kernel._flow_state(
                root,
                sink,
                undirected=True,
                stem="flow-state",
            )
            try:
                if value < best_value:
                    best_value = value
                    best_side_path.parent.mkdir(parents=True, exist_ok=True)
                    with best_side_path.open("w", encoding="utf-8") as output:
                        for (node,) in flow.execute("SELECT node FROM flow_seen ORDER BY node"):
                            output.write(json.dumps(str(node), ensure_ascii=False) + "\n")
            finally:
                flow.close()
    return {
        "worker": worker_index,
        "sink_count": sink_count,
        "best_value": best_value,
        "best_side_path": str(best_side_path) if best_side_path.exists() else "",
    }


def _brandes_source_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Write Brandes source dependencies for one source batch to disk."""
    manifest_path_raw, worker_index, worker_count, source_ids, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    work_root = Path(work_root_raw) / f"brandes-{worker_index:05d}"
    output_path = work_root / "dependencies.jsonl"
    source_count = 0
    # In sampled mode the controller sends each selected source to exactly one
    # worker.  An empty bucket must therefore remain empty; treating it like
    # exact mode would accidentally process a whole hash partition.  It also
    # avoids creating an O(V) SQLite score table for an idle worker.
    if source_ids is not None and not source_ids:
        work_root.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8"):
            pass
        return {
            "worker": worker_index,
            "source_count": 0,
            "output_path": str(output_path),
        }
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(graph, work_root, work_root / "results", workers=1)
        partial = kernel._connection("partial-scores")
        partial.execute(
            "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
        )
        partial.executemany(
            "INSERT INTO scores(node,value) VALUES (?,0)",
            ((node,) for node in graph.iter_node_ids()),
        )
        partial.commit()
        sources: Iterable[str]
        if source_ids is None:
            sources = (
                source
                for source in graph.iter_node_ids()
                if _stable_partition(source, worker_count) == worker_index
            )
        else:
            # The aggregate size of all buckets is protected by
            # LARGE_GRAPH_MAX_SAMPLE_SOURCES.  A bucket is pickled only for
            # its owning worker instead of copying the complete source list
            # into every child process.
            sources = (str(source) for source in source_ids)
        for source in sources:
            # Source jobs in one worker do not overlap.  Reuse the same
            # graph-sized shortest-path database instead of leaving one
            # complete SQLite file behind for every sampled source.
            state = kernel._shortest_state(
                source,
                weighted=True,
                stem="source-state",
                collect_all_predecessors=True,
            )
            try:
                state.executescript(
                    """
                    DROP TABLE IF EXISTS sigma;
                    DROP TABLE IF EXISTS dependency;
                    CREATE TABLE sigma(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
                    CREATE TABLE dependency(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
                    """
                )
                state.execute("INSERT INTO sigma(node,value) VALUES (?,1)", (source,))
                for (node,) in state.execute(
                    "SELECT node FROM distances WHERE node<>? ORDER BY distance,node",
                    (source,),
                ):
                    sigma = float(
                        state.execute(
                            """
                            SELECT COALESCE(SUM(sigma.value),0) FROM predecessors
                            JOIN sigma ON sigma.node=predecessors.predecessor
                            WHERE predecessors.node=?
                            """,
                            (str(node),),
                        ).fetchone()[0]
                    )
                    state.execute(
                        "INSERT INTO sigma(node,value) VALUES (?,?)", (str(node), sigma)
                    )
                state.execute("INSERT INTO dependency(node,value) SELECT node,0 FROM distances")
                for (node,) in state.execute(
                    "SELECT node FROM distances WHERE node<>? ORDER BY distance DESC,node DESC",
                    (source,),
                ):
                    node = str(node)
                    sigma_w = float(
                        state.execute("SELECT value FROM sigma WHERE node=?", (node,)).fetchone()[0]
                    )
                    dependency = float(
                        state.execute(
                            "SELECT value FROM dependency WHERE node=?", (node,)
                        ).fetchone()[0]
                    )
                    if sigma_w:
                        for (predecessor,) in state.execute(
                            "SELECT predecessor FROM predecessors WHERE node=?", (node,)
                        ):
                            pred = str(predecessor)
                            sigma_v = float(
                                state.execute(
                                    "SELECT value FROM sigma WHERE node=?", (pred,)
                                ).fetchone()[0]
                            )
                            state.execute(
                                "UPDATE dependency SET value=value+? WHERE node=?",
                                (sigma_v / sigma_w * (1.0 + dependency), pred),
                            )
                # Brandes excludes the source vertex itself from the
                # dependency accumulation.  Aggregate inside the worker so
                # the interchange file is O(V), not O(sources * V).
                partial.executemany(
                    "UPDATE scores SET value=value+? WHERE node=?",
                    (
                        (float(dependency), str(node))
                        for node, dependency in state.execute(
                            "SELECT node,value FROM dependency WHERE node<>? AND value<>0",
                            (source,),
                        )
                    ),
                )
                partial.commit()
            finally:
                state.close()
            source_count += 1
        with output_path.open("w", encoding="utf-8") as output:
            for node, dependency in partial.execute(
                "SELECT node,value FROM scores WHERE value<>0 ORDER BY node"
            ):
                output.write(
                    json.dumps([str(node), float(dependency)], separators=(",", ":"))
                    + "\n"
                )
        partial.close()
    return {
        "worker": worker_index,
        "source_count": source_count,
        "output_path": str(output_path),
    }


def _distance_source_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Compute an independent batch of single-source distance statistics.

    The mutable traversal state is a single SQLite database per worker and is
    rebuilt in place for every source.  Node-valued centrality results are
    streamed to a worker-owned JSONL shard; graph-level metrics return only
    constant-size partial aggregates to the parent process.
    """
    (
        manifest_path_raw,
        worker_index,
        worker_count,
        source_path_raw,
        mode,
        weighted,
        direction,
        work_root_raw,
    ) = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    source_path = Path(str(source_path_raw)) if source_path_raw else None
    work_root = Path(work_root_raw) / f"distance-{mode}-{worker_index:05d}"
    output_path = work_root / "scores.jsonl"
    source_count = 0
    pair_count = 0
    total_distance = 0.0
    diameter = 0.0
    radius: float | None = None

    def iter_sources(graph: PartitionedDiskGraph) -> Iterable[str]:
        if source_path is None:
            yield from graph.iter_node_ids()
            return
        with source_path.open("r", encoding="utf-8") as source_file:
            for line in source_file:
                if line.strip():
                    yield str(json.loads(line))

    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(graph, work_root, work_root / "results", workers=1)
        score_output = (
            output_path.open("w", encoding="utf-8")
            if mode in {"closeness", "harmonic"}
            else None
        )
        try:
            for source in iter_sources(graph):
                if _stable_partition(source, worker_count) != worker_index:
                    continue
                distances = kernel._shortest_state(
                    source,
                    weighted=bool(weighted),
                    direction=str(direction),
                    stem="source-state",
                )
                try:
                    count, total, eccentricity = distances.execute(
                        """
                        SELECT COUNT(*),COALESCE(SUM(distance),0),
                               COALESCE(MAX(distance),0)
                        FROM distances WHERE node<>?
                        """,
                        (source,),
                    ).fetchone()
                    reachable = int(count)
                    distance_sum = float(total)
                    eccentricity_value = float(eccentricity)
                    if mode == "closeness":
                        value = (
                            reachable / distance_sum
                            * reachable / max(1, graph.node_count - 1)
                            if reachable and distance_sum > 0
                            else 0.0
                        )
                        assert score_output is not None
                        score_output.write(
                            json.dumps(
                                [source, value],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                    elif mode == "harmonic":
                        value = float(
                            distances.execute(
                                """
                                SELECT COALESCE(SUM(1.0/distance),0)
                                FROM distances WHERE distance>0
                                """
                            ).fetchone()[0]
                        )
                        assert score_output is not None
                        score_output.write(
                            json.dumps(
                                [source, value],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                    else:
                        pair_count += reachable
                        total_distance += distance_sum
                        diameter = max(diameter, eccentricity_value)
                        radius = (
                            eccentricity_value
                            if radius is None
                            else min(radius, eccentricity_value)
                        )
                finally:
                    distances.close()
                source_count += 1
        finally:
            if score_output is not None:
                score_output.close()
    return {
        "worker": worker_index,
        "source_count": source_count,
        "pair_count": pair_count,
        "total_distance": total_distance,
        "diameter": diameter,
        "radius": radius,
        "output_path": str(output_path) if output_path.exists() else "",
    }


def _result_partition_writer(
    assignments: list[tuple[int, str]], rows: Any, errors: Any
) -> None:
    """Encode and write a fixed set of result partitions in one process."""
    handles: dict[int, Any] = {}
    try:
        for partition, path_raw in assignments:
            path = Path(path_raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            handles[int(partition)] = path.open("w", encoding="utf-8")
        while True:
            item = rows.get()
            if item is None:
                break
            partition, row = item
            handles[int(partition)].write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
    except BaseException as exc:
        try:
            errors.put({"type": type(exc).__name__, "error": str(exc)})
        finally:
            raise
    finally:
        for handle in handles.values():
            handle.close()


def _degree_analysis_node_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Compute degree rows for disjoint node-owned snapshot partitions."""
    manifest_path_raw, worker_index, worker_count, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    work_root = Path(work_root_raw) / f"degree-analysis-{worker_index:05d}"
    work_root.mkdir(parents=True, exist_ok=True)
    output_path = work_root / "degrees.jsonl"
    row_count = 0
    with PartitionedDiskGraph(manifest_path_raw) as graph, output_path.open(
        "w", encoding="utf-8"
    ) as output:
        node_files = list(graph.manifest.get("node_files") or [])
        for partition, relative in enumerate(node_files):
            if partition % worker_count != worker_index:
                continue
            with (graph.root / str(relative)).open("r", encoding="utf-8") as nodes:
                for line in nodes:
                    row = json.loads(line)
                    node = str(row["id"])
                    incoming = (
                        graph.degree(node, direction="in") if graph.directed else 0
                    )
                    outgoing = (
                        graph.degree(node, direction="out") if graph.directed else 0
                    )
                    total = (
                        incoming + outgoing
                        if graph.directed
                        else graph.degree(node)
                    )
                    output.write(
                        json.dumps(
                            [
                                node,
                                int(row.get("ordinal", 0)),
                                incoming,
                                outgoing,
                                total,
                            ],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    row_count += 1
    return {
        "worker": worker_index,
        "row_count": row_count,
        "output_path": str(output_path),
    }


def _community_label_node_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Map one immutable-label community iteration to a worker JSONL shard.

    The controller passes a disk-backed node assignment rather than a Python
    node list or the complete label map.  The published graph and the current
    iteration's committed ``labels`` table are opened read-only.  A worker
    reuses one private SQLite vote table, so even a hub with O(V) distinct
    neighbouring labels cannot grow a Python ``Counter`` without bound.
    """
    (
        manifest_path_raw,
        labels_path_raw,
        node_path_raw,
        worker_index,
        iteration,
        map_root_raw,
    ) = args
    worker_index = int(worker_index)
    iteration = int(iteration)
    iteration_root = (
        Path(map_root_raw)
        / f"iteration-{iteration:05d}"
        / f"worker-{worker_index:05d}"
    )
    iteration_root.mkdir(parents=True, exist_ok=True)
    output_path = iteration_root / "labels.jsonl"
    votes_path = iteration_root / "votes.sqlite3"
    labels_uri = Path(str(labels_path_raw)).resolve().as_uri()
    labels = sqlite3.connect(f"{labels_uri}?mode=ro", uri=True)
    labels.execute("PRAGMA query_only=ON")
    labels.execute("PRAGMA temp_store=FILE")
    votes = sqlite3.connect(votes_path)
    votes.execute("PRAGMA journal_mode=OFF")
    votes.execute("PRAGMA synchronous=OFF")
    votes.execute("PRAGMA temp_store=FILE")
    votes.executescript(
        """
        DROP TABLE IF EXISTS label_votes;
        CREATE TABLE label_votes(
            label TEXT PRIMARY KEY,score REAL NOT NULL DEFAULT 0
        ) WITHOUT ROWID;
        """
    )
    votes.commit()
    row_count = 0
    changed = 0
    try:
        # No writer runs until every map future has returned, so this
        # read-only transaction is a stable snapshot for the whole shard.
        labels.execute("BEGIN")
        with PartitionedDiskGraph(manifest_path_raw) as graph, Path(
            str(node_path_raw)
        ).open("r", encoding="utf-8") as nodes, output_path.open(
            "w", encoding="utf-8"
        ) as output:
            for line in nodes:
                if not line.strip():
                    continue
                node = str(json.loads(line))
                current_row = labels.execute(
                    "SELECT label FROM labels WHERE node=?", (node,)
                ).fetchone()
                if current_row is None:
                    raise RuntimeError(f"社区标签快照缺少节点 {node!r}")
                current = str(current_row[0])
                votes.execute("DELETE FROM label_votes")
                for neighbor, weight in graph.iter_neighbors(
                    node, undirected=True
                ):
                    row = labels.execute(
                        "SELECT label FROM labels WHERE node=?", (neighbor,)
                    ).fetchone()
                    if row is not None:
                        votes.execute(
                            """
                            INSERT INTO label_votes(label,score) VALUES (?,?)
                            ON CONFLICT(label) DO UPDATE SET
                                score=score+excluded.score
                            """,
                            (str(row[0]), max(0.0, float(weight))),
                        )
                selected_row = votes.execute(
                    """
                    SELECT label FROM label_votes
                    ORDER BY score DESC,label ASC LIMIT 1
                    """
                ).fetchone()
                selected = str(selected_row[0]) if selected_row else current
                row_changed = int(selected != current)
                output.write(
                    json.dumps(
                        [node, selected, row_changed],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                row_count += 1
                changed += row_changed
        votes.commit()
    finally:
        labels.close()
        votes.close()
    return {
        "worker": worker_index,
        "row_count": row_count,
        "changed": changed,
        "output_path": str(output_path),
    }


def _triangle_node_aggregate(
    connection: sqlite3.Connection,
    nodes: Iterable[str],
    max_weight: float,
) -> tuple[int, int, float, int]:
    """Reduce triangle/wedge contributions for an independent node batch.

    ``projected_edges`` is immutable while this runs. Only the current
    vertex's neighbor set is materialised, in a connection-local temporary
    table, so a worker never keeps graph-sized state in memory.
    """
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS triangle_neighbors(
            node TEXT PRIMARY KEY,weight REAL NOT NULL
        ) WITHOUT ROWID
        """
    )
    closed_triplets = 0
    triplets = 0
    clustering_sum = 0.0
    node_count = 0
    for node in nodes:
        node_count += 1
        connection.execute("DELETE FROM triangle_neighbors")
        connection.executemany(
            """
            INSERT INTO triangle_neighbors(node,weight) VALUES (?,?)
            ON CONFLICT(node) DO UPDATE SET weight=MIN(weight,excluded.weight)
            """,
            (
                (str(neighbor), float(weight))
                for neighbor, weight in connection.execute(
                    """
                    SELECT CASE WHEN source=? THEN target ELSE source END AS neighbor,
                           weight
                    FROM projected_edges
                    WHERE (source=? OR target=?) AND source<>target
                    ORDER BY neighbor
                    """,
                    (node, node, node),
                )
            ),
        )
        degree = int(
            connection.execute(
                "SELECT COUNT(*) FROM triangle_neighbors"
            ).fetchone()[0]
        )
        if degree < 2:
            continue
        local_triangles = 0
        weighted_triangles = 0.0
        for left, left_weight in connection.execute(
            "SELECT node,weight FROM triangle_neighbors ORDER BY node"
        ):
            for right, right_weight in connection.execute(
                "SELECT node,weight FROM triangle_neighbors WHERE node>? ORDER BY node",
                (str(left),),
            ):
                edge_left, edge_right = sorted((str(left), str(right)))
                cross = connection.execute(
                    "SELECT weight FROM projected_edges WHERE source=? AND target=?",
                    (edge_left, edge_right),
                ).fetchone()
                if cross is None:
                    continue
                local_triangles += 1
                if max_weight == 0:
                    raise ValueError(
                        "weighted clustering is undefined when maximum edge weight is zero"
                    )
                product = (
                    float(left_weight) * float(right_weight) * float(cross[0])
                ) / (max_weight**3)
                weighted_triangles += math.copysign(
                    abs(product) ** (1.0 / 3.0), product
                )
        closed_triplets += local_triangles
        triplets += degree * (degree - 1) // 2
        clustering_sum += 2.0 * weighted_triangles / (degree * (degree - 1))
    return closed_triplets, triplets, clustering_sum, node_count


def _triangle_node_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Enumerate wedges for one deterministic node batch in a process."""
    (
        manifest_path_raw,
        projection_path_raw,
        worker_index,
        worker_count,
        max_weight,
    ) = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    projection_uri = Path(str(projection_path_raw)).resolve().as_uri()
    connection = sqlite3.connect(
        f"{projection_uri}?mode=ro&immutable=1", uri=True
    )
    connection.execute("PRAGMA temp_store=FILE")
    try:
        with PartitionedDiskGraph(manifest_path_raw) as graph:
            aggregate = _triangle_node_aggregate(
                connection,
                (
                    node
                    for node in graph.iter_node_ids()
                    if _stable_partition(node, worker_count) == worker_index
                ),
                float(max_weight),
            )
    finally:
        connection.close()
    return {
        "worker": worker_index,
        "closed_triplets": aggregate[0],
        "triplets": aggregate[1],
        "clustering_sum": aggregate[2],
        "node_count": aggregate[3],
    }


def _maximum_cycle_start_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Scan one canonical-cycle start partition in an isolated process.

    A manager-backed counter enforces ``max_cycles`` across the whole job,
    rather than accidentally applying that bound once per worker. Each child
    returns only scalar metadata and one private best-cycle file, so no worker
    sends an unbounded cycle membership through the process control channel.
    """

    (
        manifest_path_raw,
        worker_index,
        worker_count,
        optimize,
        work_root_raw,
    ) = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    worker_root = Path(work_root_raw) / f"maximum-cycle-{worker_index:05d}"
    worker_root.mkdir(parents=True, exist_ok=True)
    best_path = worker_root / "best-cycle.jsonl"
    best: list[str] | None = None
    best_weight = 0.0
    best_key: tuple[float, float] = (-1.0, -math.inf)
    examined = 0
    truncated = False

    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(
            graph,
            worker_root,
            worker_root / "results",
            workers=1,
        )
        for cycle in kernel._iter_cycles(
            start_worker=worker_index,
            start_worker_count=worker_count,
        ):
            # The cycle has already been generated, so observing a full global
            # counter proves that at least one result was omitted.
            if _BOUNDED_ENUMERATION_COUNTER is None:
                raise RuntimeError("最大环并行计数器未初始化")
            with _BOUNDED_ENUMERATION_COUNTER.get_lock():
                current = int(_BOUNDED_ENUMERATION_COUNTER.value)
                if current >= _BOUNDED_ENUMERATION_LIMIT:
                    truncated = True
                    break
                _BOUNDED_ENUMERATION_COUNTER.value = current + 1
            examined += 1
            weight = kernel._cycle_weight(cycle)
            key = (
                (weight, len(cycle))
                if optimize == "weight"
                else (len(cycle), weight)
            )
            if key > best_key:
                best_key, best, best_weight = key, cycle, weight

    if best is not None:
        with best_path.open("w", encoding="utf-8") as output:
            for node in best:
                output.write(json.dumps(node, ensure_ascii=False) + "\n")
    return {
        "worker": worker_index,
        "examined": examined,
        "truncated": truncated,
        "best_key": best_key,
        "best_weight": best_weight,
        "best_path": str(best_path) if best is not None else "",
    }


def _find_cycles_start_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Write one bounded canonical-cycle start partition to private disk."""

    manifest_path_raw, worker_index, worker_count, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    worker_root = Path(work_root_raw) / f"find-cycles-{worker_index:05d}"
    worker_root.mkdir(parents=True, exist_ok=True)
    output_path = worker_root / "cycles.jsonl"
    returned = 0
    truncated = False
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(
            graph,
            worker_root,
            worker_root / "results",
            workers=1,
        )
        with output_path.open("w", encoding="utf-8") as output:
            for cycle in kernel._iter_cycles(
                include_undirected_self_loops=True,
                start_worker=worker_index,
                start_worker_count=worker_count,
            ):
                if _BOUNDED_ENUMERATION_COUNTER is None:
                    raise RuntimeError("环枚举并行计数器未初始化")
                with _BOUNDED_ENUMERATION_COUNTER.get_lock():
                    cycle_index = int(_BOUNDED_ENUMERATION_COUNTER.value)
                    if cycle_index >= _BOUNDED_ENUMERATION_LIMIT:
                        truncated = True
                        break
                    _BOUNDED_ENUMERATION_COUNTER.value = cycle_index + 1
                output.write(
                    json.dumps(
                        {"cycle_index": cycle_index, "nodes": cycle},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                returned += 1
    return {
        "worker": worker_index,
        "returned": returned,
        "truncated": truncated,
        "output_path": str(output_path),
    }


def _clique_start_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Enumerate the maximal cliques owned by one canonical root shard."""

    (
        manifest_path_raw,
        worker_index,
        worker_count,
        mode,
        requested_node,
        work_root_raw,
    ) = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    worker_root = Path(work_root_raw) / f"cliques-{worker_index:05d}"
    worker_root.mkdir(parents=True, exist_ok=True)
    output_path = worker_root / "cliques.jsonl"
    count_path = worker_root / "counts.jsonl"
    returned = 0
    truncated = False
    maximum_size = 0
    requested_count = 0

    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(
            graph,
            worker_root,
            worker_root / "results",
            workers=1,
        )
        if mode == "find_cliques":
            with output_path.open("w", encoding="utf-8") as output:
                for clique in kernel._iter_maximal_cliques(
                    root_worker=worker_index,
                    root_worker_count=worker_count,
                ):
                    if _BOUNDED_ENUMERATION_COUNTER is None:
                        raise RuntimeError("团枚举并行计数器未初始化")
                    with _BOUNDED_ENUMERATION_COUNTER.get_lock():
                        clique_index = int(_BOUNDED_ENUMERATION_COUNTER.value)
                        if clique_index >= _BOUNDED_ENUMERATION_LIMIT:
                            truncated = True
                            break
                        _BOUNDED_ENUMERATION_COUNTER.value = clique_index + 1
                    output.write(
                        json.dumps(
                            {"clique_index": clique_index, "nodes": clique},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    returned += 1
        elif mode == "clique_number":
            maximum_size = max(
                (
                    len(clique)
                    for clique in kernel._iter_maximal_cliques(
                        root_worker=worker_index,
                        root_worker_count=worker_count,
                    )
                ),
                default=0,
            )
        else:
            counts = kernel._connection("partial-clique-counts")
            counts.execute(
                "CREATE TABLE counts(node TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID"
            )
            try:
                for clique in kernel._iter_maximal_cliques(
                    root_worker=worker_index,
                    root_worker_count=worker_count,
                ):
                    if requested_node is not None:
                        if str(requested_node) in clique:
                            requested_count += 1
                        continue
                    counts.executemany(
                        """
                        INSERT INTO counts(node,value) VALUES (?,1)
                        ON CONFLICT(node) DO UPDATE SET value=value+1
                        """,
                        ((node,) for node in clique),
                    )
                counts.commit()
                if requested_node is None:
                    with count_path.open("w", encoding="utf-8") as output:
                        for node, value in counts.execute(
                            "SELECT node,value FROM counts ORDER BY node"
                        ):
                            output.write(
                                json.dumps(
                                    [str(node), int(value)],
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            )
            finally:
                counts.close()

    return {
        "worker": worker_index,
        "returned": returned,
        "truncated": truncated,
        "maximum_size": maximum_size,
        "requested_count": requested_count,
        "output_path": str(output_path) if output_path.exists() else "",
        "count_path": str(count_path) if count_path.exists() else "",
    }


def _transitive_closure_source_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    """Traverse one source shard and write a globally bounded closure sample."""

    manifest_path_raw, worker_index, worker_count, work_root_raw = args
    worker_index = int(worker_index)
    worker_count = int(worker_count)
    worker_root = Path(work_root_raw) / f"closure-{worker_index:05d}"
    worker_root.mkdir(parents=True, exist_ok=True)
    output_path = worker_root / "closure.jsonl"
    returned = 0
    truncated = False
    source_count = 0
    with PartitionedDiskGraph(manifest_path_raw) as graph:
        kernel = LargeGraphAlgorithms(
            graph,
            worker_root,
            worker_root / "results",
            workers=1,
        )
        with output_path.open("w", encoding="utf-8") as output:
            for source in graph.iter_node_ids():
                if _stable_partition(source, worker_count) != worker_index:
                    continue
                source_count += 1
                reached = kernel._reachability_state(
                    source, stem="closure-source"
                )
                try:
                    for (target,) in reached.execute(
                        "SELECT node FROM reached WHERE node<>? ORDER BY node",
                        (source,),
                    ):
                        if _BOUNDED_ENUMERATION_COUNTER is None:
                            raise RuntimeError("传递闭包并行计数器未初始化")
                        with _BOUNDED_ENUMERATION_COUNTER.get_lock():
                            current = int(_BOUNDED_ENUMERATION_COUNTER.value)
                            if current >= _BOUNDED_ENUMERATION_LIMIT:
                                truncated = True
                                break
                            _BOUNDED_ENUMERATION_COUNTER.value = current + 1
                        output.write(
                            json.dumps(
                                [source, str(target)],
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        returned += 1
                finally:
                    reached.close()
                if truncated:
                    break
    return {
        "worker": worker_index,
        "returned": returned,
        "truncated": truncated,
        "source_count": source_count,
        "output_path": str(output_path),
    }


def _limit(parameters: dict[str, Any], default: int = _LIST_LIMIT) -> int:
    """Large-graph Agent previews are always bounded; full rows go to Artifact."""
    raw = parameters.get("limit")
    if raw is None:
        return default
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return _MAX_LIMIT
    return max(1, min(int(raw), _MAX_LIMIT))


class LargeGraphAlgorithms(GroupedAnalysisMixin):
    """Algorithm catalogue operating on a partitioned disk graph."""

    # The public small-graph catalogue is the compatibility contract.  Tests
    # also verify that every name resolves to a concrete method on this class.
    SUPPORTED: frozenset[str] = SMALL_GRAPH_ALGORITHM_NAMES

    def __init__(
        self,
        graph: PartitionedDiskGraph,
        work_root: str | Path,
        result_root: str | Path,
        *,
        workers: int = 1,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.graph = graph
        self.work_root = Path(work_root)
        self.result_root = Path(result_root)
        # Source-batch algorithms use this bounded worker count directly.
        # Dependency-ordered kernels use it for a parallel partition map
        # before their disk-backed state machine performs the reduce phase.
        self.workers = max(1, int(workers))
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.result_root.mkdir(parents=True, exist_ok=True)
        self.progress = progress
        self._result_processing: dict[str, Any] = {
            "strategy": "single-process-partition-writer",
            "workers": 1,
        }

    def run(self, algorithm: str, parameters: dict[str, Any]) -> dict[str, Any]:
        name = str(algorithm).strip().lower()
        if name not in self.SUPPORTED:
            raise ValueError(f"并行引擎不支持算法 {name!r}")
        parsed_parameters = dict(parameters or {})
        native_parallel = self._uses_native_parallel_map(name, parsed_parameters)
        census_workers = self._parallel_worker_count()
        census_summary: dict[str, Any] | None = None
        if not native_parallel and census_workers > 1:
            census_pool, census_futures = self._start_partition_census(
                name, census_workers
            )
            census_summary = self._finish_partition_census(
                name,
                census_workers,
                census_pool,
                census_futures,
            )
        result = getattr(self, name)(parsed_parameters)

        summary = result.get("summary")
        if (
            isinstance(summary, dict)
            and summary.get("success") is not False
            and "parallel" not in summary
        ):
            if census_summary is not None:
                summary["parallel"] = census_summary
            elif not native_parallel:
                summary["parallel"] = {
                    "strategy": "adjacency-shard-map/disk-state-reduce",
                    "workers": 1,
                    "map_partitions": self.graph.partitions,
                    "processes_used": 1,
                    "node_count_validated": self.graph.node_count,
                    "edge_count_validated": self.graph.edge_count,
                }
            elif census_workers > 1:
                # Native parallel modes can return before reaching their map
                # stage on an empty/boundary graph. Keep the public contract
                # truthful by executing the common shard map in that case.
                pool, futures = self._start_partition_census(name, census_workers)
                summary["parallel"] = self._finish_partition_census(
                    name, census_workers, pool, futures
                )
        if result.get("result_files") and isinstance(result.get("summary"), dict):
            result["summary"].setdefault(
                "result_processing", dict(self._result_processing)
            )
        return result

    def _parallel_worker_count(self) -> int:
        return max(
            1,
            min(
                self.workers,
                max(1, self.graph.partitions),
                os.cpu_count() or 1,
            ),
        )

    @staticmethod
    def _uses_native_parallel_map(
        algorithm: str, parameters: dict[str, Any]
    ) -> bool:
        """Return whether this concrete mode already owns a parallel map."""

        if algorithm in {
            "global_minimum_cut",
            "degree_analysis",
            "community_detection",
            "find_cycles",
            "maximum_cycle",
            "clique_analysis",
        }:
            return True
        if algorithm == "node_centrality":
            return str(parameters.get("centrality_type", "degree")).lower() in {
                "betweenness",
                "closeness",
                "harmonic",
                "load",
                "percolation",
            }
        if algorithm == "reachability":
            return str(parameters.get("method", "")).lower() == "transitive_closure"
        if algorithm == "link_prediction_score":
            return not (
                parameters.get("source") is not None
                and parameters.get("target") is not None
            )
        if algorithm == "path_analysis":
            return (
                str(parameters.get("method", "")).lower()
                == "average_shortest_path_length"
            )
        if algorithm == "global_metrics":
            return str(parameters.get("metric", "")).lower() in {
                "diameter",
                "radius",
                "transitivity",
                "average_clustering",
            }
        return False

    def _start_partition_census(
        self, algorithm: str, worker_count: int
    ) -> tuple[ProcessPoolExecutor, list[Any]]:
        self._emit(
            "algorithm_map",
            0.12,
            "算法邻接分区并行扫描中",
            algorithm=algorithm,
            workers=worker_count,
            partitions=self.graph.partitions,
        )
        pool = ProcessPoolExecutor(
            max_workers=worker_count, mp_context=process_context()
        )
        futures = [
            pool.submit(
                _algorithm_partition_census,
                (
                    str(self.graph.manifest_path),
                    worker,
                    worker_count,
                    algorithm,
                ),
            )
            for worker in range(worker_count)
        ]
        return pool, futures

    def _finish_partition_census(
        self,
        algorithm: str,
        worker_count: int,
        pool: ProcessPoolExecutor,
        futures: list[Any],
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        try:
            for completed, future in enumerate(as_completed(futures), 1):
                results.append(dict(future.result()))
                self._emit(
                    "algorithm_map",
                    0.12 + 0.58 * completed / max(1, len(futures)),
                    "算法邻接分区并行扫描中",
                    algorithm=algorithm,
                    workers=worker_count,
                    workers_completed=completed,
                )
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        node_count = sum(int(row["node_count"]) for row in results)
        out_edge_count = sum(int(row["out_edge_count"]) for row in results)
        in_edge_count = sum(int(row["in_edge_count"]) for row in results)
        partitions_scanned = sum(
            int(row["partitions_scanned"]) for row in results
        )
        if node_count != self.graph.node_count:
            raise RuntimeError(
                "并行邻接分区节点计数与快照清单不一致: "
                f"{node_count} != {self.graph.node_count}"
            )
        if out_edge_count != self.graph.edge_count:
            raise RuntimeError(
                "并行邻接分区出边计数与快照清单不一致: "
                f"{out_edge_count} != {self.graph.edge_count}"
            )
        if in_edge_count != self.graph.edge_count:
            raise RuntimeError(
                "并行邻接分区入边计数与快照清单不一致: "
                f"{in_edge_count} != {self.graph.edge_count}"
            )
        if partitions_scanned != self.graph.partitions:
            raise RuntimeError("并行邻接分区扫描未覆盖完整快照")
        return {
            "strategy": "adjacency-shard-map/disk-state-reduce",
            "workers": worker_count,
            "map_partitions": partitions_scanned,
            "processes_used": len(
                {int(row["process_id"]) for row in results}
            ),
            "node_count_validated": node_count,
            "edge_count_validated": out_edge_count,
            "negative_edges_observed": sum(
                int(row["negative_edge_count"]) for row in results
            ),
        }

    def _emit(self, stage: str, value: float, message: str, **detail: Any) -> None:
        if self.progress:
            self.progress(stage, value, message, detail)

    def _connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.work_root / f"{name}.sqlite3")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        return connection

    def _display(self, node: str) -> str:
        return self.graph.display_name(node)

    def _preview_node(self, node: str) -> str:
        """Return a byte-bounded-ish label suitable for manifests/responses.

        JSON uses up to four UTF-8 bytes per Unicode code point, so the fixed
        character cap also puts a small, deterministic ceiling on each label
        in a control-plane preview.  Artifact rows retain the complete label.
        """
        value = self._display(node)
        if len(value) <= _CONTROL_NODE_CHARS:
            return value
        return value[: _CONTROL_NODE_CHARS - 1] + "…"

    def _sequence_preview(self, nodes: Iterable[str]) -> tuple[list[str], int]:
        """Consume a sequence while retaining only a fixed node preview."""
        preview: list[str] = []
        count = 0
        for node in nodes:
            if len(preview) < _SEQUENCE_PREVIEW_LIMIT:
                preview.append(self._preview_node(str(node)))
            count += 1
        return preview, count

    def _required_node(self, raw: Any, label: str) -> tuple[str | None, dict[str, Any] | None]:
        node = self.graph.resolve_node(str(raw or ""))
        if node is not None:
            return node, None
        preview = [name for _, name in self._take(self.graph.iter_nodes(), _LIST_LIMIT)]
        return None, {
            "success": False,
            "error": f"{label} '{raw or ''}' 不存在",
            "available_nodes_preview": preview,
            "available_nodes_total": self.graph.node_count,
            "available_nodes_truncated": self.graph.node_count > len(preview),
        }

    @staticmethod
    def _take(iterator: Iterable[Any], count: int) -> list[Any]:
        result: list[Any] = []
        for item in iterator:
            if len(result) >= count:
                break
            result.append(item)
        return result

    def _write_rows(
        self, stem: str, rows: Iterable[dict[str, Any]]
    ) -> tuple[list[str], list[dict[str, Any]], int]:
        """Hash-partition complete results while retaining only 20 previews."""
        root = self.result_root / stem
        root.mkdir(parents=True, exist_ok=True)
        paths = [
            root / f"part-{partition:05d}.jsonl"
            for partition in range(self.graph.partitions)
        ]
        preview: list[dict[str, Any]] = []
        count = 0
        writer_count = min(
            self.workers, max(1, self.graph.partitions), os.cpu_count() or 1
        )
        if writer_count <= 1:
            handles = [path.open("w", encoding="utf-8") for path in paths]
            try:
                for row in rows:
                    partition_key = str(
                        row.get("node_id")
                        or row.get("node")
                        or row.get("source")
                        or row.get("from")
                        or row.get("position")
                        or count
                    )
                    partition = _stable_partition(
                        partition_key, self.graph.partitions
                    )
                    handles[partition].write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    if len(preview) < _LIST_LIMIT:
                        preview.append(row)
                    count += 1
            finally:
                for handle in handles:
                    handle.close()
            self._result_processing = {
                "strategy": "single-process-partition-writer",
                "workers": 1,
            }
        else:
            context = process_context()
            row_queues = [context.Queue(maxsize=512) for _ in range(writer_count)]
            error_queue = context.Queue()
            assignments = [
                [
                    (partition, str(paths[partition]))
                    for partition in range(self.graph.partitions)
                    if partition % writer_count == writer
                ]
                for writer in range(writer_count)
            ]
            processes = [
                context.Process(
                    target=_result_partition_writer,
                    args=(assignments[index], row_queues[index], error_queue),
                    name=f"igraph-result-writer-{index}",
                )
                for index in range(writer_count)
            ]

            def put(index: int, item: Any) -> None:
                while True:
                    try:
                        row_queues[index].put(item, timeout=0.2)
                        return
                    except queue.Full:
                        failed = [
                            process
                            for process in processes
                            if process.exitcode not in (None, 0)
                        ]
                        if failed:
                            raise RuntimeError(
                                "结果分区 writer 异常退出: "
                                + ", ".join(
                                    f"{process.name}(exitcode={process.exitcode})"
                                    for process in failed
                                )
                            )

            try:
                for process in processes:
                    process.start()
                for row in rows:
                    partition_key = str(
                        row.get("node_id")
                        or row.get("node")
                        or row.get("source")
                        or row.get("from")
                        or row.get("position")
                        or count
                    )
                    partition = _stable_partition(
                        partition_key, self.graph.partitions
                    )
                    put(partition % writer_count, (partition, row))
                    if len(preview) < _LIST_LIMIT:
                        preview.append(row)
                    count += 1
                for index in range(writer_count):
                    put(index, None)
                for process in processes:
                    process.join()
                failures = [
                    process
                    for process in processes
                    if process.exitcode not in (0,)
                ]
                if failures:
                    details: list[str] = []
                    while True:
                        try:
                            error = error_queue.get_nowait()
                        except queue.Empty:
                            break
                        details.append(
                            f"{error.get('type', 'Error')}: {error.get('error', '')}"
                        )
                    raise RuntimeError(
                        "结果分区 writer 失败: "
                        + (
                            "; ".join(details)
                            if details
                            else ", ".join(
                                f"{process.name}(exitcode={process.exitcode})"
                                for process in failures
                            )
                        )
                    )
            except BaseException:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                for process in processes:
                    if process.pid is not None:
                        process.join(timeout=1)
                raise
            finally:
                for row_queue in row_queues:
                    row_queue.close()
                error_queue.close()
            self._result_processing = {
                "strategy": "bounded-queue-partition-writers",
                "workers": writer_count,
                "queue_rows_per_worker": 512,
            }
        return (
            [f"results/{stem}/{path.name}" for path in paths],
            preview,
            count,
        )

    # ── 外存最短路与遍历（新增）────────────────────────────────────

    def _shortest_state(
        self,
        source: str,
        *,
        weighted: bool,
        direction: str = "out",
        target: str | None = None,
        stem: str = "shortest",
        collect_all_predecessors: bool = False,
        blocked_nodes: set[str] | None = None,
        blocked_edges: set[tuple[str, str]] | None = None,
    ) -> sqlite3.Connection:
        """Dijkstra/BFS with distance, predecessor and priority queue on disk."""
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS distances;
            DROP TABLE IF EXISTS frontier;
            DROP TABLE IF EXISTS predecessors;
            CREATE TABLE distances(
                node TEXT PRIMARY KEY, distance REAL NOT NULL, predecessor TEXT
            ) WITHOUT ROWID;
            CREATE TABLE frontier(distance REAL NOT NULL, node TEXT NOT NULL);
            CREATE INDEX frontier_distance_idx ON frontier(distance,node);
            CREATE TABLE predecessors(
                node TEXT NOT NULL, predecessor TEXT NOT NULL,
                PRIMARY KEY(node,predecessor)
            ) WITHOUT ROWID;
            """
        )
        connection.execute(
            "INSERT INTO distances(node,distance,predecessor) VALUES (?,?,NULL)",
            (source, 0.0),
        )
        connection.execute("INSERT INTO frontier(distance,node) VALUES (?,?)", (0.0, source))
        connection.commit()
        blocked_nodes = blocked_nodes or set()
        blocked_edges = blocked_edges or set()

        while True:
            row = connection.execute(
                "SELECT rowid,distance,node FROM frontier ORDER BY distance,node LIMIT 1"
            ).fetchone()
            if row is None:
                break
            rowid, distance, node = int(row[0]), float(row[1]), str(row[2])
            connection.execute("DELETE FROM frontier WHERE rowid=?", (rowid,))
            current = connection.execute(
                "SELECT distance FROM distances WHERE node=?", (node,)
            ).fetchone()
            if current is None or distance != float(current[0]):
                continue
            if target is not None and node == target:
                break
            for neighbor, raw_weight in self.graph.iter_neighbors(node, direction=direction):
                if neighbor in blocked_nodes or (node, neighbor) in blocked_edges:
                    continue
                weight = float(raw_weight) if weighted else 1.0
                if weight < 0:
                    raise ValueError("Dijkstra 不支持负权边")
                candidate = distance + weight
                old = connection.execute(
                    "SELECT distance FROM distances WHERE node=?", (neighbor,)
                ).fetchone()
                if old is None or candidate < float(old[0]):
                    connection.execute(
                        """
                        INSERT INTO distances(node,distance,predecessor) VALUES (?,?,?)
                        ON CONFLICT(node) DO UPDATE SET
                            distance=excluded.distance, predecessor=excluded.predecessor
                        """,
                        (neighbor, candidate, node),
                    )
                    connection.execute(
                        "INSERT INTO frontier(distance,node) VALUES (?,?)",
                        (candidate, neighbor),
                    )
                    if collect_all_predecessors:
                        connection.execute("DELETE FROM predecessors WHERE node=?", (neighbor,))
                        connection.execute(
                            "INSERT INTO predecessors(node,predecessor) VALUES (?,?)",
                            (neighbor, node),
                        )
                elif collect_all_predecessors and candidate == float(old[0]):
                    connection.execute(
                        "INSERT OR IGNORE INTO predecessors(node,predecessor) VALUES (?,?)",
                        (neighbor, node),
                    )
            connection.commit()
        return connection

    def _bidirectional_bfs_state(
        self,
        source: str,
        target: str,
        *,
        stem: str = "bidirectional-bfs",
    ) -> sqlite3.Connection:
        """Find one exact unit-cost path with disk-backed bidirectional BFS.

        Both visited sets and both frontiers stay in SQLite.  Forward rows
        retain a predecessor towards ``source`` while backward rows retain a
        successor towards ``target``.  Only the selected path is copied into
        the established ``distances`` table consumed by the bounded result
        and artifact writers.

        For directed graphs the reverse search follows incoming edges.  An
        undirected snapshot exposes both endpoint orientations through the
        same cursor API, so it needs no separate in-memory projection.
        """
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS distances;
            DROP TABLE IF EXISTS forward_seen;
            DROP TABLE IF EXISTS backward_seen;
            DROP TABLE IF EXISTS forward_frontier;
            DROP TABLE IF EXISTS backward_frontier;
            DROP TABLE IF EXISTS forward_next;
            DROP TABLE IF EXISTS backward_next;
            DROP TABLE IF EXISTS search_stats;
            CREATE TABLE distances(
                node TEXT PRIMARY KEY, distance REAL NOT NULL, predecessor TEXT
            ) WITHOUT ROWID;
            CREATE TABLE forward_seen(
                node TEXT PRIMARY KEY, depth INTEGER NOT NULL, predecessor TEXT
            ) WITHOUT ROWID;
            CREATE TABLE backward_seen(
                node TEXT PRIMARY KEY, depth INTEGER NOT NULL, successor TEXT
            ) WITHOUT ROWID;
            CREATE TABLE forward_frontier(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE backward_frontier(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE forward_next(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE backward_next(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE search_stats(
                strategy TEXT NOT NULL,
                forward_visited INTEGER NOT NULL,
                backward_visited INTEGER NOT NULL,
                forward_depth INTEGER NOT NULL,
                backward_depth INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO forward_seen(node,depth,predecessor) VALUES (?,?,NULL)",
            (source, 0),
        )
        connection.execute(
            "INSERT INTO backward_seen(node,depth,successor) VALUES (?,?,NULL)",
            (target, 0),
        )
        connection.execute("INSERT INTO forward_frontier(node) VALUES (?)", (source,))
        connection.execute("INSERT INTO backward_frontier(node) VALUES (?)", (target,))
        connection.commit()

        forward_visited = 1
        backward_visited = 1
        forward_depth = 0
        backward_depth = 0
        forward_frontier_count = 1
        backward_frontier_count = 1
        meeting: str | None = source if source == target else None
        # Alternating the preferred side on equal frontier sizes prevents a
        # regular chain/ring from degenerating back into one-sided BFS.
        prefer_forward = True
        emit_step = max(10_000, max(1, self.graph.node_count) // 200)
        next_emit = emit_step

        while meeting is None and forward_frontier_count and backward_frontier_count:
            if forward_frontier_count == backward_frontier_count:
                expand_forward = prefer_forward
                prefer_forward = not prefer_forward
            else:
                expand_forward = forward_frontier_count < backward_frontier_count

            if expand_forward:
                frontier_table = "forward_frontier"
                next_table = "forward_next"
                seen_table = "forward_seen"
                other_seen_table = "backward_seen"
                link_column = "predecessor"
                direction = "out"
                next_depth = forward_depth + 1
            else:
                frontier_table = "backward_frontier"
                next_table = "backward_next"
                seen_table = "backward_seen"
                other_seen_table = "forward_seen"
                link_column = "successor"
                direction = "in"
                next_depth = backward_depth + 1

            connection.execute(f"DELETE FROM {next_table}")
            next_frontier_count = 0
            for (raw_node,) in connection.execute(
                f"SELECT node FROM {frontier_table} ORDER BY node"
            ):
                node = str(raw_node)
                for neighbor, _ in self.graph.iter_neighbors(node, direction=direction):
                    inserted = connection.execute(
                        f"INSERT OR IGNORE INTO {seen_table}"
                        f"(node,depth,{link_column}) VALUES (?,?,?)",
                        (neighbor, next_depth, node),
                    )
                    if inserted.rowcount != 1:
                        continue
                    connection.execute(
                        f"INSERT INTO {next_table}(node) VALUES (?)", (neighbor,)
                    )
                    next_frontier_count += 1
                    if expand_forward:
                        forward_visited += 1
                    else:
                        backward_visited += 1
                    visited_total = forward_visited + backward_visited
                    if visited_total >= next_emit:
                        ratio = min(
                            1.0,
                            visited_total / max(1, self.graph.node_count),
                        )
                        self._emit(
                            "algorithm",
                            min(0.88, 0.72 + 0.16 * ratio),
                            "执行双向 BFS",
                            strategy="bidirectional_bfs",
                            forward_visited=forward_visited,
                            backward_visited=backward_visited,
                            forward_depth=forward_depth,
                            backward_depth=backward_depth,
                        )
                        next_emit += emit_step

            connection.execute(f"DELETE FROM {frontier_table}")
            connection.execute(
                f"INSERT INTO {frontier_table}(node) SELECT node FROM {next_table}"
            )
            connection.execute(f"DELETE FROM {next_table}")
            if expand_forward:
                forward_depth = next_depth
                forward_frontier_count = next_frontier_count
            else:
                backward_depth = next_depth
                backward_frontier_count = next_frontier_count

            # Every row in the new frontier has the same depth.  Select the
            # shallowest opposite-side match, then a stable node id for
            # deterministic path selection when several shortest paths tie.
            intersection = connection.execute(
                f"""
                SELECT frontier.node
                FROM {frontier_table} AS frontier
                JOIN {other_seen_table} AS other ON other.node=frontier.node
                ORDER BY other.depth,frontier.node
                LIMIT 1
                """
            ).fetchone()
            if intersection is not None:
                meeting = str(intersection[0])
            connection.commit()

        if meeting is not None:
            # Copy the source -> meeting predecessor chain without retaining
            # an O(path_length) Python list.
            current = meeting
            while True:
                row = connection.execute(
                    "SELECT depth,predecessor FROM forward_seen WHERE node=?",
                    (current,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("双向 BFS 的正向 predecessor 状态不完整")
                depth = int(row[0])
                predecessor = str(row[1]) if row[1] is not None else None
                connection.execute(
                    "INSERT OR REPLACE INTO distances(node,distance,predecessor) "
                    "VALUES (?,?,?)",
                    (current, float(depth), predecessor),
                )
                if current == source:
                    break
                if predecessor is None or depth <= 0:
                    raise RuntimeError("双向 BFS 的正向 predecessor 链无效")
                current = predecessor

            # Follow backward successors from the meeting point to the target
            # and invert them into the predecessor representation expected by
            # the common result writer.
            current = meeting
            source_distance = int(
                connection.execute(
                    "SELECT depth FROM forward_seen WHERE node=?", (meeting,)
                ).fetchone()[0]
            )
            while current != target:
                row = connection.execute(
                    "SELECT depth,successor FROM backward_seen WHERE node=?",
                    (current,),
                ).fetchone()
                if row is None or row[1] is None or int(row[0]) <= 0:
                    raise RuntimeError("双向 BFS 的反向 successor 链无效")
                successor = str(row[1])
                source_distance += 1
                connection.execute(
                    "INSERT OR REPLACE INTO distances(node,distance,predecessor) "
                    "VALUES (?,?,?)",
                    (successor, float(source_distance), current),
                )
                current = successor

        connection.execute(
            """
            INSERT INTO search_stats(
                strategy,forward_visited,backward_visited,
                forward_depth,backward_depth
            ) VALUES (?,?,?,?,?)
            """,
            (
                "bidirectional_bfs",
                forward_visited,
                backward_visited,
                forward_depth,
                backward_depth,
            ),
        )
        connection.commit()
        return connection

    @staticmethod
    def _reconstruct(
        connection: sqlite3.Connection, source: str, target: str
    ) -> int:
        """Materialise predecessor order in SQLite and return its node count.

        A predecessor chain may itself contain O(V) nodes.  Keeping the chain
        and a Python ``seen`` set in the control process defeats the external-
        memory route, so both order and cycle detection live in a TEMP table.
        ``reverse_position=0`` is the target; descending order is source to
        target.
        """
        connection.executescript(
            """
            DROP TABLE IF EXISTS temp.reconstructed_path;
            CREATE TEMP TABLE reconstructed_path(
                reverse_position INTEGER PRIMARY KEY,
                node TEXT NOT NULL UNIQUE
            );
            """
        )
        current: str | None = target
        position = 0
        found_source = False
        while current is not None:
            try:
                connection.execute(
                    "INSERT INTO reconstructed_path(reverse_position,node) VALUES (?,?)",
                    (position, current),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("最短路 predecessor 状态出现环") from exc
            if current == source:
                found_source = True
                break
            row = connection.execute(
                "SELECT predecessor FROM distances WHERE node=?", (current,)
            ).fetchone()
            current = str(row[0]) if row and row[0] is not None else None
            position += 1
        if not found_source:
            connection.execute("DELETE FROM reconstructed_path")
            return 0
        return position + 1

    @staticmethod
    def _iter_reconstructed(connection: sqlite3.Connection) -> Iterable[str]:
        for (node,) in connection.execute(
            "SELECT node FROM reconstructed_path ORDER BY reverse_position DESC"
        ):
            yield str(node)

    def _reconstruct_list(
        self, connection: sqlite3.Connection, source: str, target: str
    ) -> list[str]:
        """Materialise a path only for algorithms (such as Yen) that need it."""
        if self._reconstruct(connection, source, target) == 0:
            return []
        return list(self._iter_reconstructed(connection))

    def _edge_weight(self, source: str, target: str) -> float:
        weight = self.graph.edge_weight(source, target)
        if weight is None and not self.graph.directed:
            weight = self.graph.edge_weight(source, target, undirected=True)
        return float(weight if weight is not None else 1.0)

    def _path_payload(
        self, path: Iterable[str], *, weighted: bool
    ) -> dict[str, Any]:
        """Build a bounded control-plane representation of a complete path."""
        nodes_preview: list[str] = []
        edges_preview: list[dict[str, Any]] = []
        total = 0.0
        node_count = 0
        previous: str | None = None
        for raw_node in path:
            node = str(raw_node)
            if len(nodes_preview) < _SEQUENCE_PREVIEW_LIMIT:
                nodes_preview.append(self._preview_node(node))
            node_count += 1
            if previous is not None:
                actual = self._edge_weight(previous, node)
                total += actual if weighted else 1.0
                if len(edges_preview) < _SEQUENCE_PREVIEW_LIMIT:
                    edges_preview.append(
                        {
                            "from": self._preview_node(previous),
                            "to": self._preview_node(node),
                            "weight": actual,
                        }
                    )
            previous = node
        edge_count = max(0, node_count - 1)
        return {
            # Keep the established keys exact for ordinary small paths.  For
            # a long path they become explicitly marked previews.
            "path": nodes_preview,
            "path_preview": nodes_preview,
            "path_nodes_total": node_count,
            "path_truncated": node_count > len(nodes_preview),
            "hop_count": edge_count,
            "total_length": total,
            "edges": edges_preview,
            "edges_preview": edges_preview,
            "edges_total": edge_count,
            "edges_truncated": edge_count > len(edges_preview),
            "weighted": weighted,
        }

    def _path_rows(
        self,
        path: Iterable[str],
        *,
        path_index: int = 0,
        node_count: int | None = None,
    ) -> Iterable[dict[str, Any]]:
        """Yield scalar rows from which the complete ordered path is recoverable."""
        previous: str | None = None
        for position, raw_node in enumerate(path):
            node = str(raw_node)
            row: dict[str, Any] = {
                "kind": "path_step",
                "path_index": int(path_index),
                "position": position,
                "node_id": node,
                "node": self._display(node),
            }
            if node_count is not None:
                row["path_nodes_total"] = int(node_count)
            if previous is not None:
                row.update(
                    {
                        "from_id": previous,
                        "from": self._display(previous),
                        "to_id": node,
                        "to": self._display(node),
                        "weight": self._edge_weight(previous, node),
                    }
                )
            previous = node
            yield row

    def shortest_path(self, p: dict[str, Any]) -> dict[str, Any]:
        source, error = self._required_node(p.get("source"), "起点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert source is not None
        weighted = bool(p.get("use_weight", True))
        target_raw = str(p.get("target", ""))
        if target_raw == "all":
            self._emit(
                "algorithm",
                0.15,
                "执行外存最短路",
                strategy="disk_dijkstra" if weighted else "disk_bfs",
            )
            connection = self._shortest_state(
                source, weighted=weighted, stem="shortest-all"
            )
            try:
                reachable = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM distances WHERE node<>?", (source,)
                    ).fetchone()[0]
                )
                aggregate = connection.execute(
                    "SELECT AVG(distance),MIN(distance),MAX(distance) FROM distances WHERE node<>?",
                    (source,),
                ).fetchone()
                limit = _limit(p, _PATH_LIMIT)
                selected_targets = [
                    (str(target), float(distance))
                    for target, distance in connection.execute(
                        "SELECT node,distance FROM distances WHERE node<>? ORDER BY distance,node LIMIT ?",
                        (source, limit),
                    )
                ]
                nearest: list[dict[str, Any]] = []
                rows = (
                    {"target": self._display(str(node)), "distance": float(distance)}
                    for node, distance in connection.execute(
                        "SELECT node,distance FROM distances WHERE node<>? ORDER BY distance,node",
                        (source,),
                    )
                )
                result_files, _, _ = self._write_rows("shortest-distances", rows)

                def nearest_path_rows() -> Iterable[dict[str, Any]]:
                    for path_index, (target, distance) in enumerate(selected_targets):
                        node_count = self._reconstruct(connection, source, target)
                        payload = self._path_payload(
                            self._iter_reconstructed(connection), weighted=weighted
                        )
                        if len(nearest) < _SEQUENCE_PREVIEW_LIMIT:
                            nearest.append(
                                {
                                    "target": self._preview_node(target),
                                    "distance": distance,
                                    "path": payload["path"],
                                    "path_nodes_total": node_count,
                                    "path_truncated": payload["path_truncated"],
                                }
                            )
                        yield from self._path_rows(
                            self._iter_reconstructed(connection),
                            path_index=path_index,
                            node_count=node_count,
                        )

                path_files, _, path_row_count = self._write_rows(
                    "nearest-shortest-paths", nearest_path_rows()
                )
                result_files.extend(path_files)
                summary = {
                    "success": True, "found": reachable > 0,
                    "source": self._preview_node(source), "weighted": weighted,
                    "reachable_count": reachable,
                    "avg_distance": round(float(aggregate[0] or 0), 4),
                    "min_distance": round(float(aggregate[1] or 0), 4),
                    "max_distance": round(float(aggregate[2] or 0), 4),
                    "nearest_paths": nearest,
                    "nearest_paths_returned": len(selected_targets),
                    "nearest_paths_preview_truncated": len(selected_targets) > len(nearest),
                    "nearest_path_artifact_rows": path_row_count,
                    "truncated": reachable > len(selected_targets),
                }
                return {
                    "summary": summary,
                    "preview": nearest,
                    "result_files": result_files,
                }
            finally:
                connection.close()

        target, error = self._required_node(target_raw, "终点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert target is not None
        if str(p.get("algorithm", "default")) == "yen_k_shortest":
            self._emit("algorithm", 0.15, "执行 Yen K 最短路")
            return self._yen_k_shortest(
                source, target, weighted, max(1, min(int(p.get("k", 3)), _MAX_LIMIT))
            )
        use_bidirectional_bfs = not weighted or not self.graph.has_weights
        if use_bidirectional_bfs:
            self._emit(
                "algorithm",
                0.72,
                "执行双向 BFS",
                strategy="bidirectional_bfs",
                unit_weight_fallback=weighted and not self.graph.has_weights,
            )
            connection = self._bidirectional_bfs_state(
                source, target, stem="shortest-pair"
            )
            stats_row = connection.execute(
                """
                SELECT strategy,forward_visited,backward_visited,
                       forward_depth,backward_depth
                FROM search_stats LIMIT 1
                """
            ).fetchone()
            search_metadata: dict[str, Any] = {
                "execution_strategy": str(stats_row[0]),
                "forward_visited": int(stats_row[1]),
                "backward_visited": int(stats_row[2]),
                "forward_depth": int(stats_row[3]),
                "backward_depth": int(stats_row[4]),
                "unit_weight_fallback": weighted and not self.graph.has_weights,
            }
        else:
            self._emit(
                "algorithm", 0.15, "执行外存 Dijkstra", strategy="disk_dijkstra"
            )
            connection = self._shortest_state(
                source, weighted=True, target=target, stem="shortest-pair"
            )
            search_metadata = {
                "execution_strategy": "disk_dijkstra",
                "unit_weight_fallback": False,
            }
        try:
            row = connection.execute(
                "SELECT distance FROM distances WHERE node=?", (target,)
            ).fetchone()
            if row is None:
                summary = {
                    "success": True, "found": False,
                    "message": f"从 '{self._display(source)}' 到 '{self._display(target)}' 不存在可达路径",
                    **search_metadata,
                }
                result_files: list[str] = []
                preview: list[dict[str, Any]] = []
            else:
                node_count = self._reconstruct(connection, source, target)
                payload = self._path_payload(
                    self._iter_reconstructed(connection), weighted=weighted
                )
                summary = {
                    "success": True,
                    "found": True,
                    "source": self._preview_node(source),
                    "target": self._preview_node(target),
                    **payload,
                    **search_metadata,
                }
                result_files, _, _ = self._write_rows(
                    "shortest-path",
                    self._path_rows(
                        self._iter_reconstructed(connection), node_count=node_count
                    ),
                )
                preview = list(payload["edges_preview"])
            return {"summary": summary, "preview": preview, "result_files": result_files}
        finally:
            connection.close()

    def _yen_k_shortest(self, source: str, target: str, weighted: bool, k: int) -> dict[str, Any]:
        """Bounded Yen implementation; every spur search still uses disk state."""
        # Yen's spur searches are sequential and ``_shortest_state`` resets
        # its tables on every call.  One reusable database bounds temporary
        # storage to a single shortest-path state instead of O(k * path_len).
        state_stem = "yen-state"
        first_db = self._shortest_state(
            source, weighted=weighted, target=target, stem=state_stem
        )
        try:
            first = self._reconstruct_list(first_db, source, target)
        finally:
            first_db.close()
        if not first:
            summary = {"success": True, "found": False, "algorithm": "yen_k_shortest", "paths": []}
            return {"summary": summary, "preview": [], "result_files": []}
        try:
            state_limit = int(
                os.getenv("LARGE_GRAPH_YEN_MAX_PATH_STATE_BYTES", str(128 * 1024 * 1024))
            )
        except (TypeError, ValueError, OverflowError):
            state_limit = 128 * 1024 * 1024
        state_limit = max(1024, state_limit)

        def path_state_bytes(path: Iterable[str]) -> int:
            # Strings returned from SQLite are owned by this process; include
            # their encoded body plus list/tuple/hash-table reference overhead.
            return 128 + sum(64 + len(str(node).encode("utf-8")) for node in path)

        accepted_state = path_state_bytes(first)
        if accepted_state > state_limit:
            raise ValueError(
                "Yen 路径状态超过运行时内存上限；请改用 default 最短路、缩小图范围或提高 "
                "LARGE_GRAPH_YEN_MAX_PATH_STATE_BYTES"
            )
        accepted: list[list[str]] = [first]
        candidates: list[tuple[float, tuple[str, ...], list[str], int]] = []
        candidate_keys: set[tuple[str, ...]] = set()
        candidate_state = 0
        for iteration in range(1, k):
            previous = accepted[-1]
            for index in range(len(previous) - 1):
                root = previous[: index + 1]
                blocked_edges = {
                    (path[index], path[index + 1])
                    for path in accepted
                    if len(path) > index + 1
                    and all(path[offset] == root[offset] for offset in range(index + 1))
                }
                spur_db = self._shortest_state(
                    root[-1], weighted=weighted, target=target,
                    stem=state_stem,
                    blocked_nodes=set(root[:-1]), blocked_edges=blocked_edges,
                )
                try:
                    spur = self._reconstruct_list(spur_db, root[-1], target)
                finally:
                    spur_db.close()
                if not spur:
                    continue
                candidate = root[:-1] + spur
                key = tuple(candidate)
                if key in candidate_keys or candidate in accepted:
                    continue
                payload = self._path_payload(candidate, weighted=weighted)
                candidate_bytes = path_state_bytes(candidate)
                if accepted_state + candidate_state + candidate_bytes > state_limit:
                    raise ValueError(
                        "Yen 候选路径状态超过运行时内存上限；请减小 k、缩小图范围或提高 "
                        "LARGE_GRAPH_YEN_MAX_PATH_STATE_BYTES"
                    )
                heapq.heappush(
                    candidates,
                    (float(payload["total_length"]), key, candidate, candidate_bytes),
                )
                candidate_keys.add(key)
                candidate_state += candidate_bytes
            if not candidates:
                break
            _, key, selected, selected_bytes = heapq.heappop(candidates)
            candidate_keys.discard(key)
            candidate_state -= selected_bytes
            accepted.append(selected)
            accepted_state += selected_bytes
        # Paths left in the heap are not part of the answer. Release them
        # before the accepted paths are streamed to Artifact writers.
        candidates.clear()
        candidate_keys.clear()
        serialized: list[dict[str, Any]] = []
        for path in accepted[:_SEQUENCE_PREVIEW_LIMIT]:
            payload = self._path_payload(path, weighted=weighted)
            serialized.append(
                {
                    "path": payload["path"],
                    "total_length": payload["total_length"],
                    "path_nodes_total": payload["path_nodes_total"],
                    "path_truncated": payload["path_truncated"],
                }
            )

        def accepted_rows() -> Iterable[dict[str, Any]]:
            for path_index, path in enumerate(accepted):
                yield from self._path_rows(
                    path, path_index=path_index, node_count=len(path)
                )

        result_files, _, artifact_rows = self._write_rows(
            "yen-k-shortest-paths", accepted_rows()
        )
        summary = {
            "success": True, "found": bool(accepted), "algorithm": "yen_k_shortest",
            "paths": serialized, "k": k, "weighted": weighted,
            "count_returned": len(accepted),
            "paths_preview_truncated": len(accepted) > len(serialized),
            "path_artifact_rows": artifact_rows,
        }
        return {
            "summary": summary,
            "preview": serialized,
            "result_files": result_files,
        }

    def all_shortest_paths(self, p: dict[str, Any]) -> dict[str, Any]:
        source, error = self._required_node(p.get("source"), "起点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        target, error = self._required_node(p.get("target"), "终点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert source is not None and target is not None
        connection = self._shortest_state(
            source, weighted=True, stem="all-shortest", collect_all_predecessors=True
        )
        try:
            distance = connection.execute(
                "SELECT distance FROM distances WHERE node=?", (target,)
            ).fetchone()
            if distance is None:
                summary = {"success": True, "count_returned": 0, "paths": [], "message": "不存在路径"}
                return {"summary": summary, "preview": [], "result_files": []}
            cap = _limit(p, _PATH_LIMIT)
            # Keep the DFS frontier and cycle membership in SQLite.  A target
            # with millions of equal-distance predecessors must not create a
            # Python list for every branch before the first path is emitted.
            connection.executescript(
                """
                DROP TABLE IF EXISTS temp.path_frames;
                CREATE TEMP TABLE path_frames(
                    depth INTEGER PRIMARY KEY,
                    node TEXT NOT NULL UNIQUE,
                    after_predecessor TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO path_frames(depth,node,after_predecessor) VALUES (0,?,NULL)",
                (target,),
            )
            truncated = False
            displayed: list[list[str]] = []
            path_node_counts: list[int] = []
            returned = 0

            def current_path() -> Iterable[str]:
                for (node,) in connection.execute(
                    "SELECT node FROM path_frames ORDER BY depth DESC"
                ):
                    yield str(node)

            def path_rows() -> Iterable[dict[str, Any]]:
                nonlocal returned, truncated
                while True:
                    frame = connection.execute(
                        """SELECT depth,node,after_predecessor
                           FROM path_frames ORDER BY depth DESC LIMIT 1"""
                    ).fetchone()
                    if frame is None:
                        return
                    depth, node, after = int(frame[0]), str(frame[1]), frame[2]
                    if node == source:
                        if returned >= cap:
                            truncated = True
                            return
                        path_index = returned
                        returned += 1
                        if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                            preview = [
                                self._preview_node(str(row[0]))
                                for row in connection.execute(
                                    """SELECT node FROM path_frames
                                       ORDER BY depth DESC LIMIT ?""",
                                    (_SEQUENCE_PREVIEW_LIMIT,),
                                )
                            ]
                            node_count = depth + 1
                            displayed.append(preview)
                            path_node_counts.append(node_count)
                        yield from self._path_rows(
                            current_path(), path_index=path_index, node_count=depth + 1
                        )
                        connection.execute(
                            "DELETE FROM path_frames WHERE depth=?", (depth,)
                        )
                        continue
                    predecessor = connection.execute(
                        """SELECT predecessor FROM predecessors
                           WHERE node=?
                             AND (? IS NULL OR predecessor>?)
                           ORDER BY predecessor LIMIT 1""",
                        (node, after, after),
                    ).fetchone()
                    if predecessor is None:
                        connection.execute(
                            "DELETE FROM path_frames WHERE depth=?", (depth,)
                        )
                        continue
                    pred = str(predecessor[0])
                    connection.execute(
                        "UPDATE path_frames SET after_predecessor=? WHERE depth=?",
                        (pred, depth),
                    )
                    if connection.execute(
                        "SELECT 1 FROM path_frames WHERE node=?", (pred,)
                    ).fetchone() is None:
                        connection.execute(
                            """INSERT INTO path_frames(depth,node,after_predecessor)
                               VALUES (?,?,NULL)""",
                            (depth + 1, pred),
                        )

            result_files, _, artifact_rows = self._write_rows(
                "all-shortest-paths", path_rows()
            )
            summary = {
                "success": True, "count_returned": returned,
                "length": float(distance[0]), "paths": displayed, "truncated": truncated,
                "path_node_counts_preview": path_node_counts,
                "paths_preview_truncated": (
                    returned > len(displayed)
                    or any(
                        count > len(path)
                        for count, path in zip(path_node_counts, displayed)
                    )
                ),
                "path_artifact_rows": artifact_rows,
                "note": "返回结果已达到大图路径预览上限，完整枚举被有界终止" if truncated else None,
            }
            return {
                "summary": summary,
                "preview": displayed,
                "result_files": result_files,
            }
        finally:
            connection.close()

    # ── 最大流 / 最小割（新增，残量状态落盘）────────────────────────

    def _negative_capacity_error(self) -> dict[str, Any] | None:
        """Apply the same explicit non-negative capacity rule as GraphCompute."""
        for _, _, weight, _ in self.graph.iter_edges():
            if weight < 0:
                return {"success": False, "error": "最大流/最小割不支持负容量"}
        return None

    def _flow_state(
        self, source: str, sink: str, *, undirected: bool = False, stem: str = "flow"
    ) -> tuple[sqlite3.Connection, float]:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS arcs;
            CREATE TABLE arcs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, target TEXT NOT NULL,
                capacity REAL NOT NULL, flow REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX arcs_source_idx ON arcs(source);
            CREATE INDEX arcs_target_idx ON arcs(target);
            """
        )
        batch: list[tuple[str, str, float]] = []
        for u, v, weight, _ in self.graph.iter_edges():
            if weight < 0:
                raise ValueError("最大流/最小割不支持负容量")
            batch.append((u, v, float(weight)))
            if undirected and u != v:
                batch.append((v, u, float(weight)))
            if len(batch) >= 10_000:
                connection.executemany(
                    "INSERT INTO arcs(source,target,capacity) VALUES (?,?,?)", batch
                )
                batch.clear()
        if batch:
            connection.executemany(
                "INSERT INTO arcs(source,target,capacity) VALUES (?,?,?)", batch
            )
        connection.commit()
        value = 0.0
        while True:
            connection.executescript(
                """
                DROP TABLE IF EXISTS flow_seen;
                DROP TABLE IF EXISTS flow_queue;
                CREATE TABLE flow_seen(
                    node TEXT PRIMARY KEY, arc_id INTEGER, direction INTEGER
                ) WITHOUT ROWID;
                CREATE TABLE flow_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT);
                """
            )
            connection.execute(
                "INSERT INTO flow_seen(node,arc_id,direction) VALUES (?,NULL,NULL)", (source,)
            )
            connection.execute("INSERT INTO flow_queue(node) VALUES (?)", (source,))
            found = False
            while True:
                queued = connection.execute(
                    "SELECT seq,node FROM flow_queue ORDER BY seq LIMIT 1"
                ).fetchone()
                if queued is None:
                    break
                connection.execute("DELETE FROM flow_queue WHERE seq=?", (int(queued[0]),))
                node = str(queued[1])
                for arc_id, neighbor, _ in connection.execute(
                    "SELECT id,target,capacity-flow FROM arcs WHERE source=? AND capacity-flow>0",
                    (node,),
                ):
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO flow_seen(node,arc_id,direction) VALUES (?,?,1)",
                        (str(neighbor), int(arc_id)),
                    ).rowcount
                    if inserted:
                        connection.execute("INSERT INTO flow_queue(node) VALUES (?)", (str(neighbor),))
                for arc_id, neighbor, _ in connection.execute(
                    "SELECT id,source,flow FROM arcs WHERE target=? AND flow>0", (node,)
                ):
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO flow_seen(node,arc_id,direction) VALUES (?,?,-1)",
                        (str(neighbor), int(arc_id)),
                    ).rowcount
                    if inserted:
                        connection.execute("INSERT INTO flow_queue(node) VALUES (?)", (str(neighbor),))
                if connection.execute(
                    "SELECT 1 FROM flow_seen WHERE node=?", (sink,)
                ).fetchone():
                    found = True
                    break
            if not found:
                connection.commit()
                break
            current = sink
            path: list[tuple[int, int]] = []
            bottleneck = math.inf
            while current != source:
                row = connection.execute(
                    "SELECT arc_id,direction FROM flow_seen WHERE node=?", (current,)
                ).fetchone()
                if row is None or row[0] is None:
                    raise RuntimeError("残量路径状态不完整")
                arc_id, direction = int(row[0]), int(row[1])
                arc = connection.execute(
                    "SELECT source,target,capacity,flow FROM arcs WHERE id=?", (arc_id,)
                ).fetchone()
                assert arc is not None
                residual = float(arc[2]) - float(arc[3]) if direction == 1 else float(arc[3])
                bottleneck = min(bottleneck, residual)
                path.append((arc_id, direction))
                current = str(arc[0] if direction == 1 else arc[1])
            for arc_id, direction in path:
                connection.execute(
                    "UPDATE arcs SET flow=flow+? WHERE id=?",
                    (bottleneck if direction == 1 else -bottleneck, arc_id),
                )
            value += bottleneck
            connection.commit()
        return connection, value

    def maximum_flow(self, p: dict[str, Any]) -> dict[str, Any]:
        source, error = self._required_node(p.get("source"), "源点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        sink, error = self._required_node(p.get("sink"), "汇点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert source is not None and sink is not None
        if source == sink:
            summary = {"success": False, "error": "源点与汇点不能相同"}
            return {"summary": summary, "preview": [], "result_files": []}
        capacity_error = self._negative_capacity_error()
        if capacity_error:
            return {"summary": capacity_error, "preview": [], "result_files": []}
        connection, value = self._flow_state(
            source, sink, undirected=not self.graph.directed
        )
        try:
            rows = (
                {"from": self._display(str(u)), "to": self._display(str(v)), "flow": round(float(flow), 4)}
                for u, v, flow in connection.execute(
                    """
                    SELECT source,target,SUM(flow) AS flow FROM arcs
                    WHERE flow>0 GROUP BY source,target ORDER BY flow DESC
                    """
                )
            )
            result_files, preview, count = self._write_rows("maximum-flow", rows)
            limit = _limit(p)
            summary = {
                "success": True, "max_flow_value": value, "total_nonzero_edges": count,
                "top_flow_edges": preview[:limit], "truncated": count > limit,
            }
            return {"summary": summary, "preview": preview[:limit], "result_files": result_files}
        finally:
            connection.close()

    def minimum_cut(self, p: dict[str, Any]) -> dict[str, Any]:
        source, error = self._required_node(p.get("source"), "源点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        sink, error = self._required_node(p.get("sink"), "汇点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert source is not None and sink is not None
        if source == sink:
            summary = {"success": False, "error": "源点与汇点不能相同"}
            return {"summary": summary, "preview": [], "result_files": []}
        capacity_error = self._negative_capacity_error()
        if capacity_error:
            return {"summary": capacity_error, "preview": [], "result_files": []}
        connection, value = self._flow_state(
            source, sink, undirected=not self.graph.directed, stem="minimum-cut"
        )
        try:
            # The final failed residual BFS is exactly the source side of the cut.
            connection.execute("CREATE INDEX IF NOT EXISTS flow_seen_node_idx ON flow_seen(node)")
            source_size = int(connection.execute("SELECT COUNT(*) FROM flow_seen").fetchone()[0])
            sink_size = self.graph.node_count - source_size
            smaller_is_source = source_size <= sink_size
            smaller_preview: list[str] = []
            for node in self.graph.iter_node_ids():
                is_source = connection.execute(
                    "SELECT 1 FROM flow_seen WHERE node=?", (node,)
                ).fetchone() is not None
                if is_source == smaller_is_source and len(smaller_preview) < _LIST_LIMIT:
                    smaller_preview.append(self._display(node))

            def cut_rows() -> Iterable[dict[str, Any]]:
                for u, v, weight, _ in self.graph.iter_edges():
                    u_source = connection.execute(
                        "SELECT 1 FROM flow_seen WHERE node=?", (u,)
                    ).fetchone() is not None
                    v_source = connection.execute(
                        "SELECT 1 FROM flow_seen WHERE node=?", (v,)
                    ).fetchone() is not None
                    if self.graph.directed:
                        if u_source and not v_source:
                            yield {
                                "from": self._display(u),
                                "to": self._display(v),
                                "weight": weight,
                            }
                    elif u_source != v_source:
                        # Undirected snapshots store one canonical orientation;
                        # orient the returned edge from the source-side set so
                        # a lexicographically reversed cut edge is not lost.
                        source_node, target_node = (u, v) if u_source else (v, u)
                        yield {
                            "from": self._display(source_node),
                            "to": self._display(target_node),
                            "weight": weight,
                        }

            result_files, cut_preview, cut_count = self._write_rows(
                "minimum-cut-edges", cut_rows()
            )
            summary = {
                "success": True, "min_cut_value": value,
                "source_partition_size": source_size, "sink_partition_size": sink_size,
                "smaller_partition_preview": smaller_preview,
                "smaller_partition_total": min(source_size, sink_size),
                "smaller_partition_truncated": min(source_size, sink_size) > len(smaller_preview),
                "cut_edges_preview": cut_preview, "cut_edges_total": cut_count,
                "cut_edges_truncated": cut_count > len(cut_preview),
            }
            return {"summary": summary, "preview": cut_preview, "result_files": result_files}
        finally:
            connection.close()

    # ── 连通分量 / 拓扑 / 生成树（新增）────────────────────────────

    def _component_state(
        self, stem: str, *, direction: str = "out", undirected: bool = False
    ) -> sqlite3.Connection:
        """Assign components with an on-disk DFS stack.

        For directed SCC this is the two-pass Kosaraju algorithm. Weak and
        undirected components use the same second-pass walker directly.
        """
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS visited;
            DROP TABLE IF EXISTS dfs_stack;
            DROP TABLE IF EXISTS finish_order;
            DROP TABLE IF EXISTS components;
            CREATE TABLE visited(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE dfs_stack(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL, expanded INTEGER NOT NULL
            );
            CREATE TABLE finish_order(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                node TEXT NOT NULL UNIQUE
            );
            CREATE TABLE components(
                node TEXT PRIMARY KEY, component INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )

        def pop_stack() -> tuple[str, int] | None:
            row = connection.execute(
                "SELECT seq,node,expanded FROM dfs_stack ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM dfs_stack WHERE seq=?", (int(row[0]),))
            return str(row[1]), int(row[2])

        if self.graph.directed and not undirected:
            # First pass records reverse postorder without retaining a Python stack.
            for root in self.graph.iter_node_ids():
                if connection.execute(
                    "SELECT 1 FROM visited WHERE node=?", (root,)
                ).fetchone():
                    continue
                connection.execute(
                    "INSERT INTO dfs_stack(node,expanded) VALUES (?,0)", (root,)
                )
                while True:
                    item = pop_stack()
                    if item is None:
                        break
                    node, expanded = item
                    if expanded:
                        connection.execute(
                            "INSERT OR IGNORE INTO finish_order(node) VALUES (?)", (node,)
                        )
                        continue
                    inserted = connection.execute(
                        "INSERT OR IGNORE INTO visited(node) VALUES (?)", (node,)
                    ).rowcount
                    if not inserted:
                        continue
                    connection.execute(
                        "INSERT INTO dfs_stack(node,expanded) VALUES (?,1)", (node,)
                    )
                    for neighbor, _ in self.graph.iter_neighbors(node, direction=direction):
                        if not connection.execute(
                            "SELECT 1 FROM visited WHERE node=?", (neighbor,)
                        ).fetchone():
                            connection.execute(
                                "INSERT INTO dfs_stack(node,expanded) VALUES (?,0)",
                                (neighbor,),
                            )
                connection.commit()
            roots: Iterable[str] = (
                str(row[0])
                for row in connection.execute(
                    "SELECT node FROM finish_order ORDER BY seq DESC"
                )
            )
            walk_direction = "in" if direction == "out" else "out"
        else:
            roots = self.graph.iter_node_ids()
            walk_direction = "both"

        connection.execute("DELETE FROM visited")
        component = 0
        for root in roots:
            if connection.execute(
                "SELECT 1 FROM visited WHERE node=?", (root,)
            ).fetchone():
                continue
            connection.execute(
                "INSERT INTO dfs_stack(node,expanded) VALUES (?,0)", (root,)
            )
            while True:
                item = pop_stack()
                if item is None:
                    break
                node, _ = item
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO visited(node) VALUES (?)", (node,)
                ).rowcount
                if not inserted:
                    continue
                connection.execute(
                    "INSERT INTO components(node,component) VALUES (?,?)",
                    (node, component),
                )
                for neighbor, _ in self.graph.iter_neighbors(
                    node, direction=walk_direction, undirected=undirected
                ):
                    if not connection.execute(
                        "SELECT 1 FROM visited WHERE node=?", (neighbor,)
                    ).fetchone():
                        connection.execute(
                            "INSERT INTO dfs_stack(node,expanded) VALUES (?,0)",
                            (neighbor,),
                        )
            component += 1
            connection.commit()
        connection.execute("CREATE INDEX components_id_idx ON components(component,node)")
        connection.commit()
        return connection

    def _component_summary(
        self, connection: sqlite3.Connection, *, limit: int = 5
    ) -> dict[str, Any]:
        groups = list(
            connection.execute(
                """
                SELECT component,COUNT(*) AS size
                FROM components GROUP BY component
                ORDER BY size DESC,component LIMIT ?
                """,
                (limit,),
            )
        )
        total = int(
            connection.execute(
                "SELECT COUNT(DISTINCT component) FROM components"
            ).fetchone()[0]
        )
        previews: list[dict[str, Any]] = []
        for component, size in groups:
            names = [
                self._display(str(row[0]))
                for row in connection.execute(
                    "SELECT node FROM components WHERE component=? ORDER BY node LIMIT 10",
                    (int(component),),
                )
            ]
            previews.append(
                {
                    "size": int(size),
                    "nodes_preview": names,
                    "nodes_truncated": int(size) > len(names),
                }
            )
        return {
            "count": total,
            "largest_size": int(groups[0][1]) if groups else 0,
            "components": previews,
            "truncated": total > len(groups),
        }

    def graph_connectivity(self, p: dict[str, Any]) -> dict[str, Any]:
        limit = _limit(p, 5)
        if self.graph.directed:
            scc = self._component_state("scc")
            wcc = self._component_state("wcc", undirected=True)
            try:
                scc_summary = self._component_summary(scc, limit=limit)
                wcc_summary = self._component_summary(wcc, limit=limit)

                def rows() -> Iterable[dict[str, Any]]:
                    for node in self.graph.iter_node_ids():
                        strong = scc.execute(
                            "SELECT component FROM components WHERE node=?", (node,)
                        ).fetchone()
                        weak = wcc.execute(
                            "SELECT component FROM components WHERE node=?", (node,)
                        ).fetchone()
                        yield {
                            "node_id": node, "node": self._display(node),
                            "strong_component": int(strong[0]) if strong else None,
                            "weak_component": int(weak[0]) if weak else None,
                        }

                result_files, preview, _ = self._write_rows("components", rows())
                summary = {
                    "success": True, "directed": True,
                    "is_strongly_connected": scc_summary["count"] == 1,
                    "is_weakly_connected": wcc_summary["count"] == 1,
                    "strongly_connected_components": scc_summary,
                    "weakly_connected_components": wcc_summary,
                }
                return {"summary": summary, "preview": preview, "result_files": result_files}
            finally:
                scc.close()
                wcc.close()
        components = self._component_state("components", undirected=True)
        try:
            component_summary = self._component_summary(components, limit=limit)
            rows = (
                {
                    "node_id": str(node), "node": self._display(str(node)),
                    "component": int(component),
                }
                for node, component in components.execute(
                    "SELECT node,component FROM components ORDER BY component,node"
                )
            )
            result_files, preview, _ = self._write_rows("components", rows)
            summary = {
                "success": True, "directed": False,
                "is_connected": component_summary["count"] == 1,
                "connected_components": component_summary,
            }
            return {"summary": summary, "preview": preview, "result_files": result_files}
        finally:
            components.close()

    def topological_sort(self, p: dict[str, Any]) -> dict[str, Any]:
        if not self.graph.directed:
            summary = {
                "success": False, "is_dag": False,
                "message": "拓扑排序仅适用于有向图，当前图为无向图",
            }
            return {"summary": summary, "preview": [], "result_files": []}
        connection = self._connection("topological")
        connection.executescript(
            """
            CREATE TABLE indegree(node TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE ready(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT UNIQUE);
            CREATE TABLE ordering(position INTEGER PRIMARY KEY,node TEXT NOT NULL);
            """
        )
        connection.executemany(
            "INSERT INTO indegree(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for _, target, _, _ in self.graph.iter_edges():
            connection.execute(
                "UPDATE indegree SET value=value+1 WHERE node=?", (target,)
            )
        connection.execute(
            "INSERT INTO ready(node) SELECT node FROM indegree WHERE value=0 ORDER BY node"
        )
        position = 0
        while True:
            row = connection.execute(
                "SELECT seq,node FROM ready ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM ready WHERE seq=?", (int(row[0]),))
            node = str(row[1])
            connection.execute(
                "INSERT INTO ordering(position,node) VALUES (?,?)", (position, node)
            )
            position += 1
            for neighbor, _ in self.graph.iter_out_edges(node):
                connection.execute(
                    "UPDATE indegree SET value=value-1 WHERE node=?", (neighbor,)
                )
                value = connection.execute(
                    "SELECT value FROM indegree WHERE node=?", (neighbor,)
                ).fetchone()
                if value and int(value[0]) == 0:
                    connection.execute(
                        "INSERT OR IGNORE INTO ready(node) VALUES (?)", (neighbor,)
                    )
            connection.commit()
        if position != self.graph.node_count:
            # Preserve the small-path diagnostic without retaining several
            # complete O(V)-length cycles in the control plane.  Each full
            # cycle is released as soon as its fixed-size preview is built.
            sample_cycles: list[list[str]] = []
            sample_cycle_lengths: list[int] = []
            for cycle in self._iter_cycles():
                preview, cycle_length = self._sequence_preview(cycle)
                sample_cycles.append(preview)
                sample_cycle_lengths.append(cycle_length)
                if len(sample_cycles) >= 5:
                    break
            summary = {
                "success": False, "is_dag": False,
                "message": "图中存在环，无法进行拓扑排序",
                "sample_cycles": sample_cycles,
                "sample_cycle_lengths": sample_cycle_lengths,
            }
            connection.close()
            return {"summary": summary, "preview": [], "result_files": []}
        result_files, preview_rows, count = self._write_rows(
            "topological-order",
            (
                {"position": int(pos), "node": self._display(str(node))}
                for pos, node in connection.execute(
                    "SELECT position,node FROM ordering ORDER BY position"
                )
            ),
        )
        limit = _limit(p)
        preview = [row["node"] for row in preview_rows[:limit]]
        summary = {
            "success": True, "is_dag": True, "total_nodes": count,
            "topological_order_preview": preview, "truncated": count > len(preview),
            "note": f"仅展示前 {len(preview)} 个节点，共 {count} 个" if count > len(preview) else None,
        }
        connection.close()
        return {"summary": summary, "preview": preview_rows, "result_files": result_files}

    def _union_find(self, connection: sqlite3.Connection) -> tuple[Callable[[str], str], Callable[[str, str], bool]]:
        connection.execute(
            "CREATE TABLE parent(node TEXT PRIMARY KEY,parent TEXT NOT NULL,rank INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO parent(node,parent,rank) VALUES (?,?,0)",
            ((node, node) for node in self.graph.iter_node_ids()),
        )

        def find(node: str) -> str:
            path: list[str] = []
            current = node
            while True:
                row = connection.execute(
                    "SELECT parent FROM parent WHERE node=?", (current,)
                ).fetchone()
                if row is None or str(row[0]) == current:
                    root = current
                    break
                path.append(current)
                current = str(row[0])
            connection.executemany(
                "UPDATE parent SET parent=? WHERE node=?",
                ((root, child) for child in path),
            )
            return root

        def union(left: str, right: str) -> bool:
            a, b = find(left), find(right)
            if a == b:
                return False
            rank_a = int(
                connection.execute("SELECT rank FROM parent WHERE node=?", (a,)).fetchone()[0]
            )
            rank_b = int(
                connection.execute("SELECT rank FROM parent WHERE node=?", (b,)).fetchone()[0]
            )
            if rank_a < rank_b:
                a, b = b, a
                rank_a, rank_b = rank_b, rank_a
            connection.execute("UPDATE parent SET parent=? WHERE node=?", (a, b))
            if rank_a == rank_b:
                connection.execute("UPDATE parent SET rank=rank+1 WHERE node=?", (a,))
            return True

        return find, union

    def minimum_spanning_tree(self, p: dict[str, Any]) -> dict[str, Any]:
        connection = self._connection("mst")
        connection.execute(
            """
            CREATE TABLE edges(
                source TEXT NOT NULL,target TEXT NOT NULL,weight REAL NOT NULL,
                PRIMARY KEY(source,target)
            ) WITHOUT ROWID
            """
        )
        for source, target, weight, _ in self.graph.iter_edges():
            if source == target:
                continue
            left, right = sorted((source, target))
            connection.execute(
                """
                INSERT INTO edges(source,target,weight) VALUES (?,?,?)
                ON CONFLICT(source,target) DO UPDATE SET weight=MIN(weight,excluded.weight)
                """,
                (left, right, float(weight)),
            )
        find, union = self._union_find(connection)
        edge_count = 0
        total_weight = 0.0
        # Added: route MST edges through the common partitioned Artifact
        # writer instead of leaving this otherwise-global result in one file.
        def forest_rows() -> Iterable[dict[str, Any]]:
            nonlocal edge_count, total_weight
            for source, target, weight in connection.execute(
                "SELECT source,target,weight FROM edges ORDER BY weight,source,target"
            ):
                if not union(str(source), str(target)):
                    continue
                row = {
                    "from": self._display(str(source)), "to": self._display(str(target)),
                    "weight": round(float(weight), 4),
                }
                edge_count += 1
                total_weight += float(weight)
                yield row

        result_files, preview, _ = self._write_rows("minimum-spanning-forest", forest_rows())
        # Keep component-root cardinality on disk; an edgeless billion-node
        # graph must not create a billion-entry Python set.
        connection.execute(
            "CREATE TABLE roots(node TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        for node in self.graph.iter_node_ids():
            connection.execute(
                "INSERT OR IGNORE INTO roots(node) VALUES (?)", (find(node),)
            )
        component_count = int(
            connection.execute("SELECT COUNT(*) FROM roots").fetchone()[0]
        )
        connection.commit()
        connection.close()
        connected = component_count == 1
        summary = {
            "success": True, "connected": connected, "execution_mode": "exact",
            "total_weight": round(total_weight, 4), "num_edges": edge_count,
            "mst_edges_preview": preview[: _limit(p)], "truncated": edge_count > len(preview),
        }
        if not connected:
            summary.update(
                {
                    "num_components": component_count,
                    "note": "图不连通，结果 Artifact 为完整最小生成森林",
                }
            )
        return {
            "summary": summary, "preview": preview,
            "result_files": result_files,
        }

    def global_minimum_cut(self, p: dict[str, Any]) -> dict[str, Any]:
        if self.graph.node_count < 2:
            summary = {"success": False, "error": "全局最小割至少需要 2 个节点。"}
            return {"summary": summary, "preview": [], "result_files": []}
        treat_as_undirected = bool(p.get("treat_as_undirected", True))
        if self.graph.directed and not treat_as_undirected:
            summary = {
                "success": False,
                "error": "当前仅支持无向全局最小割；有向图请设置 treat_as_undirected=true。",
            }
            return {"summary": summary, "preview": [], "result_files": []}
        capacity_error = self._negative_capacity_error()
        if capacity_error:
            return {"summary": capacity_error, "preview": [], "result_files": []}
        iterator = self.graph.iter_node_ids()
        root = next(iterator)
        best_value = math.inf
        best_side_path = self.work_root / "global-cut-side.jsonl"
        algorithm = "rooted_repeated_min_cut"
        best_worker = math.inf
        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        # A zero-capacity cut does not imply that the graph is structurally
        # disconnected.  Determine topology connectivity independently of
        # capacities and reuse the on-disk component state to select the same
        # first-component partition as the small NetworkX path.
        components = self._component_state(
            "global-cut-components", undirected=True
        )
        try:
            component_count = int(
                components.execute(
                    "SELECT COUNT(DISTINCT component) FROM components"
                ).fetchone()[0]
            )
            structurally_disconnected = component_count > 1
            if structurally_disconnected:
                algorithm = "connected_components"
                best_value = 0.0
                root_component_row = components.execute(
                    "SELECT component FROM components WHERE node=?", (root,)
                ).fetchone()
                assert root_component_row is not None
                with best_side_path.open("w", encoding="utf-8") as output:
                    for (node,) in components.execute(
                        "SELECT node FROM components WHERE component=? ORDER BY node",
                        (int(root_component_row[0]),),
                    ):
                        output.write(json.dumps(str(node), ensure_ascii=False) + "\n")
        finally:
            components.close()

        if not structurally_disconnected:
            if parallel_workers > 1:
                # Root-to-sink cuts are independent.  Each worker keeps its
                # best reachable side in a private file; the parent only
                # reduces one scalar and one side-file reference.
                args = [
                    (
                        str(self.graph.manifest_path),
                        root,
                        worker_index,
                        parallel_workers,
                        str(self.work_root),
                    )
                    for worker_index in range(parallel_workers)
                ]
                with ProcessPoolExecutor(
                    max_workers=parallel_workers, mp_context=process_context()
                ) as pool:
                    futures = [pool.submit(_global_cut_sink_partition, item) for item in args]
                    for completed, future in enumerate(as_completed(futures), 1):
                        result = future.result()
                        value = float(result["best_value"])
                        worker_index = int(result["worker"])
                        if (
                            value < best_value
                            or (value == best_value and worker_index < best_worker)
                        ) and result.get("best_side_path"):
                            best_value = value
                            best_worker = worker_index
                            Path(str(result["best_side_path"])).replace(best_side_path)
                        self._emit(
                            "algorithm_map",
                            0.20 + 0.55 * completed / len(futures),
                            "全局最小割源点分批计算中",
                            workers=parallel_workers,
                            workers_completed=completed,
                        )
            else:
                for sink in iterator:
                    flow, value = self._flow_state(
                        root, sink, undirected=True, stem="global-cut-flow"
                    )
                    try:
                        if value < best_value:
                            best_value = value
                            best_worker = 0
                            with best_side_path.open("w", encoding="utf-8") as output:
                                for (node,) in flow.execute(
                                    "SELECT node FROM flow_seen ORDER BY node"
                                ):
                                    output.write(
                                        json.dumps(str(node), ensure_ascii=False) + "\n"
                                    )
                    finally:
                        flow.close()
        side_db = self._connection("global-cut-best-side")
        side_db.execute(
            "CREATE TABLE side(node TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        if best_side_path.exists():
            with best_side_path.open("r", encoding="utf-8") as source:
                side_db.executemany(
                    "INSERT OR IGNORE INTO side(node) VALUES (?)",
                    ((str(json.loads(line)),) for line in source),
                )
        side_db.commit()
        side_size = int(side_db.execute("SELECT COUNT(*) FROM side").fetchone()[0])
        other_size = self.graph.node_count - side_size
        side_is_smaller = side_size <= other_size
        smaller_preview: list[str] = []
        if side_is_smaller:
            smaller_preview = [
                self._display(str(row[0]))
                for row in side_db.execute(
                    "SELECT node FROM side ORDER BY node LIMIT ?", (_limit(p),)
                )
            ]
        else:
            for node in self.graph.iter_node_ids():
                in_side = side_db.execute(
                    "SELECT 1 FROM side WHERE node=?", (node,)
                ).fetchone()
                if in_side is None and len(smaller_preview) < _limit(p):
                    smaller_preview.append(self._display(node))

        def cut_rows() -> Iterable[dict[str, Any]]:
            for u, v, weight, _ in self.graph.iter_edges():
                u_side = side_db.execute(
                    "SELECT 1 FROM side WHERE node=?", (u,)
                ).fetchone() is not None
                v_side = side_db.execute(
                    "SELECT 1 FROM side WHERE node=?", (v,)
                ).fetchone() is not None
                if u_side != v_side:
                    yield {"from": self._display(u), "to": self._display(v), "weight": weight}

        result_files, preview, cut_count = self._write_rows("global-minimum-cut", cut_rows())
        smaller_size = min(side_size, other_size)
        summary = {
            "success": True, "min_cut_value": round(float(best_value), 6),
            "algorithm": algorithm,
            "treat_as_undirected": treat_as_undirected,
            "execution_mode": "exact",
            "partition_a_size": side_size, "partition_b_size": other_size,
            "smaller_partition_preview": smaller_preview,
            "smaller_partition_total": smaller_size,
            "larger_partition_total": max(side_size, other_size),
            "smaller_partition_truncated": smaller_size > len(smaller_preview),
            "cut_edges_preview": preview, "cut_edges_total": cut_count,
            "cut_edges_truncated": cut_count > len(preview),
            "disconnected": structurally_disconnected,
            "parallel": (
                {"strategy": "component-scan", "workers": 1}
                if structurally_disconnected
                else {
                    "strategy": "sink-batch-map/min-cut-reduce",
                    "workers": parallel_workers,
                }
            ),
        }
        side_db.close()
        return {"summary": summary, "preview": preview, "result_files": result_files}

    # ── 度统计（新增）──────────────────────────────────────────────

    @staticmethod
    def _sql_stats(connection: sqlite3.Connection, column: str) -> dict[str, float]:
        count, mean, minimum, maximum = connection.execute(
            f"SELECT COUNT(*),AVG({column}),MIN({column}),MAX({column}) FROM degree"
        ).fetchone()
        count = int(count)
        if not count:
            return {"mean": 0, "max": 0, "min": 0, "median": 0}
        offset = (count - 1) // 2
        take = 2 if count % 2 == 0 else 1
        middle = [
            float(row[0])
            for row in connection.execute(
                f"SELECT {column} FROM degree ORDER BY {column} LIMIT ? OFFSET ?",
                (take, offset),
            )
        ]
        return {
            "mean": round(float(mean), 4), "max": float(maximum), "min": float(minimum),
            "median": round(sum(middle) / len(middle), 4),
        }

    def degree_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        connection = self._connection("degree-analysis")
        connection.execute(
            """
            CREATE TABLE degree(
                node TEXT PRIMARY KEY,in_degree INTEGER NOT NULL,
                out_degree INTEGER NOT NULL,total INTEGER NOT NULL,
                ordinal INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        parallel: dict[str, Any] | None = None
        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        if parallel_workers > 1:
            args = [
                (
                    str(self.graph.manifest_path),
                    worker_index,
                    parallel_workers,
                    str(self.work_root),
                )
                for worker_index in range(parallel_workers)
            ]
            results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=parallel_workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(_degree_analysis_node_partition, item)
                    for item in args
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    self._emit(
                        "algorithm_map",
                        0.20 + 0.50 * completed / len(futures),
                        "节点归属分片度统计进行中",
                        workers=parallel_workers,
                        workers_completed=completed,
                    )
            for result in sorted(results, key=lambda item: int(item["worker"])):
                batch: list[tuple[str, int, int, int, int]] = []
                with Path(str(result["output_path"])).open(
                    "r", encoding="utf-8"
                ) as source:
                    for line in source:
                        node, ordinal, incoming, outgoing, total = json.loads(line)
                        batch.append(
                            (
                                str(node),
                                int(incoming),
                                int(outgoing),
                                int(total),
                                int(ordinal),
                            )
                        )
                        if len(batch) >= 10_000:
                            connection.executemany(
                                """INSERT INTO degree(
                                       node,in_degree,out_degree,total,ordinal
                                   ) VALUES (?,?,?,?,?)""",
                                batch,
                            )
                            batch.clear()
                if batch:
                    connection.executemany(
                        """INSERT INTO degree(
                               node,in_degree,out_degree,total,ordinal
                           ) VALUES (?,?,?,?,?)""",
                        batch,
                    )
            parallel = {
                "strategy": "node-shard-map/degree-disk-reduce",
                "workers": parallel_workers,
                "node_count": sum(int(row["row_count"]) for row in results),
            }
        else:
            for ordinal, node in enumerate(self.graph.iter_node_ids()):
                incoming = (
                    self.graph.degree(node, direction="in")
                    if self.graph.directed
                    else 0
                )
                outgoing = (
                    self.graph.degree(node, direction="out")
                    if self.graph.directed
                    else 0
                )
                total = (
                    incoming + outgoing
                    if self.graph.directed
                    else self.graph.degree(node)
                )
                connection.execute(
                    """INSERT INTO degree(
                           node,in_degree,out_degree,total,ordinal
                       ) VALUES (?,?,?,?,?)""",
                    (node, incoming, outgoing, total, ordinal),
                )
        connection.commit()
        limit = _limit(p)
        rows = list(
            connection.execute(
                """SELECT node,in_degree,out_degree,total FROM degree
                   ORDER BY total DESC,ordinal LIMIT ?""",
                (limit,),
            )
        )
        top = [
            (
                {"node": self._display(str(node)), "in": int(inc), "out": int(out), "total": int(total)}
                if self.graph.directed
                else {"node": self._display(str(node)), "degree": int(total)}
            )
            for node, inc, out, total in rows
        ]
        node_count = int(connection.execute("SELECT COUNT(*) FROM degree").fetchone()[0])
        low_rows = list(
            connection.execute(
                """
                SELECT node,in_degree,out_degree,total FROM degree
                ORDER BY total DESC,ordinal LIMIT 10 OFFSET ?
                """,
                (max(0, node_count - 10),),
            )
        )
        low = [
            (
                {"node": self._display(str(node)), "in": int(inc), "out": int(out), "total": int(total)}
                if self.graph.directed
                else {"node": self._display(str(node)), "degree": int(total)}
            )
            for node, inc, out, total in low_rows
        ]
        result_files, preview, count = self._write_rows(
            "degree-analysis",
            (
                {
                    "node_id": str(node), "node": self._display(str(node)),
                    "in": int(inc), "out": int(out), "total": int(total),
                }
                for node, inc, out, total in connection.execute(
                    "SELECT node,in_degree,out_degree,total FROM degree ORDER BY node"
                )
            ),
        )
        if self.graph.directed:
            summary = {
                "success": True, "directed": True, "total_nodes": count,
                "in_degree_stats": self._sql_stats(connection, "in_degree"),
                "out_degree_stats": self._sql_stats(connection, "out_degree"),
                "total_degree_stats": self._sql_stats(connection, "total"),
                "avg_in_degree": round(self.graph.edge_count / max(1, count), 4),
                "avg_out_degree": round(self.graph.edge_count / max(1, count), 4),
                "top_degree_nodes": top, "low_degree_nodes": low,
                "truncated": count > len(top),
            }
        else:
            summary = {
                "success": True, "directed": False, "total_nodes": count,
                "degree_stats": self._sql_stats(connection, "total"),
                "avg_degree": round(2 * self.graph.edge_count / max(1, count), 4),
                "top_degree_nodes": top, "low_degree_nodes": low,
                "truncated": count > len(top),
            }
        if parallel is not None:
            summary["parallel"] = parallel
        connection.close()
        return {"summary": summary, "preview": preview, "result_files": result_files}

    # ── 社区与中心性（新增）────────────────────────────────────────

    def community_detection(self, p: dict[str, Any]) -> dict[str, Any]:
        requested = str(p.get("algorithm", "louvain")).strip().lower()
        available = {
            "louvain", "greedy_modularity", "label_propagation", "leiden",
            "girvan_newman", "asyn_fluidc", "label_propagation_asyn",
        }
        if requested not in available:
            summary = {
                "success": False, "error": f"未知算法 '{requested}'",
                "available": sorted(available),
            }
            return {"summary": summary, "preview": [], "result_files": []}

        raw_focus_nodes = p.get("focus_nodes") or []
        if not isinstance(raw_focus_nodes, list):
            summary = {"success": False, "error": "focus_nodes 必须为节点数组"}
            return {"summary": summary, "preview": [], "result_files": []}
        if len(raw_focus_nodes) > 200:
            summary = {"success": False, "error": "focus_nodes 最多允许 200 个节点"}
            return {"summary": summary, "preview": [], "result_files": []}
        focus_nodes: list[str] = []
        missing_focus_nodes: list[str] = []
        seen_focus_nodes: set[str] = set()
        for raw_node in raw_focus_nodes:
            requested_node = str(raw_node)
            resolved = self.graph.resolve_node(requested_node)
            if resolved is None:
                missing_focus_nodes.append(requested_node)
                continue
            resolved = str(resolved)
            if resolved not in seen_focus_nodes:
                seen_focus_nodes.add(resolved)
                focus_nodes.append(resolved)

        # 大图实现统一使用可分区迭代的加权标签传播内核。对小图目录中的
        # 每个算法名都显式标为 scalable approximation，避免伪装成
        # NetworkX 的精确实现或悄然截断子图。
        # Added: the disk kernel is a deterministic weighted synchronous
        # implementation, while both small-path label-propagation variants
        # have different weighting/scheduling semantics.  All named modes
        # are therefore reported as explicit scalable approximations.
        approximate = True
        connection = self._connection("communities")
        connection.executescript(
            """
            CREATE TABLE labels(node TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE next_labels(node TEXT PRIMARY KEY,label TEXT NOT NULL) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO labels(node,label) VALUES (?,?)",
            ((node, node) for node in self.graph.iter_node_ids()),
        )
        connection.commit()

        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        map_root = self.work_root / "community-label-map"
        node_root = map_root / "nodes"
        node_root.mkdir(parents=True, exist_ok=True)
        node_paths = [
            node_root / f"part-{worker:05d}.jsonl"
            for worker in range(parallel_workers)
        ]
        node_counts = [0 for _ in range(parallel_workers)]
        # Open incrementally so a failure on a later shard cannot leak the
        # descriptors already acquired by the controller.
        node_outputs: list[Any] = []
        try:
            for path in node_paths:
                node_outputs.append(path.open("w", encoding="utf-8"))
            for node in self.graph.iter_node_ids():
                worker = _stable_partition(node, parallel_workers)
                node_outputs[worker].write(
                    json.dumps(node, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                node_counts[worker] += 1
        finally:
            for output in node_outputs:
                output.close()

        try:
            batch_rows = int(os.getenv("LARGE_GRAPH_REDUCE_BATCH_ROWS", "4096"))
        except (TypeError, ValueError, OverflowError):
            batch_rows = 4096
        batch_rows = max(128, min(batch_rows, 65_536))
        try:
            default_iterations = int(
                os.getenv("LARGE_GRAPH_COMMUNITY_DEFAULT_MAX_ITERATIONS", "5")
            )
        except (TypeError, ValueError, OverflowError):
            default_iterations = 5
        max_iterations = max(
            1,
            min(int(p.get("max_iterations", default_iterations)), 500),
        )
        iterations = 0
        pool = (
            ProcessPoolExecutor(
                max_workers=parallel_workers, mp_context=process_context()
            )
            if parallel_workers > 1
            else None
        )
        try:
            for iteration in range(max_iterations):
                # Every worker reads this committed labels table while the
                # controller performs no writes.  Only after every map shard
                # closes its read transaction does the SQLite reduce begin.
                connection.commit()
                items = [
                    (
                        str(self.graph.manifest_path),
                        str(self.work_root / "communities.sqlite3"),
                        str(node_paths[worker]),
                        worker,
                        iteration + 1,
                        str(map_root),
                    )
                    for worker in range(parallel_workers)
                ]
                if pool is None:
                    map_results = [_community_label_node_partition(items[0])]
                else:
                    map_results = []
                    futures = [
                        pool.submit(_community_label_node_partition, item)
                        for item in items
                    ]
                    for completed, future in enumerate(as_completed(futures), 1):
                        map_results.append(future.result())
                        self._emit(
                            "algorithm_map",
                            0.15
                            + 0.60
                            * (iteration + completed / parallel_workers)
                            / max_iterations,
                            "社区标签节点分区计算中",
                            iteration=iteration + 1,
                            workers=parallel_workers,
                            workers_completed=completed,
                        )
                map_results.sort(key=lambda item: int(item["worker"]))
                if len(map_results) != parallel_workers:
                    raise RuntimeError("社区标签 map 分区数量与计划不一致")

                connection.execute("DELETE FROM next_labels")
                changed = 0
                reduced_rows = 0
                for result in map_results:
                    worker = int(result["worker"])
                    if worker < 0 or worker >= parallel_workers:
                        raise RuntimeError("社区标签 map 返回了无效 worker 编号")
                    if int(result["row_count"]) != node_counts[worker]:
                        raise RuntimeError("社区标签 map 分区节点数量与输入不一致")
                    worker_changed = 0
                    worker_rows = 0
                    batch: list[tuple[str, str]] = []
                    with Path(str(result["output_path"])).open(
                        "r", encoding="utf-8"
                    ) as source:
                        for line in source:
                            if not line.strip():
                                continue
                            node, label, row_changed = json.loads(line)
                            batch.append((str(node), str(label)))
                            worker_changed += int(row_changed)
                            worker_rows += 1
                            reduced_rows += 1
                            if len(batch) >= batch_rows:
                                connection.executemany(
                                    "INSERT INTO next_labels(node,label) VALUES (?,?)",
                                    batch,
                                )
                                batch.clear()
                        if batch:
                            connection.executemany(
                                "INSERT INTO next_labels(node,label) VALUES (?,?)",
                                batch,
                            )
                    if worker_rows != int(result["row_count"]):
                        raise RuntimeError("社区标签 map 输出行数与元数据不一致")
                    if worker_changed != int(result["changed"]):
                        raise RuntimeError("社区标签 map changed 汇总不一致")
                    changed += worker_changed
                next_count = int(
                    connection.execute("SELECT COUNT(*) FROM next_labels").fetchone()[0]
                )
                if reduced_rows != self.graph.node_count or next_count != reduced_rows:
                    raise RuntimeError("社区标签 SQLite reduce 未覆盖完整节点集合")
                connection.execute(
                    """
                    UPDATE labels SET label=(
                        SELECT label FROM next_labels
                        WHERE next_labels.node=labels.node
                    )
                    """
                )
                connection.commit()
                iterations = iteration + 1
                # Map output is consumed completely by this reduce. Retaining
                # every JSONL/vote database until job teardown would grow
                # temporary disk as O(iterations * V), contradicting both the
                # streaming architecture and resource admission estimate.
                # Remove the completed round before starting the next one.
                shutil.rmtree(
                    map_root / f"iteration-{iterations:05d}",
                    ignore_errors=False,
                )
                self._emit(
                    "algorithm_iterate",
                    0.15 + 0.65 * iterations / max_iterations,
                    "社区标签传播迭代",
                    iteration=iterations,
                    changed=changed,
                    workers=parallel_workers,
                )
                if not changed:
                    break
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        # The node primary key supports direct focus-node lookup.  A secondary
        # label index and compact size table make community representatives,
        # sizes and top-community summaries bounded lookups rather than one
        # complete 7M-node scan per requested node.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS labels_by_label_node ON labels(label,node)"
        )
        connection.execute(
            """
            CREATE TABLE community_sizes(
                label TEXT PRIMARY KEY,
                size INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        connection.execute(
            """
            INSERT INTO community_sizes(label,size)
            SELECT label,COUNT(*) FROM labels GROUP BY label
            """
        )
        connection.commit()

        limit = _limit(p, 5)
        groups = list(
            connection.execute(
                """
                SELECT label,size FROM community_sizes
                ORDER BY size DESC,label LIMIT ?
                """,
                (limit,),
            )
        )
        community_count = int(
            connection.execute("SELECT COUNT(*) FROM community_sizes").fetchone()[0]
        )
        size_aggregate = connection.execute(
            "SELECT MIN(size),MAX(size),AVG(size) FROM community_sizes"
        ).fetchone()
        median_offset = max(0, (community_count - 1) // 2)
        median_take = 2 if community_count and community_count % 2 == 0 else 1
        median_values = [
            float(row[0])
            for row in connection.execute(
                """
                SELECT size FROM community_sizes
                ORDER BY size LIMIT ? OFFSET ?
                """,
                (median_take, median_offset),
            )
        ]
        size_stats = {
            "min": float(size_aggregate[0] or 0),
            "max": float(size_aggregate[1] or 0),
            "mean": round(float(size_aggregate[2] or 0), 6),
            "median": round(sum(median_values) / len(median_values), 6)
            if median_values else 0.0,
        }
        # Added: compute modularity from the node-owned adjacency shards and
        # the persisted labels.  This restores the scalar reported by the
        # small-graph path without materialising a NetworkX graph.
        modularity = self._community_modularity(connection)
        communities: list[dict[str, Any]] = []
        for community_id, (label, size) in enumerate(groups[:limit]):
            nodes = [
                self._display(str(row[0]))
                for row in connection.execute(
                    "SELECT node FROM labels WHERE label=? ORDER BY node LIMIT 10",
                    (str(label),),
                )
            ]
            communities.append(
                {
                    "community_id": community_id, "size": int(size),
                    "nodes_preview": nodes, "nodes_truncated": int(size) > len(nodes),
                }
            )
        focus_node_communities: list[dict[str, Any]] = []
        representative_cache: dict[str, list[str]] = {}
        for node in focus_nodes:
            row = connection.execute(
                """
                SELECT labels.label,community_sizes.size
                FROM labels JOIN community_sizes
                  ON community_sizes.label=labels.label
                WHERE labels.node=?
                """,
                (node,),
            ).fetchone()
            if row is None:
                missing_focus_nodes.append(self._display(node))
                continue
            label, size = str(row[0]), int(row[1])
            if label not in representative_cache:
                representative_cache[label] = [
                    self._display(str(item[0]))
                    for item in connection.execute(
                        """
                        SELECT node FROM labels
                        WHERE label=? ORDER BY node LIMIT 10
                        """,
                        (label,),
                    )
                ]
            focus_node_communities.append(
                {
                    "node_id": node,
                    "node": self._display(node),
                    "community_id": label,
                    "community_size": size,
                    "representative_nodes": representative_cache[label],
                }
            )
        result_files, preview, count = self._write_rows(
            "communities",
            (
                {
                    "node_id": str(node), "node": self._display(str(node)),
                    "community": str(label),
                }
                for node, label in connection.execute(
                    "SELECT node,label FROM labels ORDER BY label,node"
                )
            ),
        )
        summary = {
            "success": True, "algorithm": requested,
            "execution_algorithm": "weighted_label_propagation",
            "execution_mode": "approximate",
            "approximate": approximate, "iterations": iterations,
            "num_communities": community_count, "modularity": modularity,
            "size_stats": size_stats, "communities_shown": len(communities),
            "communities_truncated": community_count > len(communities),
            "communities": communities,
            "focus_node_communities": focus_node_communities,
            "missing_focus_nodes": missing_focus_nodes,
            "parallel": {
                "strategy": "node-partition-map/sqlite-label-reduce",
                "workers": parallel_workers,
                "iterations": iterations,
                "map_partitions": parallel_workers,
                "nonempty_map_partitions": sum(
                    int(count > 0) for count in node_counts
                ),
                "node_count": sum(node_counts),
                "reduce_batch_rows": batch_rows,
            },
        }
        if approximate:
            summary["note"] = (
                f"大图路径将 {requested} 映射为不物化全图的加权标签传播近似；"
                "结果中已保留 requested/execution_algorithm 以便审计。"
            )
        connection.close()
        return {"summary": summary, "preview": preview, "result_files": result_files}

    def _community_modularity(self, connection: sqlite3.Connection) -> float | None:
        """Calculate weighted undirected modularity using streaming adjacency."""
        connection.executescript(
            """
            DROP TABLE IF EXISTS community_modularity_stats;
            CREATE TABLE community_modularity_stats(
                label TEXT PRIMARY KEY,
                degree REAL NOT NULL DEFAULT 0,
                internal_weight REAL NOT NULL DEFAULT 0
            ) WITHOUT ROWID;
            """
        )
        total_degree = 0.0
        for node in self.graph.iter_node_ids():
            label_row = connection.execute(
                "SELECT label FROM labels WHERE node=?", (node,)
            ).fetchone()
            if label_row is None:
                continue
            label = str(label_row[0])
            degree = 0.0
            # Count each undirected edge once using canonical node ordering,
            # while consuming a disk cursor instead of retaining a hub's
            # complete neighbor list.
            for neighbor, weight in self.graph.iter_neighbors(
                node, undirected=True
            ):
                nonnegative_weight = max(0.0, float(weight))
                degree += nonnegative_weight
                if neighbor == node:
                    # An undirected self-loop contributes two incidences to
                    # weighted degree but once to internal edge weight.
                    degree += nonnegative_weight
                    connection.execute(
                        """
                        INSERT INTO community_modularity_stats(
                            label,degree,internal_weight
                        ) VALUES(?,0,?)
                        ON CONFLICT(label) DO UPDATE SET
                            internal_weight=internal_weight+excluded.internal_weight
                        """,
                        (label, nonnegative_weight),
                    )
                    continue
                if str(node) > str(neighbor):
                    continue
                neighbor_label = connection.execute(
                    "SELECT label FROM labels WHERE node=?", (neighbor,)
                ).fetchone()
                if neighbor_label is not None and str(neighbor_label[0]) == label:
                    connection.execute(
                        """
                        INSERT INTO community_modularity_stats(
                            label,degree,internal_weight
                        ) VALUES(?,0,?)
                        ON CONFLICT(label) DO UPDATE SET
                            internal_weight=internal_weight+excluded.internal_weight
                        """,
                        (label, nonnegative_weight),
                    )
            connection.execute(
                """
                INSERT INTO community_modularity_stats(label,degree,internal_weight)
                VALUES(?,?,0)
                ON CONFLICT(label) DO UPDATE SET degree=degree+excluded.degree
                """,
                (label, degree),
            )
            total_degree += degree
        edge_weight = total_degree / 2.0
        if edge_weight <= 0.0:
            return None
        value = 0.0
        for degree, internal_weight in connection.execute(
            """
            SELECT degree,internal_weight
            FROM community_modularity_stats ORDER BY label
            """
        ):
            value += float(internal_weight) / edge_weight
            value -= (float(degree) / (2.0 * edge_weight)) ** 2
        return round(float(value), 6)

    def _centrality_iterative(
        self, kind: str, p: dict[str, Any]
    ) -> tuple[sqlite3.Connection, int, bool]:
        # Revalidate in the worker as durable jobs may have been queued by an
        # older deployment.  Reject non-finite values before scanning the
        # snapshot or creating graph-sized SQLite state.
        damping = _finite_float_parameter(p, "damping", 0.85)
        alpha = _finite_float_parameter(p, "alpha", 0.1)
        beta = _finite_float_parameter(p, "beta", 1.0)
        tolerance = _finite_float_parameter(p, "tolerance", 1e-6)
        if not 0 < damping < 1:
            raise ValueError("参数 damping 必须大于 0 且小于 1")
        if tolerance <= 0:
            raise ValueError("参数 tolerance 必须为正数")
        connection = self._connection(f"centrality-{kind}")
        connection.executescript(
            """
            CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
            CREATE TABLE next_scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
            CREATE TABLE outgoing(node TEXT PRIMARY KEY,total REAL NOT NULL) WITHOUT ROWID;
            """
        )
        n = max(1, self.graph.node_count)
        if kind in {"pagerank", "eigenvector"}:
            initial = 1.0 / n
        else:
            # NetworkX Katz starts from the zero vector when nstart is absent.
            initial = 0.0
        for node in self.graph.iter_node_ids():
            # Preserve the same weighted stochastic semantics as the small
            # NetworkX path.  Negative weights are not silently clipped;
            # outgoing weight is the algebraic sum and only a zero sum is a
            # dangling node.
            outgoing = sum(weight for _, weight in self.graph.iter_out_edges(node))
            connection.execute("INSERT INTO scores(node,value) VALUES (?,?)", (node, initial))
            connection.execute("INSERT INTO outgoing(node,total) VALUES (?,?)", (node, outgoing))
        connection.commit()
        default_iterations = 100 if kind == "pagerank" else 1000
        max_iterations = max(
            1, min(int(p.get("max_iterations", default_iterations)), 2000)
        )
        converged = False
        iterations = 0
        for iteration in range(max_iterations):
            if kind == "pagerank":
                dangling = float(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(scores.value),0) FROM scores
                        JOIN outgoing ON outgoing.node=scores.node WHERE outgoing.total=0
                        """
                    ).fetchone()[0]
                )
                base = (1.0 - damping) / n + damping * dangling / n
            else:
                base = beta if kind == "katz" else 0.0
            connection.execute("DELETE FROM next_scores")
            if kind == "eigenvector":
                # NetworkX iterates with A + I.  The spectral shift preserves
                # eigenvectors and avoids oscillation on bipartite graphs.
                connection.execute(
                    "INSERT INTO next_scores(node,value) SELECT node,value FROM scores"
                )
            else:
                connection.execute(
                    "INSERT INTO next_scores(node,value) SELECT node,? FROM scores", (base,)
                )
            for node, score, outgoing in connection.execute(
                """
                SELECT scores.node,scores.value,outgoing.total FROM scores
                JOIN outgoing ON outgoing.node=scores.node
                """
            ):
                node = str(node)
                if kind == "pagerank":
                    if float(outgoing) == 0.0:
                        continue
                    for target, weight in self.graph.iter_out_edges(node):
                        contribution = damping * float(score) * float(weight) / float(outgoing)
                        connection.execute(
                            "UPDATE next_scores SET value=value+? WHERE node=?",
                            (contribution, target),
                        )
                else:
                    factor = alpha if kind == "katz" else 1.0
                    for target, weight in self.graph.iter_out_edges(node):
                        connection.execute(
                            "UPDATE next_scores SET value=value+? WHERE node=?",
                            (factor * float(score) * float(weight), target),
                        )
            if kind == "eigenvector":
                norm = math.sqrt(
                    float(
                        connection.execute(
                            "SELECT COALESCE(SUM(value*value),0) FROM next_scores"
                        ).fetchone()[0]
                    )
                )
                if norm:
                    connection.execute("UPDATE next_scores SET value=value/?", (norm,))
            delta = float(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(ABS(scores.value-next_scores.value)),0)
                    FROM scores JOIN next_scores ON scores.node=next_scores.node
                    """
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE scores SET value=(SELECT value FROM next_scores WHERE next_scores.node=scores.node)"
            )
            connection.commit()
            iterations = iteration + 1
            if delta <= tolerance * n:
                converged = True
                break
        if kind == "katz" and converged:
            # ``nx.katz_centrality`` returns a unit-L2 vector by default.
            norm = math.sqrt(
                float(
                    connection.execute(
                        "SELECT COALESCE(SUM(value*value),0) FROM scores"
                    ).fetchone()[0]
                )
            )
            if norm:
                connection.execute("UPDATE scores SET value=value/?", (norm,))
                connection.commit()
        return connection, iterations, converged

    def _run_distance_source_batches(
        self,
        *,
        mode: str,
        weighted: bool,
        direction: str,
        source_path: Path | None = None,
        progress_message: str,
    ) -> list[dict[str, Any]]:
        """Run source-decomposable shortest-path work in isolated processes."""
        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        args = [
            (
                str(self.graph.manifest_path),
                worker_index,
                parallel_workers,
                str(source_path) if source_path is not None else "",
                mode,
                weighted,
                direction,
                str(self.work_root),
            )
            for worker_index in range(parallel_workers)
        ]
        results: list[dict[str, Any]] = []
        with ProcessPoolExecutor(
            max_workers=parallel_workers, mp_context=process_context()
        ) as pool:
            futures = [pool.submit(_distance_source_partition, item) for item in args]
            for completed, future in enumerate(as_completed(futures), 1):
                results.append(future.result())
                self._emit(
                    "algorithm_map",
                    0.20 + 0.55 * completed / len(futures),
                    progress_message,
                    workers=parallel_workers,
                    workers_completed=completed,
                )
        return sorted(results, key=lambda item: int(item["worker"]))

    def _distance_centrality_parallel(
        self, kind: str
    ) -> sqlite3.Connection:
        """Merge worker-owned closeness/harmonic shards into disk state."""
        connection = self._connection(f"centrality-{kind}")
        connection.execute(
            "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
        )
        try:
            results = self._run_distance_source_batches(
                mode=kind,
                weighted=True,
                direction="in",
                progress_message="距离中心性源点分批计算中",
            )
            for result in results:
                output_path = Path(str(result["output_path"]))
                batch: list[tuple[str, float]] = []
                with output_path.open("r", encoding="utf-8") as source:
                    for line in source:
                        node, value = json.loads(line)
                        batch.append((str(node), float(value)))
                        if len(batch) >= 10_000:
                            connection.executemany(
                                "INSERT INTO scores(node,value) VALUES (?,?)", batch
                            )
                            batch.clear()
                if batch:
                    connection.executemany(
                        "INSERT INTO scores(node,value) VALUES (?,?)", batch
                    )
                connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def _distance_centrality(self, kind: str, p: dict[str, Any]) -> sqlite3.Connection:
        if self.workers > 1 and self.graph.partitions > 1:
            return self._distance_centrality_parallel(kind)
        connection = self._connection(f"centrality-{kind}")
        connection.execute(
            "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
        )
        for index, node in enumerate(self.graph.iter_node_ids()):
            # NetworkX defines both directed closeness and harmonic centrality
            # from other nodes *to* the measured node.
            direction = "in"
            distances = self._shortest_state(
                node, weighted=True, direction=direction, stem=f"{kind}-source"
            )
            count, total = distances.execute(
                "SELECT COUNT(*),COALESCE(SUM(distance),0) FROM distances WHERE node<>?",
                (node,),
            ).fetchone()
            if kind == "closeness":
                reachable = int(count)
                value = (
                    reachable / float(total) * reachable / max(1, self.graph.node_count - 1)
                    if reachable and float(total) > 0 else 0.0
                )
            else:
                value = float(
                    distances.execute(
                        "SELECT COALESCE(SUM(1.0/distance),0) FROM distances WHERE distance>0"
                    ).fetchone()[0]
                )
            distances.close()
            connection.execute("INSERT INTO scores(node,value) VALUES (?,?)", (node, value))
            if index % 100 == 0:
                connection.commit()
        connection.commit()
        return connection

    def _brandes_centrality_parallel(
        self, kind: str, p: dict[str, Any]
    ) -> tuple[sqlite3.Connection, bool]:
        """Run independent Brandes source batches in worker processes."""
        sample_cap = _brandes_sample_cap(p)
        connection = self._connection(f"centrality-{kind}")
        connection.execute(
            "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO scores(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        approximate = self.graph.node_count > sample_cap
        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        # Sampling only materialises at most the deployment hard cap.  Split
        # that bounded set immediately so no child receives/pickles the same
        # complete source list.  Exact mode keeps all source selection on disk
        # and workers scan only their stable hash partition.
        source_buckets: list[list[str]] | None = None
        if approximate:
            source_buckets = [[] for _ in range(parallel_workers)]
            selected_count = 0
            for source in self.graph.iter_node_ids():
                if selected_count >= sample_cap:
                    break
                source_buckets[
                    _stable_partition(source, parallel_workers)
                ].append(source)
                selected_count += 1
        args = [
            (
                str(self.graph.manifest_path),
                worker_index,
                parallel_workers,
                source_buckets[worker_index] if source_buckets is not None else None,
                str(self.work_root),
            )
            for worker_index in range(parallel_workers)
        ]
        source_count = 0
        with ProcessPoolExecutor(
            max_workers=parallel_workers, mp_context=process_context()
        ) as pool:
            futures = [pool.submit(_brandes_source_partition, item) for item in args]
            for completed, future in enumerate(as_completed(futures), 1):
                result = future.result()
                source_count += int(result["source_count"])
                batch: list[tuple[float, str]] = []
                with Path(str(result["output_path"])).open("r", encoding="utf-8") as source:
                    for line in source:
                        node, value = json.loads(line)
                        batch.append((float(value), str(node)))
                        if len(batch) >= 10_000:
                            connection.executemany(
                                "UPDATE scores SET value=value+? WHERE node=?", batch
                            )
                            batch.clear()
                if batch:
                    connection.executemany(
                        "UPDATE scores SET value=value+? WHERE node=?", batch
                    )
                connection.commit()
                self._emit(
                    "algorithm_map",
                    0.20 + 0.55 * completed / len(futures),
                    "中心性源点分批计算中",
                    workers=parallel_workers,
                    workers_completed=completed,
                )
        if approximate and source_count:
            connection.execute(
                "UPDATE scores SET value=value*?", (self.graph.node_count / source_count,)
            )
        if self.graph.node_count > 2:
            scale = 1.0 / ((self.graph.node_count - 1) * (self.graph.node_count - 2))
            connection.execute("UPDATE scores SET value=value*?", (scale,))
        connection.commit()
        return connection, approximate

    def _brandes_centrality(self, kind: str, p: dict[str, Any]) -> tuple[sqlite3.Connection, bool]:
        """Disk-state Brandes; source sampling is explicit above the safety cap."""
        sample_cap = _brandes_sample_cap(p)
        # Added: Brandes is source-decomposable, so use the parallel reducer
        # whenever the engine supplied more than one worker.
        if self.workers > 1 and self.graph.partitions > 1:
            return self._brandes_centrality_parallel(kind, p)
        connection = self._connection(f"centrality-{kind}")
        connection.execute(
            "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO scores(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        approximate = self.graph.node_count > sample_cap
        sources = self._take(self.graph.iter_node_ids(), sample_cap) if approximate else self.graph.iter_node_ids()
        source_count = 0
        for source in sources:
            state = self._shortest_state(
                source, weighted=True, stem="brandes-source", collect_all_predecessors=True
            )
            state.executescript(
                """
                DROP TABLE IF EXISTS sigma;
                DROP TABLE IF EXISTS dependency;
                CREATE TABLE sigma(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
                CREATE TABLE dependency(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID;
                """
            )
            state.execute("INSERT INTO sigma(node,value) VALUES (?,1)", (source,))
            for (node,) in state.execute(
                "SELECT node FROM distances WHERE node<>? ORDER BY distance,node", (source,)
            ):
                sigma = float(
                    state.execute(
                        """
                        SELECT COALESCE(SUM(sigma.value),0) FROM predecessors
                        JOIN sigma ON sigma.node=predecessors.predecessor
                        WHERE predecessors.node=?
                        """,
                        (str(node),),
                    ).fetchone()[0]
                )
                state.execute(
                    "INSERT INTO sigma(node,value) VALUES (?,?)", (str(node), sigma)
                )
            state.execute(
                "INSERT INTO dependency(node,value) SELECT node,0 FROM distances"
            )
            for (node,) in state.execute(
                "SELECT node FROM distances WHERE node<>? ORDER BY distance DESC,node DESC",
                (source,),
            ):
                node = str(node)
                sigma_w = float(
                    state.execute("SELECT value FROM sigma WHERE node=?", (node,)).fetchone()[0]
                )
                dependency = float(
                    state.execute(
                        "SELECT value FROM dependency WHERE node=?", (node,)
                    ).fetchone()[0]
                )
                if sigma_w:
                    for (predecessor,) in state.execute(
                        "SELECT predecessor FROM predecessors WHERE node=?", (node,)
                    ):
                        pred = str(predecessor)
                        sigma_v = float(
                            state.execute(
                                "SELECT value FROM sigma WHERE node=?", (pred,)
                            ).fetchone()[0]
                        )
                        state.execute(
                            "UPDATE dependency SET value=value+? WHERE node=?",
                            (sigma_v / sigma_w * (1.0 + dependency), pred),
                        )
                connection.execute(
                    "UPDATE scores SET value=value+? WHERE node=?", (dependency, node)
                )
            state.close()
            source_count += 1
            connection.commit()
        if approximate and source_count:
            connection.execute(
                "UPDATE scores SET value=value*?", (self.graph.node_count / source_count,)
            )
        if self.graph.node_count > 2:
            scale = 1.0 / (
                (self.graph.node_count - 1) * (self.graph.node_count - 2)
            )
            connection.execute("UPDATE scores SET value=value*?", (scale,))
        connection.commit()
        return connection, approximate

    def _voterank(self, p: dict[str, Any]) -> sqlite3.Connection:
        connection = self._connection("centrality-voterank")
        connection.execute(
            """
            CREATE TABLE scores(
                node TEXT PRIMARY KEY,value REAL NOT NULL,
                ability REAL NOT NULL,selected INTEGER NOT NULL DEFAULT 0,
                ordinal INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        avg_degree = (
            (self.graph.edge_count if self.graph.directed else 2.0 * self.graph.edge_count)
            / max(1, self.graph.node_count)
        )
        for ordinal, node in enumerate(self.graph.iter_node_ids()):
            connection.execute(
                "INSERT INTO scores(node,value,ability,ordinal) VALUES (?,0,1,?)",
                (node, ordinal),
            )
        connection.commit()

        # NetworkX attempts to rank every node and stops as soon as all
        # remaining candidates receive zero votes.  ``limit`` only bounds the
        # response preview; the artifact retains every actually elected node.
        for rank in range(1, self.graph.node_count + 1):
            connection.execute("UPDATE scores SET value=0")
            for source, target, _, _ in self.graph.iter_edges():
                target_ability = float(
                    connection.execute(
                        "SELECT ability FROM scores WHERE node=?", (target,)
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE scores SET value=value+? WHERE node=?",
                    (target_ability, source),
                )
                if not self.graph.directed:
                    source_ability = float(
                        connection.execute(
                            "SELECT ability FROM scores WHERE node=?", (source,)
                        ).fetchone()[0]
                    )
                    connection.execute(
                        "UPDATE scores SET value=value+? WHERE node=?",
                        (source_ability, target),
                    )
            connection.execute("UPDATE scores SET value=0 WHERE selected<>0")
            row = connection.execute(
                "SELECT node FROM scores WHERE selected=0 "
                "ORDER BY value DESC,ordinal LIMIT 1"
            ).fetchone()
            if row is None or float(
                connection.execute(
                    "SELECT value FROM scores WHERE node=?", (str(row[0]),)
                ).fetchone()[0]
            ) <= 0:
                break
            selected = str(row[0])
            connection.execute(
                "UPDATE scores SET selected=?,ability=0,value=0 WHERE node=?",
                (rank, selected),
            )
            # In a directed graph only the selected node's out-neighbors lose
            # voting ability.  For an undirected snapshot iter_out_edges
            # exposes both endpoint orientations.
            for neighbor, _ in self.graph.iter_out_edges(selected):
                connection.execute(
                    "UPDATE scores SET ability=MAX(0,ability-?) WHERE node=?",
                    (1.0 / avg_degree, neighbor),
                )
            connection.commit()
        connection.execute("DELETE FROM scores WHERE selected=0")
        connection.execute(
            "UPDATE scores SET value=?-selected+1", (self.graph.node_count,)
        )
        connection.commit()
        return connection

    def node_centrality(self, p: dict[str, Any]) -> dict[str, Any]:
        kind = str(p.get("centrality_type", "degree")).strip().lower()
        available = {
            "degree", "betweenness", "closeness", "pagerank", "eigenvector",
            "harmonic", "katz", "load", "voterank", "percolation",
        }
        if kind not in available:
            summary = {
                "success": False, "error": f"未知类型 '{kind}'",
                "available": sorted(available),
            }
            return {"summary": summary, "preview": [], "result_files": []}
        approximate = False
        approximation_reasons: list[str] = []
        iterations: int | None = None
        converged: bool | None = None
        if kind == "degree":
            connection = self._connection("centrality-degree")
            connection.execute(
                "CREATE TABLE scores(node TEXT PRIMARY KEY,value REAL NOT NULL) WITHOUT ROWID"
            )
            divisor = max(1, self.graph.node_count - 1)
            connection.executemany(
                "INSERT INTO scores(node,value) VALUES (?,?)",
                (
                    (
                        node,
                        1.0
                        if self.graph.node_count <= 1
                        else self.graph.degree(node) / divisor,
                    )
                    for node in self.graph.iter_node_ids()
                ),
            )
        elif kind in {"pagerank", "eigenvector", "katz"}:
            connection, iterations, converged = self._centrality_iterative(kind, p)
            if not converged:
                connection.close()
                summary = {
                    "success": False,
                    "centrality_type": kind,
                    "error": f"{kind} 在 {iterations} 次迭代内未收敛",
                    "iterations": iterations,
                    "converged": False,
                    "execution_mode": "failed",
                }
                return {"summary": summary, "preview": [], "result_files": []}
        elif kind in {"closeness", "harmonic"}:
            connection = self._distance_centrality(kind, p)
        elif kind == "voterank":
            connection = self._voterank(p)
        else:
            # Added: load/percolation currently share the disk Brandes kernel.
            # Keep the result usable, but make the semantic substitution
            # visible instead of presenting it as an exact small-path result.
            if kind == "load":
                approximate = True
                approximation_reasons.append("load 使用 Brandes betweenness 近似")
            elif kind == "percolation":
                approximate = True
                approximation_reasons.append(
                    "percolation 未携带节点状态且大图内核按加权 Brandes 计算"
                )
            connection, sampled = self._brandes_centrality(kind, p)
            approximate = approximate or sampled
            if sampled:
                approximation_reasons.append("Brandes 仅采样部分源点")
        connection.executescript(
            """
            DROP TABLE IF EXISTS ranking_order;
            CREATE TABLE ranking_order(
                node TEXT PRIMARY KEY,ordinal INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO ranking_order(node,ordinal) VALUES (?,?)",
            (
                (node, ordinal)
                for ordinal, node in enumerate(self.graph.iter_node_ids())
            ),
        )
        connection.commit()
        count = int(connection.execute("SELECT COUNT(*) FROM scores").fetchone()[0])
        limit = _limit(p)
        top = [
            {
                "node": self._display(str(node)),
                "value": _centrality_control_value(value),
            }
            for node, value in connection.execute(
                """SELECT scores.node,scores.value FROM scores
                   JOIN ranking_order ON ranking_order.node=scores.node
                   ORDER BY scores.value DESC,ranking_order.ordinal LIMIT ?""",
                (limit,),
            )
        ]
        bottom_start = max(limit, count - 10)
        bottom = [
            {
                "node": self._display(str(node)),
                "value": _centrality_control_value(value),
            }
            for node, value in connection.execute(
                """
                SELECT scores.node,scores.value FROM scores
                JOIN ranking_order ON ranking_order.node=scores.node
                ORDER BY scores.value DESC,ranking_order.ordinal
                LIMIT 10 OFFSET ?
                """,
                (bottom_start,),
            )
        ]
        aggregate = connection.execute(
            "SELECT MIN(value),MAX(value),AVG(value) FROM scores"
        ).fetchone()
        median_offset = max(0, (count - 1) // 2)
        median_take = 2 if count and count % 2 == 0 else 1
        median_values = [
            float(row[0])
            for row in connection.execute(
                "SELECT value FROM scores ORDER BY value LIMIT ? OFFSET ?",
                (median_take, median_offset),
            )
        ]
        median = (
            _centrality_control_value(sum(median_values) / len(median_values))
            if median_values
            else 0.0
        )
        result_files, preview, _ = self._write_rows(
            f"centrality-{kind}",
            (
                {
                    "node_id": str(node), "node": self._display(str(node)),
                    "value": float(value),
                }
                for node, value in connection.execute(
                    """SELECT scores.node,scores.value FROM scores
                       JOIN ranking_order ON ranking_order.node=scores.node
                       ORDER BY ranking_order.ordinal"""
                )
            ),
        )
        summary: dict[str, Any] = {
            "success": True, "centrality_type": kind, "total_nodes": count,
            "execution_algorithm": (
                "disk_brandes" if kind in {"betweenness", "load", "percolation"}
                else f"disk_{kind}"
            ),
            "execution_mode": "approximate" if approximate else "exact",
            "value_stats": {
                "min": _centrality_control_value(aggregate[0] or 0),
                "max": _centrality_control_value(aggregate[1] or 0),
                "mean": _centrality_control_value(aggregate[2] or 0),
                "median": median,
            },
            "top_nodes": top, "bottom_nodes": bottom, "truncated": count > len(top),
            "approximate": approximate,
        }
        if approximation_reasons:
            summary["approximation_reasons"] = approximation_reasons
        if iterations is not None:
            summary.update({"iterations": iterations, "converged": converged})
        if self.workers > 1 and self.graph.partitions > 1:
            if kind in {"betweenness", "load", "percolation"}:
                summary["parallel"] = {
                    "strategy": "source-batch-map/dependency-reduce",
                    "workers": min(self.workers, self.graph.partitions),
                }
            elif kind in {"closeness", "harmonic"}:
                summary["parallel"] = {
                    "strategy": "source-batch-map/score-disk-reduce",
                    "workers": min(self.workers, self.graph.partitions),
                    "source_count": self.graph.node_count,
                }
        if kind == "percolation":
            summary["note"] = "快照不含 percolation 节点属性时按全体状态 1.0 计算。"
        connection.close()
        return {"summary": summary, "preview": preview, "result_files": result_files}

    # ── 可达性、路径与环（新增）────────────────────────────────────

    def _reachability_state(
        self,
        source: str,
        *,
        direction: str = "out",
        undirected: bool = False,
        max_depth: int | None = None,
        target: str | None = None,
        stem: str = "reachability",
    ) -> sqlite3.Connection:
        connection = self._connection(stem)
        connection.executescript(
            """
            DROP TABLE IF EXISTS reached;
            DROP TABLE IF EXISTS reach_queue;
            CREATE TABLE reached(node TEXT PRIMARY KEY,depth INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE reach_queue(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT,depth INTEGER);
            """
        )
        connection.execute("INSERT INTO reached(node,depth) VALUES (?,0)", (source,))
        connection.execute("INSERT INTO reach_queue(node,depth) VALUES (?,0)", (source,))
        while True:
            row = connection.execute(
                "SELECT seq,node,depth FROM reach_queue ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM reach_queue WHERE seq=?", (int(row[0]),))
            node, depth = str(row[1]), int(row[2])
            # Pairwise reachability only needs to prove that ``target`` was
            # discovered.  Continuing to materialise the full reachable set
            # turns a nearby has_path query into an unnecessary whole-graph
            # traversal on a large connected graph.
            if target is not None and node == target:
                break
            if max_depth is not None and depth >= max_depth:
                continue
            for neighbor, _ in self.graph.iter_neighbors(
                node, direction=direction, undirected=undirected
            ):
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO reached(node,depth) VALUES (?,?)",
                    (neighbor, depth + 1),
                ).rowcount
                if inserted:
                    connection.execute(
                        "INSERT INTO reach_queue(node,depth) VALUES (?,?)",
                        (neighbor, depth + 1),
                    )
            connection.commit()
        return connection

    def reachability(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        limit = _limit(p)
        if method == "transitive_closure":
            parallel_workers = min(
                self.workers,
                max(1, self.graph.partitions),
                max(1, limit),
                os.cpu_count() or 1,
            )
            if parallel_workers > 1:
                context = process_context()
                shared_counter = context.Value("i", 0)
                args = [
                    (
                        str(self.graph.manifest_path),
                        worker_index,
                        parallel_workers,
                        str(self.work_root),
                    )
                    for worker_index in range(parallel_workers)
                ]
                results: list[dict[str, Any]] = []
                with ProcessPoolExecutor(
                    max_workers=parallel_workers,
                    mp_context=context,
                    initializer=_init_bounded_enumeration_worker,
                    initargs=(shared_counter, limit),
                ) as pool:
                    futures = [
                        pool.submit(_transitive_closure_source_partition, item)
                        for item in args
                    ]
                    for completed, future in enumerate(
                        as_completed(futures), 1
                    ):
                        results.append(future.result())
                        self._emit(
                            "algorithm_map",
                            0.20 + 0.55 * completed / len(futures),
                            "传递闭包源点分片遍历中",
                            workers=parallel_workers,
                            workers_completed=completed,
                        )
                returned = sum(
                    int(result["returned"]) for result in results
                )
                truncated = any(
                    bool(result["truncated"]) for result in results
                )
                preview: list[dict[str, Any]] = []

                def closure_rows() -> Iterable[dict[str, Any]]:
                    for result in sorted(
                        results, key=lambda item: int(item["worker"])
                    ):
                        with Path(str(result["output_path"])).open(
                            "r", encoding="utf-8"
                        ) as source_file:
                            for line in source_file:
                                if not line.strip():
                                    continue
                                source, target = json.loads(line)
                                row = {
                                    "from_id": str(source),
                                    "from": self._display(str(source)),
                                    "to_id": str(target),
                                    "to": self._display(str(target)),
                                }
                                if len(preview) < limit:
                                    preview.append(
                                        {
                                            "from": self._preview_node(
                                                str(source)
                                            ),
                                            "to": self._preview_node(str(target)),
                                        }
                                    )
                                yield row

                result_files, _, artifact_rows = self._write_rows(
                    "transitive-closure", closure_rows()
                )
                summary = {
                    "success": True,
                    "method": method,
                    "edges_preview": preview,
                    "truncated": truncated,
                    "num_edges_at_least": returned + (1 if truncated else 0),
                    "closure_artifact_rows": artifact_rows,
                    "parallel": {
                        "strategy": "source-partition-map/bounded-artifact-merge",
                        "workers": parallel_workers,
                        "sources_started": sum(
                            int(result["source_count"]) for result in results
                        ),
                    },
                    "note": (
                        f"已展示前 {len(preview)} 条传递闭包边；完整闭包未物化。"
                        if truncated
                        else None
                    ),
                }
                return {
                    "summary": summary,
                    "preview": preview,
                    "result_files": result_files,
                }
            path = self.result_root / "transitive-closure.jsonl"
            count = 0
            preview: list[dict[str, Any]] = []
            # Matches the bounded small-path contract: closure generation stops
            # after one look-ahead row instead of creating an uncontrolled V² file.
            with path.open("w", encoding="utf-8") as output:
                stop = False
                for source in self.graph.iter_node_ids():
                    reached = self._reachability_state(
                        source, stem="closure-source"
                    )
                    try:
                        for (target,) in reached.execute(
                            "SELECT node FROM reached WHERE node<>? ORDER BY node", (source,)
                        ):
                            row = {
                                "from": self._display(source),
                                "to": self._display(str(target)),
                            }
                            if count < limit:
                                output.write(
                                    json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                )
                                preview.append(row)
                            count += 1
                            if count > limit:
                                stop = True
                                break
                    finally:
                        reached.close()
                    if stop:
                        break
            truncated = count > limit
            summary = {
                "success": True, "method": method, "edges_preview": preview,
                "truncated": truncated,
                "num_edges_at_least": min(count, limit) + (1 if truncated else 0),
                "note": (
                    f"已展示前 {len(preview)} 条传递闭包边；完整闭包未物化。"
                    if truncated else None
                ),
            }
            return {
                "summary": summary, "preview": preview,
                "result_files": ["results/transitive-closure.jsonl"],
            }
        if method not in {"descendants", "ancestors"}:
            summary = {"success": False, "error": f"未知方法 '{method}'"}
            return {"summary": summary, "preview": [], "result_files": []}
        node, error = self._required_node(p.get("node"), "节点")
        if error:
            return {"summary": error, "preview": [], "result_files": []}
        assert node is not None
        direction = "out" if method == "descendants" else "in"
        reached = self._reachability_state(node, direction=direction, stem=method)
        try:
            total = int(
                reached.execute(
                    "SELECT COUNT(*) FROM reached WHERE node<>?", (node,)
                ).fetchone()[0]
            )
            names = [
                self._display(str(row[0]))
                for row in reached.execute(
                    "SELECT node FROM reached WHERE node<>? ORDER BY node LIMIT ?",
                    (node, limit),
                )
            ]
            result_files, preview, _ = self._write_rows(
                method,
                (
                    {"node_id": str(target), "node": self._display(str(target)), "depth": int(depth)}
                    for target, depth in reached.execute(
                        "SELECT node,depth FROM reached WHERE node<>? ORDER BY depth,node",
                        (node,),
                    )
                ),
            )
            summary = {
                "success": True, "method": method, "node": self._display(node),
                "total_count": total, "nodes_preview": names,
                "truncated": total > len(names),
            }
            return {"summary": summary, "preview": preview, "result_files": result_files}
        finally:
            reached.close()

    def _iter_simple_paths(
        self, source: str, target: str, cutoff: int
    ) -> Iterable[list[str]]:
        # A frontier of complete path copies can become exponential before the
        # first result is yielded. Keep exactly one DFS path and one lazy
        # adjacency cursor per depth instead; resident state is O(cutoff).
        path = [source]
        path_nodes = {source}
        cursors = [iter(self.graph.iter_out_edges(source))]
        while cursors:
            try:
                neighbor, _ = next(cursors[-1])
            except StopIteration:
                cursors.pop()
                removed = path.pop()
                path_nodes.remove(removed)
                continue
            neighbor = str(neighbor)
            if neighbor in path_nodes:
                continue
            candidate_hops = len(path)
            if neighbor == target:
                yield [*path, neighbor]
                continue
            if candidate_hops >= cutoff:
                continue
            path.append(neighbor)
            path_nodes.add(neighbor)
            cursors.append(iter(self.graph.iter_out_edges(neighbor)))

    def path_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        if method in {"all_simple_paths", "has_path"}:
            source, error = self._required_node(p.get("source"), "起点")
            if error:
                return {"summary": error, "preview": [], "result_files": []}
            target, error = self._required_node(p.get("target"), "终点")
            if error:
                return {"summary": error, "preview": [], "result_files": []}
            assert source is not None and target is not None
            if method == "has_path":
                reached = self._reachability_state(
                    source, target=target, stem="has-path"
                )
                found = reached.execute(
                    "SELECT 1 FROM reached WHERE node=?", (target,)
                ).fetchone() is not None
                reached.close()
                summary = {"success": True, "method": method, "has_path": found}
                return {"summary": summary, "preview": [], "result_files": []}
            cap = _limit(p, _PATH_LIMIT)
            cutoff = max(1, min(int(p.get("cutoff", 5)), 10_000))
            displayed: list[list[str]] = []
            path_node_counts: list[int] = []
            returned = 0
            truncated = False

            def path_rows() -> Iterable[dict[str, Any]]:
                nonlocal returned, truncated
                for path in self._iter_simple_paths(source, target, cutoff):
                    if returned >= cap:
                        truncated = True
                        return
                    path_index = returned
                    returned += 1
                    if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                        preview, node_count = self._sequence_preview(path)
                        displayed.append(preview)
                        path_node_counts.append(node_count)
                    yield from self._path_rows(
                        path, path_index=path_index, node_count=len(path)
                    )

            result_files, _, artifact_rows = self._write_rows(
                "all-simple-paths", path_rows()
            )
            summary = {
                "success": True, "method": method, "count": returned,
                "paths": displayed, "truncated": truncated,
                "path_node_counts_preview": path_node_counts,
                "paths_preview_truncated": (
                    returned > len(displayed)
                    or any(
                        count > len(path)
                        for count, path in zip(path_node_counts, displayed)
                    )
                ),
                "path_artifact_rows": artifact_rows,
                "note": f"最多返回 {cap} 条路径" if truncated else None,
            }
            return {
                "summary": summary,
                "preview": displayed,
                "result_files": result_files,
            }
        if method == "average_shortest_path_length":
            if self.graph.node_count == 0:
                summary = {"success": False, "error": "空图没有平均最短路径长度"}
                return {"summary": summary, "preview": [], "result_files": []}
            parallel: dict[str, Any] | None = None
            if self.graph.directed:
                components = self._component_state("path-average-scc")
                try:
                    component_count = int(
                        components.execute(
                            "SELECT COUNT(DISTINCT component) FROM components"
                        ).fetchone()[0]
                    )
                finally:
                    components.close()
                if component_count != 1:
                    summary = {
                        "success": False,
                        "error": "有向图不是强连通图，无法计算全图平均最短路径长度",
                    }
                    return {"summary": summary, "preview": [], "result_files": []}
                if self.workers > 1 and self.graph.partitions > 1:
                    value, _, _, parallel = self._distance_batch_aggregate(
                        mode="path-average",
                        weighted=True,
                        direction="out",
                        source_path=None,
                        progress_message="平均最短路径源点分批计算中",
                    )
                else:
                    total_distance = 0.0
                    pair_count = 0
                    for source in self.graph.iter_node_ids():
                        distances = self._shortest_state(
                            source,
                            weighted=True,
                            direction="out",
                            stem="path-average-source",
                        )
                        try:
                            count, total = distances.execute(
                                """
                                SELECT COUNT(*),COALESCE(SUM(distance),0)
                                FROM distances WHERE node<>?
                                """,
                                (source,),
                            ).fetchone()
                            pair_count += int(count)
                            total_distance += float(total)
                        finally:
                            distances.close()
                    value = total_distance / pair_count if pair_count else 0.0
            else:
                if self.workers > 1 and self.graph.partitions > 1:
                    value, _, _, parallel = self._distance_extrema_parallel(
                        weighted=True, mode="path-average"
                    )
                else:
                    value, _, _ = self._distance_extrema(
                        need_average=True, weighted=True
                    )
            summary = {"success": True, "method": method, "value": round(value, 6)}
            if parallel is not None:
                summary["parallel"] = parallel
            return {"summary": summary, "preview": [], "result_files": []}
        summary = {"success": False, "error": f"未知方法 '{method}'"}
        return {"summary": summary, "preview": [], "result_files": []}

    @staticmethod
    def _normalise_cycle(cycle: list[str], directed: bool) -> tuple[str, ...]:
        """Return the canonical rotation in O(L), not O(L²), memory/time."""
        if not cycle:
            return ()

        def least_rotation(values: tuple[str, ...]) -> tuple[str, ...]:
            size = len(values)
            if size < 2:
                return values
            doubled = values + values
            left, right, offset = 0, 1, 0
            while left < size and right < size and offset < size:
                lhs = doubled[left + offset]
                rhs = doubled[right + offset]
                if lhs == rhs:
                    offset += 1
                    continue
                if lhs > rhs:
                    left = left + offset + 1
                    if left == right:
                        left += 1
                else:
                    right = right + offset + 1
                    if left == right:
                        right += 1
                offset = 0
            start = min(left, right)
            return tuple(doubled[start : start + size])

        forward = least_rotation(tuple(cycle))
        if not directed:
            backward = least_rotation(tuple(reversed(cycle)))
            return min(forward, backward)
        return forward

    def _iter_cycles(
        self,
        *,
        include_undirected_self_loops: bool = False,
        start_worker: int | None = None,
        start_worker_count: int = 1,
    ) -> Iterable[list[str]]:
        """Yield each canonical simple cycle without retaining prior cycles."""
        for start in self.graph.iter_node_ids():
            if (
                start_worker is not None
                and _stable_partition(start, max(1, int(start_worker_count)))
                != int(start_worker)
            ):
                continue
            # As with simple paths, retain one lazy DFS branch rather than an
            # exponential frontier of copied paths.
            path = [start]
            path_nodes = {start}
            cursors = [iter(self.graph.iter_neighbors(start, direction="out"))]
            while cursors:
                try:
                    neighbor, _ = next(cursors[-1])
                except StopIteration:
                    cursors.pop()
                    removed = path.pop()
                    path_nodes.remove(removed)
                    continue
                neighbor = str(neighbor)
                if neighbor == start:
                    if not self.graph.directed:
                        if len(path) == 2:
                            # Walking an undirected edge and immediately
                            # returning is not a cycle in a simple Graph.
                            continue
                        if len(path) == 1 and not include_undirected_self_loops:
                            continue
                    key = self._normalise_cycle(path, self.graph.directed)
                    # ``start`` is the least member. Directed cycles have one
                    # start; undirected cycles emit only canonical orientation.
                    if key and tuple(path) == key:
                        yield list(key)
                    continue
                if neighbor in path_nodes or neighbor < start:
                    continue
                path.append(neighbor)
                path_nodes.add(neighbor)
                cursors.append(
                    iter(self.graph.iter_neighbors(neighbor, direction="out"))
                )

    def _cycle_weight(self, cycle: list[str]) -> float:
        total = 0.0
        for index, source in enumerate(cycle):
            target = cycle[(index + 1) % len(cycle)]
            weight = self.graph.edge_weight(
                source, target, undirected=not self.graph.directed
            )
            total += float(weight if weight is not None else 1.0)
        return total

    def _cycle_rows(
        self, cycle: list[str], *, cycle_index: int = 0
    ) -> Iterable[dict[str, Any]]:
        for position, source in enumerate(cycle):
            target = cycle[(position + 1) % len(cycle)]
            weight = self.graph.edge_weight(
                source, target, undirected=not self.graph.directed
            )
            yield {
                "kind": "cycle_edge",
                "cycle_index": int(cycle_index),
                "position": position,
                "cycle_nodes_total": len(cycle),
                "from_id": source,
                "from": self._display(source),
                "to_id": target,
                "to": self._display(target),
                "weight": float(weight if weight is not None else 1.0),
            }

    def _cycle_payload(
        self, cycle: list[str], *, total_weight: float | None = None
    ) -> dict[str, Any]:
        nodes, node_count = self._sequence_preview(cycle)
        edges: list[dict[str, Any]] = []
        for position, source in enumerate(cycle[:_SEQUENCE_PREVIEW_LIMIT]):
            target = cycle[(position + 1) % len(cycle)]
            weight = self.graph.edge_weight(
                source, target, undirected=not self.graph.directed
            )
            edges.append(
                {
                    "from": self._preview_node(source),
                    "to": self._preview_node(target),
                    "weight": float(weight if weight is not None else 1.0),
                }
            )
        return {
            "cycle": nodes,
            "cycle_preview": nodes,
            "cycle_length": node_count,
            "cycle_truncated": node_count > len(nodes),
            "cycle_weight": round(
                self._cycle_weight(cycle) if total_weight is None else total_weight,
                6,
            ),
            "edges": edges,
            "edges_preview": edges,
            "edges_total": node_count,
            "edges_truncated": node_count > len(edges),
        }

    def find_cycles(self, p: dict[str, Any]) -> dict[str, Any]:
        cap = _limit(p)
        displayed: list[list[str]] = []
        cycle_lengths: list[int] = []
        returned = 0
        truncated = False
        parallel: dict[str, Any] | None = None
        parallel_workers = min(
            self.workers,
            max(1, self.graph.partitions),
            cap,
            os.cpu_count() or 1,
        )

        def cycle_rows() -> Iterable[dict[str, Any]]:
            nonlocal returned, truncated
            for cycle in self._iter_cycles(include_undirected_self_loops=True):
                if returned >= cap:
                    truncated = True
                    return
                cycle_index = returned
                returned += 1
                if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                    preview, node_count = self._sequence_preview(cycle)
                    displayed.append(preview)
                    cycle_lengths.append(node_count)
                yield from self._cycle_rows(cycle, cycle_index=cycle_index)

        rows: Iterable[dict[str, Any]]
        if parallel_workers > 1:
            context = process_context()
            shared_counter = context.Value("i", 0)
            args = [
                (
                    str(self.graph.manifest_path),
                    worker_index,
                    parallel_workers,
                    str(self.work_root),
                )
                for worker_index in range(parallel_workers)
            ]
            results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=parallel_workers,
                mp_context=context,
                initializer=_init_bounded_enumeration_worker,
                initargs=(shared_counter, cap),
            ) as pool:
                futures = [
                    pool.submit(_find_cycles_start_partition, item)
                    for item in args
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    self._emit(
                        "algorithm_map",
                        0.20 + 0.55 * completed / len(futures),
                        "环起始节点分片枚举中",
                        workers=parallel_workers,
                        workers_completed=completed,
                    )
            returned = sum(int(result["returned"]) for result in results)
            truncated = any(bool(result["truncated"]) for result in results)

            def parallel_cycle_rows() -> Iterable[dict[str, Any]]:
                # Worker files are bounded by ``cap`` in aggregate. Sorting
                # the compact file descriptors, not cycle memberships, keeps
                # controller memory independent of graph size.
                for result in sorted(results, key=lambda item: int(item["worker"])):
                    with Path(str(result["output_path"])).open(
                        "r", encoding="utf-8"
                    ) as source:
                        for line in source:
                            if not line.strip():
                                continue
                            record = json.loads(line)
                            cycle = [str(node) for node in record["nodes"]]
                            if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                                preview, node_count = self._sequence_preview(cycle)
                                displayed.append(preview)
                                cycle_lengths.append(node_count)
                            yield from self._cycle_rows(
                                cycle,
                                cycle_index=int(record["cycle_index"]),
                            )

            rows = parallel_cycle_rows()
            parallel = {
                "strategy": "cycle-start-map/bounded-artifact-merge",
                "workers": parallel_workers,
                "cycles_returned": returned,
            }
        else:
            rows = cycle_rows()

        result_files, _, artifact_rows = self._write_rows(
            "simple-cycles", rows
        )
        summary = {
            "success": True, "has_cycles": returned > 0, "cycles": displayed,
            "truncated": truncated,
            "count_returned": returned,
            "total_cycles_at_least": returned + (1 if truncated else 0),
            "cycle_lengths_preview": cycle_lengths,
            "cycles_preview_truncated": (
                returned > len(displayed)
                or any(
                    length > len(cycle)
                    for length, cycle in zip(cycle_lengths, displayed)
                )
            ),
            "cycle_artifact_rows": artifact_rows,
            "note": f"已枚举 {returned} 个简单环；图可能存在更多环。" if truncated else None,
        }
        if parallel is not None:
            summary["parallel"] = parallel
        return {
            "summary": summary,
            "preview": displayed,
            "result_files": result_files,
        }

    def maximum_cycle(self, p: dict[str, Any]) -> dict[str, Any]:
        optimize = str(p.get("optimize_by", "length")).strip().lower()
        if optimize not in {"length", "weight"}:
            summary = {"success": False, "error": "optimize_by 仅支持 length 或 weight"}
            return {"summary": summary, "preview": [], "result_files": []}
        cap = max(1, min(int(p.get("max_cycles", 10_000)), 100_000))
        best: list[str] | None = None
        best_weight = 0.0
        best_key: tuple[float, float] = (-1.0, -math.inf)
        examined = 0
        truncated = False
        parallel: dict[str, Any] | None = None
        parallel_workers = min(
            self.workers,
            max(1, self.graph.partitions),
            cap,
            os.cpu_count() or 1,
        )
        if parallel_workers > 1:
            context = process_context()
            # The counter is passed while spawn creates each process (via the
            # initializer), which is the supported way to share SemLock state;
            # it is never pickled into an already-running pool task.
            shared_counter = context.Value("i", 0)
            args = [
                (
                    str(self.graph.manifest_path),
                    worker_index,
                    parallel_workers,
                    optimize,
                    str(self.work_root),
                )
                for worker_index in range(parallel_workers)
            ]
            results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=parallel_workers,
                mp_context=context,
                initializer=_init_bounded_enumeration_worker,
                initargs=(shared_counter, cap),
            ) as pool:
                futures = [
                    pool.submit(_maximum_cycle_start_partition, item)
                    for item in args
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    self._emit(
                        "algorithm_map",
                        0.20 + 0.55 * completed / len(futures),
                        "最大环起始节点分片扫描中",
                        workers=parallel_workers,
                        workers_completed=completed,
                    )
            examined = sum(int(result["examined"]) for result in results)
            truncated = any(bool(result["truncated"]) for result in results)
            candidates = [result for result in results if result.get("best_path")]
            if candidates:
                selected = max(
                    candidates,
                    key=lambda result: (
                        tuple(float(value) for value in result["best_key"]),
                        -int(result["worker"]),
                    ),
                )
                best_key = tuple(
                    float(value) for value in selected["best_key"]
                )
                best_weight = float(selected["best_weight"])
                with Path(str(selected["best_path"])).open(
                    "r", encoding="utf-8"
                ) as source:
                    best = [
                        str(json.loads(line)) for line in source if line.strip()
                    ]
            parallel = {
                "strategy": "cycle-start-map/max-reduce",
                "workers": parallel_workers,
                "cycles_examined": examined,
            }
        else:
            for cycle in self._iter_cycles():
                if examined >= cap:
                    truncated = True
                    break
                examined += 1
                weight = self._cycle_weight(cycle)
                key = (
                    (weight, len(cycle))
                    if optimize == "weight"
                    else (len(cycle), weight)
                )
                if key > best_key:
                    best_key, best, best_weight = key, cycle, weight
        if best is None:
            summary = {
                "success": True, "has_cycle": False, "cycle": [],
                "cycle_length": 0, "cycle_weight": 0.0,
                "cycles_examined": examined, "search_exhausted": not truncated,
                "truncated": truncated, "optimize_by": optimize,
            }
            if parallel is not None:
                summary["parallel"] = parallel
            return {"summary": summary, "preview": [], "result_files": []}
        payload = self._cycle_payload(best, total_weight=best_weight)
        result_files, _, artifact_rows = self._write_rows(
            "maximum-cycle", self._cycle_rows(best)
        )
        summary = {
            "success": True, "has_cycle": True,
            **payload,
            "cycles_examined": examined,
            "search_exhausted": not truncated, "truncated": truncated,
            "optimize_by": optimize,
            "cycle_artifact_rows": artifact_rows,
            "note": (
                f"已扫描 {examined} 个简单环后达到上限；返回已扫描范围内的最大环。"
                if truncated else None
            ),
        }
        if parallel is not None:
            summary["parallel"] = parallel
        return {
            "summary": summary,
            "preview": list(payload["edges_preview"]),
            "result_files": result_files,
        }

    # ── 团分析（新增，枚举状态有界）────────────────────────────────

    def _is_adjacent_undirected(self, left: str, right: str) -> bool:
        return self.graph.edge_weight(left, right, undirected=True) is not None

    def _iter_maximal_cliques(
        self,
        *,
        root_worker: int | None = None,
        root_worker_count: int = 1,
    ) -> Iterable[list[str]]:
        """Yield maximal cliques with only one DFS branch resident.

        Materialising the candidates of every branch before visiting any one
        branch creates an exponential Python stack on dense graphs.  A frame
        here is instead a lazy, ordered adjacency iterator.  Suspended parent
        iterators resume after the child has been exhausted, so resident
        Python state is proportional to the current clique depth rather than
        to the number (or degree) of unexplored branches.
        """
        for root in self.graph.iter_node_ids():
            if (
                root_worker is not None
                and _stable_partition(root, max(1, int(root_worker_count)))
                != int(root_worker)
            ):
                continue
            clique = [str(root)]
            clique_nodes = {str(root)}
            cursors = [
                iter(self.graph.iter_neighbors(str(root), undirected=True))
            ]
            extended = [False]
            while cursors:
                try:
                    candidate, _ = next(cursors[-1])
                except StopIteration:
                    cursors.pop()
                    was_extended = extended.pop()
                    if not was_extended:
                        # Check whether any vertex outside the ordered branch
                        # can extend it; if so, this leaf is not globally
                        # maximal.  Only the current path is held in memory.
                        maximal = True
                        for node in self.graph.iter_node_ids():
                            node = str(node)
                            if node in clique_nodes:
                                continue
                            if all(
                                self._is_adjacent_undirected(node, member)
                                for member in clique
                            ):
                                maximal = False
                                break
                        if maximal:
                            yield list(clique)

                    removed = clique.pop()
                    clique_nodes.remove(removed)
                    continue

                candidate = str(candidate)
                # Strictly increasing identifiers give every clique one
                # deterministic branch rooted at its least member.
                if candidate <= clique[-1] or candidate in clique_nodes:
                    continue
                if not all(
                    self._is_adjacent_undirected(candidate, member)
                    for member in clique
                ):
                    continue
                extended[-1] = True
                clique.append(candidate)
                clique_nodes.add(candidate)
                cursors.append(
                    iter(self.graph.iter_neighbors(candidate, undirected=True))
                )
                extended.append(False)

    def clique_analysis(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        if method not in {"find_cliques", "clique_number", "number_of_cliques"}:
            summary = {"success": False, "error": f"未知方法 '{method}'"}
            return {"summary": summary, "preview": [], "result_files": []}
        parallel_workers = min(
            self.workers,
            max(1, self.graph.partitions),
            os.cpu_count() or 1,
        )

        def run_partitions(
            mode: str,
            *,
            requested_node: str | None = None,
            cap: int | None = None,
        ) -> list[dict[str, Any]]:
            context = process_context()
            args = [
                (
                    str(self.graph.manifest_path),
                    worker_index,
                    parallel_workers,
                    mode,
                    requested_node,
                    str(self.work_root),
                )
                for worker_index in range(parallel_workers)
            ]
            pool_options: dict[str, Any] = {}
            if cap is not None:
                shared_counter = context.Value("i", 0)
                pool_options = {
                    "initializer": _init_bounded_enumeration_worker,
                    "initargs": (shared_counter, cap),
                }
            results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=parallel_workers,
                mp_context=context,
                **pool_options,
            ) as pool:
                futures = [
                    pool.submit(_clique_start_partition, item) for item in args
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    self._emit(
                        "algorithm_map",
                        0.20 + 0.55 * completed / len(futures),
                        "极大团根节点分片枚举中",
                        mode=mode,
                        workers=parallel_workers,
                        workers_completed=completed,
                    )
            return results

        def member_rows(
            clique: list[str], clique_index: int
        ) -> Iterable[dict[str, Any]]:
            for position, node in enumerate(clique):
                yield {
                    "kind": "clique_member",
                    "clique_index": clique_index,
                    "position": position,
                    "clique_size": len(clique),
                    "node_id": node,
                    "node": self._display(node),
                }

        if method == "find_cliques":
            cap = _limit(p)
            displayed: list[list[str]] = []
            clique_sizes: list[int] = []
            returned = 0
            truncated = False
            parallel: dict[str, Any] | None = None

            def clique_rows() -> Iterable[dict[str, Any]]:
                nonlocal returned, truncated
                for clique in self._iter_maximal_cliques():
                    if returned >= cap:
                        truncated = True
                        return
                    clique_index = returned
                    returned += 1
                    if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                        preview, node_count = self._sequence_preview(clique)
                        displayed.append(preview)
                        clique_sizes.append(node_count)
                    yield from member_rows(clique, clique_index)

            rows: Iterable[dict[str, Any]]
            if parallel_workers > 1:
                results = run_partitions(method, cap=cap)
                returned = sum(int(result["returned"]) for result in results)
                truncated = any(bool(result["truncated"]) for result in results)

                def parallel_clique_rows() -> Iterable[dict[str, Any]]:
                    for result in sorted(
                        results, key=lambda item: int(item["worker"])
                    ):
                        with Path(str(result["output_path"])).open(
                            "r", encoding="utf-8"
                        ) as source:
                            for line in source:
                                if not line.strip():
                                    continue
                                record = json.loads(line)
                                clique = [
                                    str(node) for node in record["nodes"]
                                ]
                                if len(displayed) < _SEQUENCE_PREVIEW_LIMIT:
                                    preview, node_count = self._sequence_preview(
                                        clique
                                    )
                                    displayed.append(preview)
                                    clique_sizes.append(node_count)
                                yield from member_rows(
                                    clique, int(record["clique_index"])
                                )

                rows = parallel_clique_rows()
                parallel = {
                    "strategy": "clique-root-map/bounded-artifact-merge",
                    "workers": parallel_workers,
                    "cliques_returned": returned,
                }
            else:
                rows = clique_rows()

            result_files, _, artifact_rows = self._write_rows(
                "maximal-cliques", rows
            )
            summary = {
                "success": True, "method": method, "cliques": displayed,
                "truncated": truncated,
                "count_returned": returned,
                "total_cliques_at_least": returned + (1 if truncated else 0),
                "clique_sizes_preview": clique_sizes,
                "cliques_preview_truncated": (
                    returned > len(displayed)
                    or any(
                        size > len(clique)
                        for size, clique in zip(clique_sizes, displayed)
                    )
                ),
                "clique_artifact_rows": artifact_rows,
                "note": f"已枚举 {returned} 个极大团；图中可能存在更多。" if truncated else None,
            }
            if parallel is not None:
                summary["parallel"] = parallel
            return {
                "summary": summary,
                "preview": displayed,
                "result_files": result_files,
            }
        if method == "clique_number":
            parallel = None
            if parallel_workers > 1:
                results = run_partitions(method)
                value = max(
                    (int(result["maximum_size"]) for result in results),
                    default=0,
                )
                parallel = {
                    "strategy": "clique-root-map/max-reduce",
                    "workers": parallel_workers,
                }
            else:
                value = max(
                    (len(clique) for clique in self._iter_maximal_cliques()),
                    default=0,
                )
            summary = {
                "success": True, "method": method,
                "value": value,
            }
            if parallel is not None:
                summary["parallel"] = parallel
            return {"summary": summary, "preview": [], "result_files": []}
        requested_node = self.graph.resolve_node(str(p.get("node") or ""))
        if p.get("node") and requested_node is None:
            summary = {"success": False, "error": f"节点 '{p.get('node')}' 不存在"}
            return {"summary": summary, "preview": [], "result_files": []}
        results: list[dict[str, Any]] = []
        if parallel_workers > 1:
            results = run_partitions(method, requested_node=requested_node)
            if requested_node:
                summary = {
                    "success": True,
                    "method": method,
                    "node": self._display(requested_node),
                    "value": sum(
                        int(result["requested_count"]) for result in results
                    ),
                    "parallel": {
                        "strategy": "clique-root-map/count-reduce",
                        "workers": parallel_workers,
                    },
                }
                return {"summary": summary, "preview": [], "result_files": []}
        connection = self._connection("clique-count")
        connection.execute(
            "CREATE TABLE counts(node TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO counts(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        if parallel_workers > 1:
            for result in results:
                count_path = str(result.get("count_path") or "")
                if not count_path:
                    continue
                with Path(count_path).open("r", encoding="utf-8") as source:
                    connection.executemany(
                        "UPDATE counts SET value=value+? WHERE node=?",
                        (
                            (int(value), str(node))
                            for node, value in (
                                json.loads(line)
                                for line in source
                                if line.strip()
                            )
                        ),
                    )
        else:
            for clique in self._iter_maximal_cliques():
                connection.executemany(
                    "UPDATE counts SET value=value+1 WHERE node=?",
                    ((node,) for node in clique),
                )
        connection.commit()
        if requested_node:
            value = int(
                connection.execute(
                    "SELECT value FROM counts WHERE node=?", (requested_node,)
                ).fetchone()[0]
            )
            summary = {
                "success": True, "method": method,
                "node": self._display(requested_node), "value": value,
            }
            connection.close()
            return {"summary": summary, "preview": [], "result_files": []}
        limit = _limit(p)
        top = [
            {"node": self._display(str(node)), "clique_count": int(value)}
            for node, value in connection.execute(
                "SELECT node,value FROM counts ORDER BY value DESC,node LIMIT ?", (limit,)
            )
        ]
        result_files, preview, count = self._write_rows(
            "clique-counts",
            (
                {
                    "node_id": str(node), "node": self._display(str(node)),
                    "clique_count": int(value),
                }
                for node, value in connection.execute(
                    "SELECT node,value FROM counts ORDER BY node"
                )
            ),
        )
        summary = {
            "success": True, "method": method, "total_nodes": count,
            "top_nodes": top, "truncated": count > len(top),
        }
        if parallel_workers > 1:
            summary["parallel"] = {
                "strategy": "clique-root-map/disk-count-reduce",
                "workers": parallel_workers,
            }
        connection.close()
        return {"summary": summary, "preview": preview, "result_files": result_files}

    # ── 子图抽取（新增）────────────────────────────────────────────

    def _active_node_state(self, stem: str) -> sqlite3.Connection:
        connection = self._connection(stem)
        connection.execute(
            "CREATE TABLE active(node TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO active(node,value) VALUES (?,1)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        connection.commit()
        return connection

    def _selected_node_state(
        self, stem: str, nodes: Iterable[str]
    ) -> sqlite3.Connection:
        """Store only an explicitly selected subgraph's nodes.

        Ego and node-subset extraction used to initialise ``active`` with all
        V nodes and then switch almost all of them off.  That makes sampling a
        tiny subgraph from a large snapshot perform O(V) SQLite writes before
        any edge is emitted.  Core/truss algorithms still use the full active
        state; explicit selections use this sparse state instead.
        """
        connection = self._connection(stem)
        connection.execute(
            "CREATE TABLE active(node TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT OR IGNORE INTO active(node,value) VALUES (?,1)",
            ((str(node),) for node in nodes),
        )
        connection.commit()
        return connection

    @staticmethod
    def _is_active(connection: sqlite3.Connection, node: str) -> bool:
        row = connection.execute(
            "SELECT value FROM active WHERE node=?", (node,)
        ).fetchone()
        return bool(row and int(row[0]))

    def _core_state(self, k: int, stem: str = "k-core") -> sqlite3.Connection:
        if any(source == target for source, target, _, _ in self.graph.iter_edges()):
            raise ValueError(
                "Input graph has self loops which are not permitted for core algorithms"
            )
        connection = self._active_node_state(stem)
        connection.execute(
            "CREATE TABLE degree(node TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE peel(seq INTEGER PRIMARY KEY AUTOINCREMENT,node TEXT UNIQUE)"
        )
        for node in self.graph.iter_node_ids():
            # NetworkX defines directed core degree as in-degree + out-degree.
            # Reciprocal arcs therefore contribute twice and must not be
            # collapsed into one undirected neighbor.
            degree = self.graph.degree(node)
            connection.execute("INSERT INTO degree(node,value) VALUES (?,?)", (node, degree))
            if degree < k:
                connection.execute("INSERT OR IGNORE INTO peel(node) VALUES (?)", (node,))
        connection.commit()
        while True:
            row = connection.execute(
                "SELECT seq,node FROM peel ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                break
            connection.execute("DELETE FROM peel WHERE seq=?", (int(row[0]),))
            node = str(row[1])
            if not self._is_active(connection, node):
                continue
            connection.execute("UPDATE active SET value=0 WHERE node=?", (node,))

            def decrement(neighbor: str) -> None:
                if not self._is_active(connection, neighbor):
                    return
                connection.execute(
                    "UPDATE degree SET value=value-1 WHERE node=?", (neighbor,)
                )
                degree = int(
                    connection.execute(
                        "SELECT value FROM degree WHERE node=?", (neighbor,)
                    ).fetchone()[0]
                )
                if degree < k:
                    connection.execute(
                        "INSERT OR IGNORE INTO peel(node) VALUES (?)", (neighbor,)
                    )

            if self.graph.directed:
                # Remove each incident arc separately: u->v lowers v's
                # in-degree and x->u lowers x's out-degree.  A reciprocal pair
                # consequently lowers the surviving endpoint by two.
                for neighbor, _ in self.graph.iter_out_edges(node):
                    decrement(neighbor)
                for neighbor, _ in self.graph.iter_in_edges(node):
                    decrement(neighbor)
            else:
                for neighbor, _ in self.graph.iter_neighbors(node, undirected=True):
                    decrement(neighbor)
            connection.commit()
        return connection

    def _truss_state(self, k: int) -> sqlite3.Connection:
        connection = self._connection("k-truss")
        connection.executescript(
            """
            CREATE TABLE active(node TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID;
            CREATE TABLE active_edges(
                source TEXT NOT NULL,target TEXT NOT NULL,value INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            CREATE TABLE pending_edges(
                source TEXT NOT NULL,target TEXT NOT NULL,
                PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO active(node,value) VALUES (?,1)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        for source, target, _, _ in self.graph.iter_edges():
            if source == target:
                continue
            left, right = sorted((source, target))
            connection.execute(
                "INSERT OR IGNORE INTO active_edges(source,target,value) VALUES (?,?,1)",
                (left, right),
            )

        def edge_active(left: str, right: str) -> bool:
            left, right = sorted((left, right))
            row = connection.execute(
                "SELECT value FROM active_edges WHERE source=? AND target=?", (left, right)
            ).fetchone()
            return bool(row and int(row[0]))

        changed = True
        threshold = max(0, k - 2)
        while changed:
            changed = False
            # Peeling is synchronous: first persist every edge below the
            # support threshold, then deactivate the complete round at once.
            # ``pending_edges`` replaces a worst-case O(E) Python list and is
            # reused across rounds in the disk-backed work database.
            connection.execute("DELETE FROM pending_edges")
            for source, target in connection.execute(
                "SELECT source,target FROM active_edges WHERE value=1"
            ):
                source, target = str(source), str(target)
                support = 0
                for neighbor, _ in self.graph.iter_neighbors(source, undirected=True):
                    if neighbor != target and edge_active(source, neighbor) and edge_active(target, neighbor):
                        support += 1
                if support < threshold:
                    connection.execute(
                        "INSERT INTO pending_edges(source,target) VALUES (?,?)",
                        (source, target),
                    )
            if connection.execute(
                "SELECT 1 FROM pending_edges LIMIT 1"
            ).fetchone() is not None:
                changed = True
                connection.execute(
                    """
                    UPDATE active_edges SET value=0
                    WHERE EXISTS (
                        SELECT 1 FROM pending_edges
                        WHERE pending_edges.source=active_edges.source
                          AND pending_edges.target=active_edges.target
                    )
                    """
                )
                connection.commit()
        connection.execute(
            """
            UPDATE active SET value=0 WHERE node NOT IN (
                SELECT source FROM active_edges WHERE value=1
                UNION SELECT target FROM active_edges WHERE value=1
            )
            """
        )
        connection.commit()
        return connection

    def subgraph_extraction(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        k = max(1, int(p.get("k", 2)))
        try:
            if method == "ego_graph":
                node, error = self._required_node(p.get("node"), "节点")
                if error:
                    return {"summary": error, "preview": [], "result_files": []}
                assert node is not None
                reached = self._reachability_state(
                    node, undirected=not self.graph.directed,
                    max_depth=k, stem="ego-graph",
                )
                connection = self._selected_node_state(
                    "ego-active",
                    (
                        str(row[0])
                        for row in reached.execute("SELECT node FROM reached")
                    ),
                )
                reached.close()
            elif method == "node_subset":
                connection = self._selected_node_state(
                    "node-subset",
                    (
                        node
                        for raw in p.get("nodes") or []
                        if (node := self.graph.resolve_node(str(raw))) is not None
                    ),
                )
            elif method in {"k_core", "k_corona"}:
                connection = self._core_state(k, method)
                if method == "k_corona":
                    connection.execute(
                        "UPDATE active SET value=0 WHERE node IN (SELECT node FROM degree WHERE value<>?)",
                        (k,),
                    )
                    connection.commit()
            elif method == "k_truss":
                if self.graph.has_self_loops:
                    raise ValueError(
                        "Input graph has self loops which is not permitted; "
                        "please prepare with quality_policy.self_loops=skip or reject."
                    )
                connection = self._truss_state(max(2, k))
            else:
                summary = {"success": False, "error": f"未知方法 '{method}'"}
                return {"summary": summary, "preview": [], "result_files": []}
        except Exception as exc:
            summary = {"success": False, "error": str(exc)}
            return {"summary": summary, "preview": [], "result_files": []}

        node_count = int(
            connection.execute("SELECT COUNT(*) FROM active WHERE value=1").fetchone()[0]
        )
        limit = _limit(p)
        node_preview = [
            self._display(str(row[0]))
            for row in connection.execute(
                "SELECT node FROM active WHERE value=1 ORDER BY node LIMIT ?", (limit,)
            )
        ]
        edge_preview_capture: list[dict[str, Any]] = []
        sparse_index_scan = (
            method in {"ego_graph", "node_subset"}
            and (
                node_count <= 10_000
                or node_count * 16 <= max(1, self.graph.node_count)
            )
        )

        def edge_row(source: str, target: str, weight: float) -> dict[str, Any]:
            return {
                "kind": "edge", "source_id": source,
                "target_id": target, "from": self._display(source),
                "to": self._display(target), "weight": weight,
            }

        def rows() -> Iterable[dict[str, Any]]:
            for (node,) in connection.execute(
                "SELECT node FROM active WHERE value=1 ORDER BY node"
            ):
                yield {"kind": "node", "node_id": str(node), "node": self._display(str(node))}
            if method == "k_truss":
                edges = connection.execute(
                    "SELECT source,target FROM active_edges WHERE value=1 ORDER BY source,target"
                )
                for source, target in edges:
                    weight = self.graph.edge_weight(
                        str(source), str(target), undirected=True
                    )
                    row = {
                        "kind": "edge", "source_id": str(source),
                        "target_id": str(target),
                        "from": self._display(str(source)),
                        "to": self._display(str(target)),
                        "weight": float(weight if weight is not None else 1.0),
                    }
                    if len(edge_preview_capture) < limit:
                        edge_preview_capture.append(row)
                    yield row
            elif sparse_index_scan:
                # For a small explicit selection, use the already-built
                # adjacency index.  This reads edges incident to the selected
                # nodes instead of scanning every edge in the parent graph.
                for (raw_source,) in connection.execute(
                    "SELECT node FROM active WHERE value=1 ORDER BY node"
                ):
                    source = str(raw_source)
                    for target, weight in self.graph.iter_out_edges(source):
                        target = str(target)
                        if not self._is_active(connection, target):
                            continue
                        # An undirected adjacency exposes an edge from both
                        # endpoints; emit one canonical orientation.
                        if not self.graph.directed and source > target:
                            continue
                        row = edge_row(source, target, float(weight))
                        if len(edge_preview_capture) < limit:
                            edge_preview_capture.append(row)
                        yield row
            else:
                for source, target, weight, _ in self.graph.iter_edges():
                    if self._is_active(connection, source) and self._is_active(connection, target):
                        row = edge_row(source, target, weight)
                        if len(edge_preview_capture) < limit:
                            edge_preview_capture.append(row)
                        yield row

        result_files, artifact_preview, row_count = self._write_rows(
            f"subgraph-{method}", rows()
        )
        edge_count = max(0, row_count - node_count)
        # Artifact preview is node-first for readability; retain an independent
        # edge preview so a graph with >20 nodes still exposes representative edges.
        # ``kind`` is an artifact row discriminator for the mixed node/edge
        # JSONL stream; it is not part of the established small-graph summary
        # edge schema.
        edge_preview = [
            {
                key: value for key, value in row.items()
                if key not in {"kind", "source_id", "target_id"}
            }
            for row in edge_preview_capture[:limit]
        ]
        summary = {
            "success": True, "method": method, "num_nodes": node_count,
            "num_edges": edge_count, "nodes_preview": node_preview,
            "nodes_truncated": node_count > len(node_preview),
            "edges_preview": edge_preview, "edges_truncated": edge_count > len(edge_preview),
            "edge_scan_strategy": (
                "selected-node-adjacency-index"
                if sparse_index_scan
                else "partition-edge-scan"
            ),
        }
        connection.close()
        return {
            "summary": summary, "preview": artifact_preview,
            "result_files": result_files,
        }

    # ── 链路预测（新增）────────────────────────────────────────────

    def _link_score(
        self, connection: sqlite3.Connection, method: str, source: str, target: str
    ) -> float:
        connection.executescript(
            """
            DELETE FROM neighbors_a;
            DELETE FROM neighbors_b;
            """
        )
        connection.executemany(
            "INSERT OR IGNORE INTO neighbors_a(node) VALUES (?)",
            ((node,) for node, _ in self.graph.iter_neighbors(source, undirected=True)),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO neighbors_b(node) VALUES (?)",
            ((node,) for node, _ in self.graph.iter_neighbors(target, undirected=True)),
        )
        # NetworkX scores on ``g.to_undirected()``.  A reciprocal pair is one
        # neighbor, while a self-loop contributes two to degree even though it
        # appears once in the neighbor set.  Start from the distinct neighbor
        # count and add the loop's second incidence.
        neighbor_count_a = int(
            connection.execute("SELECT COUNT(*) FROM neighbors_a").fetchone()[0]
        )
        neighbor_count_b = int(
            connection.execute("SELECT COUNT(*) FROM neighbors_b").fetchone()[0]
        )
        degree_a = neighbor_count_a + int(
            self._is_adjacent_undirected(source, source)
        )
        degree_b = neighbor_count_b + int(
            self._is_adjacent_undirected(target, target)
        )
        if method == "preferential_attachment":
            return float(degree_a * degree_b)
        common = (
            str(row[0])
            for row in connection.execute(
                """
                SELECT neighbors_a.node FROM neighbors_a
                JOIN neighbors_b ON neighbors_a.node=neighbors_b.node
                """
            )
        )
        if method == "jaccard":
            common_count = sum(1 for _ in common)
            # Jaccard is defined over neighbor sets, so a loop appears once
            # here even though it contributes two incidences to degree-based
            # scores below.
            union = neighbor_count_a + neighbor_count_b - common_count
            return common_count / union if union else 0.0
        score = 0.0
        for node in common:
            degree = sum(1 for _ in self.graph.iter_neighbors(node, undirected=True))
            degree += int(self._is_adjacent_undirected(node, node))
            if method == "adamic_adar":
                if degree > 1:
                    score += 1.0 / math.log(degree)
            elif degree:
                score += 1.0 / degree
        return score

    def link_prediction_score(self, p: dict[str, Any]) -> dict[str, Any]:
        method = str(p.get("method", "")).strip().lower()
        available = {
            "jaccard", "adamic_adar", "resource_allocation", "preferential_attachment"
        }
        if method not in available:
            summary = {"success": False, "error": f"未知方法 '{method}'"}
            return {"summary": summary, "preview": [], "result_files": []}
        connection = self._connection("link-prediction")
        connection.executescript(
            """
            CREATE TABLE neighbors_a(node TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE neighbors_b(node TEXT PRIMARY KEY) WITHOUT ROWID;
            """
        )
        source_raw, target_raw = p.get("source"), p.get("target")
        if source_raw is not None and target_raw is not None:
            source, error = self._required_node(source_raw, "源点")
            if error:
                connection.close()
                return {"summary": error, "preview": [], "result_files": []}
            target, error = self._required_node(target_raw, "目标点")
            if error:
                connection.close()
                return {"summary": error, "preview": [], "result_files": []}
            assert source is not None and target is not None
            score = self._link_score(connection, method, source, target)
            scores = [
                {
                    "source": self._display(source), "target": self._display(target),
                    "score": round(score, 6),
                }
            ]
            connection.close()
            summary = {"success": True, "method": method, "scores": scores}
            return {"summary": summary, "preview": scores, "result_files": []}
        raw_top_k = p.get("top_k", 10)
        top_k = (
            _MAX_LIMIT
            if isinstance(raw_top_k, str)
            and raw_top_k.strip().lower() == "all"
            else max(1, min(int(raw_top_k), _MAX_LIMIT))
        )
        top: list[tuple[float, str, str]] = []
        parallel_workers = min(self.workers, max(1, self.graph.partitions))
        source_count = 0
        if parallel_workers > 1:
            # Added: source batches are independent, so global top-k scoring
            # can use processes without sharing mutable SQLite state.
            connection.close()
            args = [
                (
                    str(self.graph.manifest_path),
                    method,
                    worker_index,
                    parallel_workers,
                    top_k,
                    str(self.work_root),
                )
                for worker_index in range(parallel_workers)
            ]
            with ProcessPoolExecutor(
                max_workers=parallel_workers, mp_context=process_context()
            ) as pool:
                futures = [pool.submit(_link_prediction_source_partition, item) for item in args]
                for completed, future in enumerate(as_completed(futures), 1):
                    result = future.result()
                    source_count += int(result["source_count"])
                    for candidate in result["top"]:
                        if len(top) < top_k:
                            heapq.heappush(top, tuple(candidate))
                        elif tuple(candidate) > top[0]:
                            heapq.heapreplace(top, tuple(candidate))
                    self._emit(
                        "algorithm_map",
                        0.20 + 0.55 * completed / len(futures),
                        "链路预测源点分批计算中",
                        workers=parallel_workers,
                        workers_completed=completed,
                    )
        else:
            for source in self.graph.iter_node_ids():
                source_count += 1
                for target in self.graph.iter_node_ids():
                    if target <= source or self._is_adjacent_undirected(source, target):
                        continue
                    score = self._link_score(connection, method, source, target)
                    candidate = (score, source, target)
                    if len(top) < top_k:
                        heapq.heappush(top, candidate)
                    elif candidate > top[0]:
                        heapq.heapreplace(top, candidate)
            connection.close()
        ranked = sorted(top, reverse=True)
        scores = [
            {
                "source": self._display(source), "target": self._display(target),
                "score": round(score, 6),
            }
            for score, source, target in ranked
        ]
        summary = {
            "success": True,
            "method": method,
            "scores": scores,
            "execution_mode": "bounded",
            "parallel": {
                "strategy": "source-batch-map/top-k-reduce",
                "workers": parallel_workers,
                "source_count": source_count,
            },
        }
        return {"summary": summary, "preview": scores, "result_files": []}

    # ── 图级指标（新增）────────────────────────────────────────────

    def _distance_extrema(
        self, *, need_average: bool = False, weighted: bool = False
    ) -> tuple[float, float, float]:
        components = self._component_state("metric-components", undirected=True)
        largest = components.execute(
            """
            SELECT component,COUNT(*) AS size FROM components
            GROUP BY component ORDER BY size DESC,component LIMIT 1
            """
        ).fetchone()
        if largest is None:
            components.close()
            return 0.0, 0.0, 0.0
        component_id, size = int(largest[0]), int(largest[1])
        total_distance = 0.0
        pair_count = 0
        diameter = 0.0
        radius = math.inf
        for (source,) in components.execute(
            "SELECT node FROM components WHERE component=? ORDER BY node",
            (component_id,),
        ):
            distances = self._shortest_state(
                str(source), weighted=weighted, direction="both", stem="metric-source"
            )
            count, total, eccentricity = distances.execute(
                """
                SELECT COUNT(*),COALESCE(SUM(distance),0),COALESCE(MAX(distance),0)
                FROM distances WHERE node<>?
                """,
                (str(source),),
            ).fetchone()
            distances.close()
            total_distance += float(total)
            pair_count += int(count)
            diameter = max(diameter, float(eccentricity))
            radius = min(radius, float(eccentricity))
        components.close()
        average = total_distance / pair_count if pair_count else 0.0
        return average if need_average else 0.0, diameter, 0.0 if math.isinf(radius) else radius

    def _distance_batch_aggregate(
        self,
        *,
        mode: str,
        weighted: bool,
        direction: str,
        source_path: Path | None,
        progress_message: str,
    ) -> tuple[float, float, float, dict[str, Any]]:
        results = self._run_distance_source_batches(
            mode=mode,
            weighted=weighted,
            direction=direction,
            source_path=source_path,
            progress_message=progress_message,
        )
        source_count = sum(int(row["source_count"]) for row in results)
        pair_count = sum(int(row["pair_count"]) for row in results)
        total_distance = sum(float(row["total_distance"]) for row in results)
        diameter = max((float(row["diameter"]) for row in results), default=0.0)
        radii = [
            float(row["radius"])
            for row in results
            if int(row["source_count"]) and row["radius"] is not None
        ]
        radius = min(radii) if radii else 0.0
        parallel = {
            "strategy": "source-batch-map/scalar-reduce",
            "workers": min(self.workers, self.graph.partitions),
            "source_count": source_count,
        }
        return (
            total_distance / pair_count if pair_count else 0.0,
            diameter,
            radius,
            parallel,
        )

    def _distance_extrema_parallel(
        self, *, weighted: bool, mode: str
    ) -> tuple[float, float, float, dict[str, Any]]:
        """Evaluate the largest undirected component using source batches."""
        components = self._component_state(f"{mode}-components", undirected=True)
        try:
            largest = components.execute(
                """
                SELECT component,COUNT(*) AS size FROM components
                GROUP BY component ORDER BY size DESC,component LIMIT 1
                """
            ).fetchone()
            if largest is None:
                return 0.0, 0.0, 0.0, {
                    "strategy": "source-batch-map/scalar-reduce",
                    "workers": min(self.workers, self.graph.partitions),
                    "source_count": 0,
                }
            source_path = self.work_root / f"{mode}-sources.jsonl"
            with source_path.open("w", encoding="utf-8") as output:
                for (node,) in components.execute(
                    "SELECT node FROM components WHERE component=? ORDER BY node",
                    (int(largest[0]),),
                ):
                    output.write(json.dumps(str(node), ensure_ascii=False) + "\n")
        finally:
            components.close()
        return self._distance_batch_aggregate(
            mode=mode,
            weighted=weighted,
            direction="both",
            source_path=source_path,
            progress_message="图级最短距离源点分批计算中",
        )

    def _undirected_projection(self, stem: str) -> sqlite3.Connection:
        """Build the exact simple-Graph projection used by ``g.to_undirected``.

        For reciprocal directed arcs NetworkX updates the undirected edge in
        source-node iteration order. Snapshot node order records upload-time
        insertion order, so the source with the later ordinal supplies the
        final weight. Self loops remain in the projection for NetworkX's
        weighted normalisation but are excluded from local neighborhoods.
        """
        connection = self._connection(stem)
        connection.executescript(
            """
            CREATE TABLE node_order(
                node TEXT PRIMARY KEY,ordinal INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE projected_edges(
                source TEXT NOT NULL,target TEXT NOT NULL,weight REAL NOT NULL,
                priority INTEGER NOT NULL,
                PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO node_order(node,ordinal) VALUES (?,?)",
            ((node, ordinal) for ordinal, node in enumerate(self.graph.iter_node_ids())),
        )
        for source, target, weight, _ in self.graph.iter_edges():
            left, right = sorted((source, target))
            if self.graph.directed:
                row = connection.execute(
                    "SELECT ordinal FROM node_order WHERE node=?", (source,)
                ).fetchone()
                priority = int(row[0]) if row is not None else 0
            else:
                priority = 0
            connection.execute(
                """
                INSERT INTO projected_edges(source,target,weight,priority)
                VALUES (?,?,?,?)
                ON CONFLICT(source,target) DO UPDATE SET
                    weight=CASE WHEN excluded.priority>=priority
                                THEN excluded.weight ELSE weight END,
                    priority=MAX(priority,excluded.priority)
                """,
                (left, right, float(weight), priority),
            )
        connection.commit()
        return connection

    def _triangle_metrics(
        self,
    ) -> tuple[float, float, dict[str, Any] | None]:
        connection = self._undirected_projection("triangles")
        max_weight_row = connection.execute(
            "SELECT MAX(weight) FROM projected_edges"
        ).fetchone()
        max_weight = (
            1.0 if max_weight_row is None or max_weight_row[0] is None
            else float(max_weight_row[0])
        )
        worker_count = min(
            self.workers,
            max(1, self.graph.partitions),
            os.cpu_count() or 1,
        )
        parallel: dict[str, Any] | None = None
        if worker_count > 1 and self.graph.node_count:
            # The projection is immutable after the commit in
            # ``_undirected_projection``. Close the writer before subprocesses
            # open the same file read-only, then reduce four bounded scalars.
            projection_path = self.work_root / "triangles.sqlite3"
            connection.close()
            items = [
                (
                    str(self.graph.manifest_path),
                    str(projection_path),
                    worker,
                    worker_count,
                    max_weight,
                )
                for worker in range(worker_count)
            ]
            results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=worker_count, mp_context=process_context()
            ) as pool:
                futures = [pool.submit(_triangle_node_partition, item) for item in items]
                for completed, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    self._emit(
                        "algorithm_map",
                        0.15 + 0.65 * completed / worker_count,
                        "三角形与聚类节点分区计算中",
                        workers_completed=completed,
                        workers=worker_count,
                    )
            closed_triplets = sum(
                int(result["closed_triplets"]) for result in results
            )
            triplets = sum(int(result["triplets"]) for result in results)
            clustering_sum = sum(
                float(result["clustering_sum"]) for result in results
            )
            node_count = sum(int(result["node_count"]) for result in results)
            if node_count != self.graph.node_count:
                raise RuntimeError("三角形节点分区归约数量与快照不一致")
            parallel = {
                "strategy": "node-partition-map/scalar-reduce",
                "workers": worker_count,
                "node_count": node_count,
            }
        else:
            (
                closed_triplets,
                triplets,
                clustering_sum,
                _node_count,
            ) = _triangle_node_aggregate(
                connection, self.graph.iter_node_ids(), max_weight
            )
            connection.close()
        transitivity = closed_triplets / triplets if triplets else 0.0
        average_clustering = clustering_sum / max(1, self.graph.node_count)
        return transitivity, average_clustering, parallel

    def _degree_assortativity(self) -> tuple[float, str | None]:
        connection = self._connection("degree-assortativity")
        connection.executescript(
            """
            CREATE TABLE undirected_edges(
                source TEXT NOT NULL,target TEXT NOT NULL,
                PRIMARY KEY(source,target)
            ) WITHOUT ROWID;
            CREATE TABLE degree(
                node TEXT PRIMARY KEY,value INTEGER NOT NULL DEFAULT 0
            ) WITHOUT ROWID;
            """
        )
        connection.executemany(
            "INSERT INTO degree(node,value) VALUES (?,0)",
            ((node,) for node in self.graph.iter_node_ids()),
        )
        # Match ``g.to_undirected()`` on the small path: reciprocal directed
        # arcs collapse to one unweighted edge.  Precomputing every endpoint
        # degree once avoids the former O(E*d) repeated adjacency scans.
        for source, target, _, _ in self.graph.iter_edges():
            left, right = sorted((source, target))
            inserted = connection.execute(
                "INSERT OR IGNORE INTO undirected_edges(source,target) VALUES (?,?)",
                (left, right),
            ).rowcount
            if not inserted:
                continue
            if left == right:
                connection.execute(
                    "UPDATE degree SET value=value+2 WHERE node=?", (left,)
                )
            else:
                connection.execute(
                    "UPDATE degree SET value=value+1 WHERE node IN (?,?)",
                    (left, right),
                )
        connection.commit()

        count = sum_x = sum_y = sum_x2 = sum_y2 = sum_xy = 0.0
        for source, target, source_degree, target_degree in connection.execute(
            """
            SELECT edges.source,edges.target,source_degree.value,target_degree.value
            FROM undirected_edges AS edges
            JOIN degree AS source_degree ON source_degree.node=edges.source
            JOIN degree AS target_degree ON target_degree.node=edges.target
            ORDER BY edges.source,edges.target
            """
        ):
            x, y = float(source_degree), float(target_degree)
            orientations = ((x, y),) if source == target else ((x, y), (y, x))
            for left_degree, right_degree in orientations:
                count += 1
                sum_x += left_degree
                sum_y += right_degree
                sum_x2 += left_degree * left_degree
                sum_y2 += right_degree * right_degree
                sum_xy += left_degree * right_degree
        connection.close()
        if not count:
            return float("nan"), "no_edges"
        numerator = count * sum_xy - sum_x * sum_y
        denominator = math.sqrt(
            max(0.0, count * sum_x2 - sum_x * sum_x)
            * max(0.0, count * sum_y2 - sum_y * sum_y)
        )
        if not denominator:
            return float("nan"), "zero_endpoint_degree_variance"
        return numerator / denominator, None

    def global_metrics(self, p: dict[str, Any]) -> dict[str, Any]:
        metric = str(p.get("metric", "")).strip().lower()
        available = {
            "density", "diameter", "radius", "transitivity",
            "average_clustering", "degree_assortativity",
        }
        if metric not in available:
            summary = {"success": False, "error": f"未知指标 '{metric}'"}
            return {"summary": summary, "preview": [], "result_files": []}
        parallel: dict[str, Any] | None = None
        if metric == "density":
            n = self.graph.node_count
            denominator = n * (n - 1)
            if not self.graph.directed:
                denominator /= 2
            value = self.graph.edge_count / denominator if denominator else 0.0
        elif metric in {"diameter", "radius"}:
            if self.workers > 1 and self.graph.partitions > 1:
                _, diameter, radius, parallel = self._distance_extrema_parallel(
                    weighted=False, mode="metric-extrema"
                )
            else:
                _, diameter, radius = self._distance_extrema(weighted=False)
            value = diameter if metric == "diameter" else radius
        elif metric in {"transitivity", "average_clustering"}:
            transitivity, clustering, parallel = self._triangle_metrics()
            value = transitivity if metric == "transitivity" else clustering
        else:
            value, undefined_reason_code = self._degree_assortativity()
        finite = math.isfinite(float(value))
        summary = {
            "success": True,
            "metric": metric,
            "value": round(float(value), 6) if finite else None,
            "defined": finite,
        }
        if metric == "degree_assortativity" and not finite:
            if undefined_reason_code == "no_edges":
                undefined_reason = (
                    "度同配性在数学上未定义：图中没有可用于比较端点度数的边；"
                    "这不表示计算引擎不支持该指标。"
                )
            else:
                undefined_reason = (
                    "度同配性在数学上未定义：边端点的度数没有方差，Pearson "
                    "相关系数的分母为 0；这不表示计算引擎不支持该指标。"
                )
            summary.update(
                {
                    "undefined_reason_code": (
                        undefined_reason_code
                        or "zero_endpoint_degree_variance"
                    ),
                    "undefined_reason": undefined_reason,
                }
            )
        if parallel is not None:
            summary["parallel"] = parallel
        return {"summary": summary, "preview": [], "result_files": []}
