"""Compute routing and single-machine resource admission."""

from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

from .compatibility import (
    GRAPHSCOPE_ONLY_ALGORITHM_NAMES,
    SMALL_GRAPH_ALGORITHM_MODE_DEFAULTS,
    SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS,
    SMALL_GRAPH_ALGORITHM_MODES,
)
from .algorithms import LargeGraphAlgorithms, brandes_sample_sources_limit
from .contracts import LargeGraphCapability, RouteDecision


def _true_flag(value: Any) -> bool:
    """Interpret only explicit boolean-like true values as an override."""

    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer guard without letting bad deployment input crash routing."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    return max(1, value)


def _nonnegative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = int(default)
    return max(0, value)


class ResourceManager:
    """Conservative resource estimator for one heavy local job at a time."""

    def __init__(
        self,
        work_root: str | Path,
        *,
        memory_reserve_ratio: float | None = None,
        disk_reserve_bytes: int | None = None,
    ) -> None:
        self.work_root = Path(work_root)
        self.memory_reserve_ratio = float(
            memory_reserve_ratio
            if memory_reserve_ratio is not None
            else os.getenv("LARGE_GRAPH_MEMORY_RESERVE_RATIO", "0.25")
        )
        self.disk_reserve_bytes = int(
            disk_reserve_bytes
            if disk_reserve_bytes is not None
            else os.getenv("LARGE_GRAPH_DISK_RESERVE_BYTES", str(2 * 1024**3))
        )

    @staticmethod
    def available_memory_bytes() -> int:
        # Linux exposes the memory that can be allocated without swapping in
        # MemAvailable.  sysconf remains a portable fallback for tests/other OSes.
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        try:
            return int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (AttributeError, OSError, ValueError):
            return 0

    @staticmethod
    def maximum_estimated_work_units() -> int:
        return _nonnegative_int_env(
            "LARGE_GRAPH_MAX_ESTIMATED_WORK_UNITS", 2_000_000_000
        )

    @classmethod
    def brandes_workload_source_limit(
        cls, node_count: int, edge_count: int
    ) -> int | None:
        max_work = cls.maximum_estimated_work_units()
        if max_work <= 0:
            return None
        return min(
            max(1, int(node_count)),
            brandes_sample_sources_limit(),
            max_work // max(1, int(edge_count)),
        )

    @classmethod
    def default_brandes_sample_sources(
        cls, node_count: int, edge_count: int
    ) -> int:
        configured = min(
            _positive_int_env("LARGE_GRAPH_BRANDES_SOURCES", 64),
            brandes_sample_sources_limit(),
            max(1, int(node_count)),
        )
        workload_limit = cls.brandes_workload_source_limit(node_count, edge_count)
        if workload_limit is None:
            return configured
        return max(1, min(configured, max(1, workload_limit)))

    def estimate_preparation(
        self, source_bytes: int, partitions: int, workers: int | None = None
    ) -> dict[str, int]:
        # Normalised JSONL map output, external-sort runs, final shards and two
        # SQLite adjacency indexes coexist briefly.  Compact numeric edge lists
        # can expand by an order of magnitude, so source size needs a sizeable
        # multiplier rather than the former optimistic 3.2x estimate.
        try:
            disk_multiplier = max(
                4.0,
                float(os.getenv("LARGE_GRAPH_PREPARATION_DISK_MULTIPLIER", "12")),
            )
        except ValueError:
            disk_multiplier = 12.0
        return {
            # External-sort chunks are bounded, but several reducers run at
            # once.  Reserve a realistic floor instead of assuming file size
            # translates directly into resident memory.
            "memory_bytes": max(
                512 * 1024**2,
                min(partitions, workers or os.cpu_count() or 1) * 128 * 1024**2,
            ),
            "disk_bytes": max(
                256 * 1024**2,
                int(source_bytes * disk_multiplier),
                max(1, int(partitions)) * 16 * 1024**2,
            ),
        }

    def estimate_algorithm(
        self,
        node_count: int,
        edge_count: int,
        partitions: int,
        algorithm: str = "",
        parameters: dict[str, Any] | None = None,
        workers: int | None = None,
        snapshot_storage_bytes: int | dict[str, Any] | None = None,
    ) -> dict[str, int]:
        partitions = max(1, int(partitions))
        largest_node_partition = max(1, (node_count + partitions - 1) // partitions)
        cpu_count = os.cpu_count() or 1
        active_workers = max(
            1,
            min(
                partitions,
                max(1, int(workers)) if workers is not None else min(8, cpu_count),
                cpu_count,
                8,
            ),
        )
        name = str(algorithm).strip().lower()
        params = dict(parameters or {})
        mode_key = ComputeRouter._MODE_PARAMETER.get(name)
        mode = str(
            params.get(mode_key, ComputeRouter._MODE_DEFAULT.get(name, ""))
            if mode_key
            else ""
        ).strip().lower()
        # Modified for the complete catalogue: traversal/flow algorithms keep
        # graph-sized state on disk, while resident memory remains one node
        # partition plus bounded queues/previews.
        edge_disk_bytes = 48
        node_disk_bytes = 96
        if name in {"maximum_flow", "minimum_cut", "global_minimum_cut"}:
            edge_disk_bytes = 192
            node_disk_bytes = 160
        elif name in {
            "node_centrality", "graph_connectivity", "topological_sort",
            "minimum_spanning_tree", "subgraph_extraction", "global_metrics",
            "graph_structure_analysis", "connectivity_analysis",
            "tree_dag_analysis", "matching_coloring_analysis", "distance_measures",
        }:
            edge_disk_bytes = 112
            node_disk_bytes = 256
        # Added: retain a rough work estimate in the route decision.  It is
        # deliberately separate from byte estimates because quadratic and
        # exponential algorithms can be impossible even when their temporary
        # files fit on disk.
        if name == "shortest_path" and mode == "yen_k_shortest":
            # Yen runs one spur search for each prefix of every accepted path.
            # Its accepted paths alone retain O(k*V) node references, while
            # the candidate heap can grow beyond that and is guarded again by
            # the worker against its actual resident path-node count.
            yen_k = max(1, min(int(params.get("k", 3)), 200))
            work_units = max(
                1,
                yen_k * max(1, node_count) * max(1, edge_count),
            )
        elif name == "link_prediction_score" and not (
            params.get("source") is not None and params.get("target") is not None
        ):
            work_units = max(0, node_count * max(0, node_count - 1) // 2)
        elif name in {"clique_analysis", "find_cycles", "maximum_cycle"} or (
            name == "path_analysis" and mode == "all_simple_paths"
        ):
            work_units = max(1, node_count * max(1, edge_count))
        elif name == "node_centrality" and mode in {"betweenness", "load", "percolation"}:
            sources = max(
                1,
                min(
                    node_count,
                    int(
                        params.get(
                            "sample_sources",
                            os.getenv("LARGE_GRAPH_BRANDES_SOURCES", "64"),
                        )
                    ),
                ),
            )
            work_units = max(1, sources * max(1, edge_count))
        elif name == "node_centrality" and mode == "voterank":
            # VoteRank recomputes every node's vote by scanning the complete
            # edge set for each elected node.  In the worst case it elects all
            # nodes, so treating it as the generic O(V+E) centrality path can
            # admit a job that is orders of magnitude too large.
            work_units = max(1, node_count * max(1, edge_count))
        elif (
            name == "global_minimum_cut"
            or (name == "node_centrality" and mode in {"closeness", "harmonic"})
            or (name == "path_analysis" and mode == "average_shortest_path_length")
            or (name == "global_metrics" and mode in {"diameter", "radius"})
            or (name == "reachability" and mode == "transitive_closure")
        ):
            work_units = max(1, node_count * max(1, edge_count))
        elif name == "node_centrality" and mode in {"pagerank", "eigenvector", "katz"}:
            iterations = max(1, min(int(params.get("max_iterations", 100)), 2000))
            work_units = max(1, iterations * max(1, edge_count))
        elif name == "fluid_community":
            work_units = max(1, 10 * max(1, node_count + edge_count))
        elif name == "densest_subgraph":
            rounds = max(1, min(int(params.get("rounds", 10)), 1000))
            work_units = max(1, rounds * max(1, node_count + edge_count))
        elif name == "motif_counting":
            average_degree = max(1, edge_count // max(1, node_count))
            motif = str(params.get("motif", "")).strip().lower()
            if motif == "k_clique":
                clique_size = max(2, min(int(params.get("k", 3)), 8))
                exponent = max(1, clique_size - 2)
            elif motif in {
                "rectangle",
                "diamond",
                "tailed_triangle",
                "cycle_plus_triangle",
            }:
                exponent = 2
            else:
                exponent = 1
            work_units = max(1, edge_count * (average_degree**exponent))
        elif name == "community_detection":
            # Weighted synchronous label propagation scans the immutable
            # adjacency once per round. The worker deliberately caps at 500;
            # admission must account for that repeated work instead of calling
            # this an O(V+E) one-pass job.
            try:
                default_iterations = int(
                    os.getenv("LARGE_GRAPH_COMMUNITY_DEFAULT_MAX_ITERATIONS", "5")
                )
            except (TypeError, ValueError, OverflowError):
                default_iterations = 5
            iterations = max(
                1,
                min(int(params.get("max_iterations", default_iterations)), 500),
            )
            work_units = max(
                1,
                iterations * max(1, node_count + edge_count),
            )
        elif name == "global_metrics" and mode in {"transitivity", "average_clustering"}:
            # Triangle enumeration is proportional to wedges rather than just
            # edges.  With only catalogue statistics available, 2E/V is the
            # best conservative average-degree estimate for admission.
            average_degree = max(1, (2 * edge_count) // max(1, node_count))
            work_units = max(1, edge_count * average_degree)
        elif name == "graph_structure_analysis" and mode == "is_at_free":
            work_units = max(1, node_count * node_count * max(1, edge_count))
        elif name == "graph_structure_analysis" and mode == "all_topological_sorts":
            work_units = max(1, 200 * max(1, node_count + edge_count))
        elif name == "connectivity_analysis" and mode in {
            "node_connectivity", "minimum_node_cut",
        } and not params.get("source"):
            work_units = max(1, node_count * node_count * max(1, edge_count))
        elif name == "connectivity_analysis" and mode in {
            "edge_connectivity", "minimum_edge_cut",
        } and not params.get("source"):
            work_units = max(1, node_count * max(1, edge_count))
        elif name == "connectivity_analysis" and mode in {
            "node_connectivity", "edge_connectivity", "minimum_node_cut",
            "minimum_edge_cut", "local_bridges",
        }:
            work_units = max(1, node_count * max(1, edge_count))
        elif name == "tree_dag_analysis" and mode == "antichains":
            work_units = max(1, node_count * max(1, edge_count))
        elif name == "distance_measures" and mode in {
            "center", "periphery", "eccentricity", "wiener_index",
            "global_efficiency", "local_efficiency", "barycenter", "girth",
        }:
            work_units = max(1, node_count * max(1, edge_count))
        else:
            work_units = max(1, node_count + edge_count)
        disk_bytes = edge_count * edge_disk_bytes + node_count * node_disk_bytes
        if name == "tree_dag_analysis" and mode == "antichains":
            # The exact partial-order closure can be dense even for a sparse
            # DAG, so reserve quadratic disk rather than admitting it with a
            # generic linear estimate.
            disk_bytes = max(disk_bytes, node_count * node_count * 48)
        source_parallel_state = (
            name == "global_minimum_cut"
            or (
                name == "link_prediction_score"
                and not (
                    params.get("source") is not None
                    and params.get("target") is not None
                )
            )
            or (
                name == "node_centrality"
                and mode
                in {
                    "betweenness",
                    "closeness",
                    "harmonic",
                    "load",
                    "percolation",
                }
            )
            or (
                name == "path_analysis"
                and mode == "average_shortest_path_length"
            )
            or (
                name == "global_metrics" and mode in {"diameter", "radius"}
            )
            or (
                name == "clique_analysis"
                and mode == "number_of_cliques"
                and not params.get("node")
            )
            or (
                name == "reachability" and mode == "transitive_closure"
            )
        )
        if source_parallel_state:
            # These source/sink-decomposable kernels keep one graph-sized
            # SQLite state per active process. The service caps compute
            # workers at eight, so reserve for the same worst-case fan-out.
            disk_bytes *= active_workers
        # V/E-only estimates substantially undercount snapshots containing
        # long UTF-8 identifiers and SQLite indexes.  The immutable manifest
        # records actual shard sizes, so reserve temporary/output space from
        # the larger of the structural estimate and a storage amplification.
        # Derived subgraphs need room for the selection artifact, rebuild
        # intermediates and the newly published snapshot while the source is
        # still present. Source-decomposable kernels keep one graph-sized
        # working database per active worker.
        if snapshot_storage_bytes is None:
            snapshot_bytes = 0
        elif isinstance(snapshot_storage_bytes, dict):
            snapshot_bytes = snapshot_storage_bytes.get("total", -1)
        else:
            snapshot_bytes = snapshot_storage_bytes
        if (
            isinstance(snapshot_bytes, bool)
            or not isinstance(snapshot_bytes, int)
            or snapshot_bytes < 0
        ):
            raise ValueError("snapshot_storage_bytes 必须包含非负整数 total")
        storage_multiplier = 1.25
        if source_parallel_state:
            storage_multiplier = max(
                storage_multiplier, 1.25 * active_workers
            )
        if name in {"maximum_flow", "minimum_cut"}:
            storage_multiplier = max(storage_multiplier, 2.0)
        if name == "subgraph_extraction":
            storage_multiplier = max(
                storage_multiplier,
                3.0 if params.get("create_version", True) else 1.5,
            )
        storage_based_disk_bytes = int(math.ceil(snapshot_bytes * storage_multiplier))
        disk_bytes = max(disk_bytes, storage_based_disk_bytes)
        # Map/reduce kernels and the generic Result Processor create real
        # child processes.  Reserving only one partition's resident state can
        # admit an eight-worker job whose aggregate RSS is many times larger.
        # Include a controller allowance plus one conservative floor for each
        # active process; the per-partition term grows beyond that floor for
        # unusually large node shards.
        per_worker_memory = max(
            128 * 1024**2,
            largest_node_partition * 160,
        )
        memory_bytes = 128 * 1024**2 + active_workers * per_worker_memory
        if name == "shortest_path" and mode == "yen_k_shortest":
            yen_k = max(1, min(int(params.get("k", 3)), 200))
            # Account for the accepted-path lower bound. Candidate-heap
            # amplification is topology-dependent and receives a hard
            # runtime counter inside the worker.
            memory_bytes += max(1, node_count) * yen_k * 32
        return {
            "memory_bytes": max(256 * 1024**2, memory_bytes),
            "disk_bytes": max(
                64 * 1024**2,
                disk_bytes,
            ),
            "estimated_work_units": int(work_units),
            "active_workers": active_workers,
            "snapshot_storage_bytes": snapshot_bytes,
        }

    def complexity_guard(
        self,
        node_count: int,
        edge_count: int,
        algorithm: str,
        parameters: dict[str, Any] | None = None,
    ) -> tuple[bool, str, str]:
        """Reject unbounded global jobs before they enter the durable queue.

        Added for the single-machine boundary in the design document.  The
        explicit ``allow_expensive`` escape hatch is still auditable in the
        request parameters; normal API calls cannot accidentally launch an
        O(V²) or exponential enumeration on a billion-edge snapshot.
        """
        params = dict(parameters or {})
        if _true_flag(params.get("allow_expensive", False)):
            return True, "用户显式允许高复杂度算法", "EXPLICIT_EXPENSIVE_OVERRIDE"
        n = int(node_count)
        e = int(edge_count)
        name = str(algorithm).strip().lower()
        mode_key = ComputeRouter._MODE_PARAMETER.get(name)
        mode = str(
            params.get(mode_key, ComputeRouter._MODE_DEFAULT.get(name, ""))
            if mode_key
            else ""
        ).strip().lower()
        if name == "shortest_path" and mode == "yen_k_shortest":
            yen_k = max(1, min(int(params.get("k", 3)), 200))
            try:
                state_nodes_limit = int(
                    os.getenv("LARGE_GRAPH_YEN_MAX_PATH_STATE_NODES", "1000000")
                )
            except (TypeError, ValueError, OverflowError):
                state_nodes_limit = 1_000_000
            state_nodes_limit = max(1, state_nodes_limit)
            projected_state_nodes = max(1, n) * yen_k
            if projected_state_nodes > state_nodes_limit:
                return (
                    False,
                    f"Yen 候选路径常驻状态下界为 {projected_state_nodes} 个节点引用，"
                    f"超过安全上限 {state_nodes_limit}；请减小 k、缩小图范围或显式 "
                    "allow_expensive=true",
                    "YEN_PATH_STATE_RISK",
                )
        if name == "link_prediction_score" and not (
            params.get("source") is not None and params.get("target") is not None
        ):
            limit = int(os.getenv("LARGE_GRAPH_LINK_PREDICTION_MAX_NODES", "10000"))
            if n > limit:
                return (
                    False,
                    f"全图链路预测需要枚举 O(V²) 个节点对，节点数 {n} 超过安全上限 {limit}；"
                    "请指定 source/target、缩小图范围或显式 allow_expensive=true",
                    "QUADRATIC_ALGORITHM_RISK",
                )
        # ``find_cliques`` stops after its requested result cap, but the two
        # exact aggregate modes must exhaust *all* maximal cliques before they
        # can return.  Giving those modes the generic 50k-node enumeration
        # threshold would make an accidental request effectively unbounded on
        # perfectly modest dense graphs.  Keep a deliberately conservative,
        # independently configurable gate; operators can still acknowledge
        # the topology-dependent risk with allow_expensive=true.
        if (
            name == "clique_analysis"
            and mode in {"clique_number", "number_of_cliques"}
        ):
            exact_clique_limit = _positive_int_env(
                "LARGE_GRAPH_EXACT_CLIQUE_MAX_NODES", 40
            )
            if n > exact_clique_limit:
                return (
                    False,
                    f"clique_analysis/{mode} 必须穷举全部极大团，节点数 {n} "
                    f"超过精确计算安全上限 {exact_clique_limit}；请先抽取更小子图或显式 "
                    "allow_expensive=true",
                    "EXACT_CLIQUE_ENUMERATION_RISK",
                )
        if name in {"clique_analysis", "find_cycles", "maximum_cycle"} or (
            name == "path_analysis" and mode == "all_simple_paths"
        ):
            limit = _positive_int_env("LARGE_GRAPH_ENUMERATION_MAX_NODES", 50000)
            if n > limit:
                return (
                    False,
                    f"{name} 可能进行指数级枚举，节点数 {n} 超过安全上限 {limit}；"
                    "请缩小图范围或显式 allow_expensive=true",
                    "EXPONENTIAL_ALGORITHM_RISK",
                )
        if name == "global_minimum_cut":
            limit = int(os.getenv("LARGE_GRAPH_GLOBAL_CUT_MAX_NODES", "100000"))
            if n > limit:
                return (
                    False,
                    f"全局最小割当前需要重复执行源点到各节点的最小割，节点数 {n} 超过安全上限 {limit}；"
                    "请缩小图范围或显式 allow_expensive=true",
                    "REPEATED_FLOW_RISK",
                )
        repeated_traversal = (
            (name == "node_centrality" and mode in {"closeness", "harmonic"})
            or (name == "path_analysis" and mode == "average_shortest_path_length")
            or (name == "global_metrics" and mode in {"diameter", "radius"})
            or (name == "reachability" and mode == "transitive_closure")
        )
        if repeated_traversal:
            work = n * max(1, e)
            limit = int(
                os.getenv("LARGE_GRAPH_REPEATED_TRAVERSAL_MAX_WORK", "250000000")
            )
            if work > limit:
                return (
                    False,
                    f"{name}/{mode} 需要从全部节点重复遍历，估算工作量 {work} "
                    f"超过安全上限 {limit}；请使用采样算法、缩小图范围或显式 "
                    "allow_expensive=true",
                    "REPEATED_TRAVERSAL_RISK",
                )
        if name == "global_metrics" and mode in {"transitivity", "average_clustering"}:
            average_degree = max(1, (2 * e) // max(1, n))
            work = e * average_degree
            limit = int(os.getenv("LARGE_GRAPH_TRIANGLE_MAX_WORK", "500000000"))
            if work > limit:
                return (
                    False,
                    f"{name}/{mode} 需要枚举邻接楔，估算工作量 {work} 超过安全上限 "
                    f"{limit}；请缩小图范围或显式 allow_expensive=true",
                    "TRIANGLE_ENUMERATION_RISK",
                )
        if name == "node_centrality" and mode == "voterank":
            work = n * max(1, e)
            limit = int(os.getenv("LARGE_GRAPH_VOTERANK_MAX_WORK", "250000000"))
            if work > limit:
                return (
                    False,
                    f"node_centrality/voterank 最坏需要逐节点扫描全图，估算工作量 "
                    f"{work} 超过安全上限 {limit}；请缩小图范围或显式 "
                    "allow_expensive=true",
                    "VOTERANK_WORK_RISK",
                )
        return True, "算法复杂度处于默认准入范围", "COMPLEXITY_ACCEPTED"

    def admit(self, estimate: dict[str, int]) -> tuple[bool, str, dict[str, int]]:
        self.work_root.mkdir(parents=True, exist_ok=True)
        memory_available = self.available_memory_bytes()
        disk_available = shutil.disk_usage(self.work_root).free
        memory_budget = max(0, int(memory_available * (1.0 - self.memory_reserve_ratio)))
        disk_budget = max(0, disk_available - self.disk_reserve_bytes)
        observed = {
            **{key: int(value) for key, value in estimate.items()},
            "memory_available_bytes": memory_available,
            "memory_budget_bytes": memory_budget,
            "disk_available_bytes": disk_available,
            "disk_budget_bytes": disk_budget,
        }
        if memory_available and estimate["memory_bytes"] > memory_budget:
            return False, "估算内存超过为 API、图数据库和操作系统预留后的预算", observed
        if estimate["disk_bytes"] > disk_budget:
            return False, "估算临时磁盘空间超过预留后的预算", observed
        max_work = self.maximum_estimated_work_units()
        if max_work > 0 and int(estimate.get("estimated_work_units", 0)) > max_work:
            return False, "估算计算工作量超过本机大图任务上限", observed
        return True, "资源预算允许执行", observed


class ComputeRouter:
    """Select the small/bounded path or the dedicated partitioned path."""

    ENGINE_NAME = "igraph-partitioned-local"
    # Bumped after adding the five grouped analysis kernels (55 modes).
    ENGINE_VERSION = "2.6"
    _MODE_PARAMETER = {
        **SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS,
        "independent_set_cover": "operation",
        "motif_counting": "motif",
    }
    _MODE_DEFAULT = SMALL_GRAPH_ALGORITHM_MODE_DEFAULTS

    def __init__(
        self,
        resource_manager: ResourceManager,
        *,
        large_node_threshold: int | None = None,
        large_edge_threshold: int | None = None,
    ) -> None:
        self.resources = resource_manager
        self.large_node_threshold = int(
            large_node_threshold
            if large_node_threshold is not None
            else os.getenv("LARGE_GRAPH_NODE_THRESHOLD", "1000000")
        )
        self.large_edge_threshold = int(
            large_edge_threshold
            if large_edge_threshold is not None
            else os.getenv("LARGE_GRAPH_EDGE_THRESHOLD", "5000000")
        )
        self.capabilities: dict[str, LargeGraphCapability] = {
            "degree": LargeGraphCapability(
                algorithm="degree", engine=self.ENGINE_NAME, engine_version=self.ENGINE_VERSION,
                parallel_strategy="edge-map/node-reduce",
            ),
            "degree_centrality": LargeGraphCapability(
                algorithm="degree_centrality", engine=self.ENGINE_NAME, engine_version=self.ENGINE_VERSION,
                parallel_strategy="edge-map/node-reduce",
            ),
            # Direction-specific degree has no unambiguous undirected meaning,
            # so its capability contract rejects undirected snapshots.
            "in_degree": LargeGraphCapability(
                algorithm="in_degree", engine=self.ENGINE_NAME,
                engine_version=self.ENGINE_VERSION, supports_undirected=False,
                parallel_strategy="edge-map/node-reduce",
            ),
            "out_degree": LargeGraphCapability(
                algorithm="out_degree", engine=self.ENGINE_NAME,
                engine_version=self.ENGINE_VERSION, supports_undirected=False,
                parallel_strategy="edge-map/node-reduce",
            ),
        }
        graphscope_only = {
            "strongly_connected_components": LargeGraphCapability(
                algorithm="strongly_connected_components",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_undirected=False,
                supports_weighted=True,
                result_kind="node_partition",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "biconnected_components": LargeGraphCapability(
                algorithm="biconnected_components",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="node_partition",
                execution_mode="approximate",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "bridges": LargeGraphCapability(
                algorithm="bridges",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="scalar",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "articulation_points": LargeGraphCapability(
                algorithm="articulation_points",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="scalar",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "minimum_spanning_forest": LargeGraphCapability(
                algorithm="minimum_spanning_forest",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="scalar",
                complexity_class="O(E log V)",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "graph_coloring": LargeGraphCapability(
                algorithm="graph_coloring",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="node_label",
                execution_mode="approximate",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "maximal_matching": LargeGraphCapability(
                algorithm="maximal_matching",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="scalar",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "hits": LargeGraphCapability(
                algorithm="hits",
                engine="graphscope",
                engine_version="0.29",
                supports_weighted=False,
                result_kind="node_multi_score",
                complexity_class="O(iterations·E)",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "k_shell": LargeGraphCapability(
                algorithm="k_shell",
                engine="graphscope",
                engine_version="0.29",
                supports_directed=False,
                supports_weighted=True,
                result_kind="node_set",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "personalized_pagerank": LargeGraphCapability(
                algorithm="personalized_pagerank",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_weighted=False,
                result_kind="node_score",
                execution_mode="approximate",
                complexity_class="O(iterations·E)",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "approximate_diameter": LargeGraphCapability(
                algorithm="approximate_diameter",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_weighted=True,
                result_kind="scalar",
                execution_mode="approximate",
                complexity_class="O(V+E) heuristic",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "fluid_community": LargeGraphCapability(
                algorithm="fluid_community",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_weighted=True,
                result_kind="node_partition",
                execution_mode="approximate",
                complexity_class="O(iterations·(V+E)) heuristic",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "densest_subgraph": LargeGraphCapability(
                algorithm="densest_subgraph",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_weighted=True,
                result_kind="scalar",
                execution_mode="approximate",
                complexity_class="O(rounds·(V+E)) 2-approximation",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "independent_set_cover": LargeGraphCapability(
                algorithm="independent_set_cover",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_weighted=True,
                result_kind="scalar",
                modes=(
                    "maximal_independent_set",
                    "minimal_dominating_set",
                    "minimal_vertex_cover",
                    "minimal_edge_cover",
                ),
                complexity_class="GraphScope FLASH native",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
            "motif_counting": LargeGraphCapability(
                algorithm="motif_counting",
                engine="graphscope-flash",
                engine_version="0.29",
                supports_undirected=False,
                supports_weighted=True,
                result_kind="scalar",
                modes=(
                    "triangle",
                    "rectangle",
                    "k_clique",
                    "diamond",
                    "tailed_triangle",
                    "three_path",
                    "cycle_plus_triangle",
                    "cyclic_triangle",
                    "acyclic_triangle",
                    "in_plus_triangle",
                    "out_plus_triangle",
                ),
                complexity_class="motif-dependent",
                parallel_strategy="graphscope-native-fragment-parallel",
            ),
        }
        if set(graphscope_only) != set(GRAPHSCOPE_ONLY_ALGORITHM_NAMES):
            raise RuntimeError("GraphScope 大图专用能力目录不完整")
        self.capabilities.update(graphscope_only)
        # Every public small-graph selector is advertised and validated from
        # the same compatibility contract.  This includes maximum_cycle's
        # length/weight branch, which used to exist only inside the worker.
        modes = SMALL_GRAPH_ALGORITHM_MODES
        approximations = {
            # The scalable kernel is weighted synchronous label propagation;
            # even the two label-propagation names are not byte-for-byte
            # NetworkX equivalents (one small-path variant is unweighted and
            # the other is asynchronous), so expose that fact explicitly.
            "community_detection": (
                "louvain", "greedy_modularity", "label_propagation",
                "label_propagation_asyn", "leiden", "girvan_newman", "asyn_fluidc",
            ),
            "node_centrality": ("betweenness", "load", "percolation"),
            "tree_dag_analysis": ("random_spanning_tree",),
            "matching_coloring_analysis": (
                "max_weight_matching", "min_weight_matching", "min_edge_cover",
                "equitable_color",
            ),
            "distance_measures": ("estrada_index",),
        }
        execution_modes = {
            "community_detection": "approximate",
            "node_centrality": "mixed",
            "find_cycles": "bounded",
            "maximum_cycle": "bounded",
            "clique_analysis": "mixed",
            "all_shortest_paths": "bounded",
            "reachability": "mixed",
            "shortest_path": "mixed",
            "path_analysis": "mixed",
            "graph_structure_analysis": "mixed",
            "connectivity_analysis": "exact",
            "tree_dag_analysis": "mixed",
            "matching_coloring_analysis": "mixed",
            "distance_measures": "mixed",
        }
        complexity_classes = {
            "link_prediction_score": "O(V²)",
            "clique_analysis": "exponential/bounded",
            "find_cycles": "exponential/bounded",
            "maximum_cycle": "exponential/bounded",
            "global_minimum_cut": "O(V·flow)",
            "node_centrality": "O(V·(V+E)) or sampled",
            "global_metrics": "O(V·(V+E)) for distance metrics",
            "path_analysis": "exponential/bounded or O(V·(V+E))",
            "reachability": "O(V+E) or bounded closure materialization",
            "graph_structure_analysis": "O(V+E) to exponential/bounded",
            "connectivity_analysis": "O(V·flow) to O(V²·flow)",
            "tree_dag_analysis": "O(V+E) to exponential/bounded",
            "matching_coloring_analysis": "O(E log E) scalable; bounded native exact",
            "distance_measures": "O(V·(V+E)) for distance metrics",
        }
        parallel_strategies = {
            "shortest_path": "adjacency-shard-map/disk-priority-reduce",
            "all_shortest_paths": "adjacency-shard-map/predecessor-dag-reduce",
            "maximum_flow": "adjacency-shard-map/residual-augment-reduce",
            "minimum_cut": "adjacency-shard-map/residual-cut-reduce",
            "minimum_spanning_tree": "adjacency-shard-map/union-find-reduce",
            "graph_connectivity": "adjacency-shard-map/component-state-reduce",
            "topological_sort": "adjacency-shard-map/kahn-state-reduce",
            "degree_analysis": "node-shard-map/degree-disk-reduce",
            "community_detection": "node-partition-map/sqlite-label-reduce",
            "link_prediction_score": "source-batch-map/top-k-reduce",
            "global_minimum_cut": "sink-batch-map/min-cut-reduce",
            "find_cycles": "cycle-start-map/bounded-artifact-merge",
            "maximum_cycle": "cycle-start-map/max-reduce",
            "clique_analysis": (
                "clique-root-map/bounded-artifact-merge for find_cliques; "
                "clique-root-map/max-or-disk-count-reduce for aggregate modes"
            ),
            "reachability": (
                "source-partition-map/bounded-artifact-merge for transitive_closure; "
                "adjacency-shard-map/reachability-state-reduce for point modes"
            ),
            "node_centrality": (
                "source-batch-map/disk-reduce for Brandes and distance modes; "
                "adjacency-shard-map/iteration-state-reduce for iterative/degree/VoteRank modes"
            ),
            "path_analysis": (
                "source-batch-map/scalar-reduce for average distance; "
                "adjacency-shard-map/path-state-reduce for pair/path modes"
            ),
            "global_metrics": (
                "source-batch-map/scalar-reduce for diameter/radius; "
                "node-partition-map/scalar-reduce for triangle metrics; "
                "adjacency-shard-map/scalar-reduce for density/assortativity"
            ),
            "subgraph_extraction": "adjacency-shard-map/subgraph-state-reduce",
            "graph_structure_analysis": "adjacency-shard-map/sqlite-structure-reduce",
            "connectivity_analysis": "adjacency-shard-map/disk-cut-state-reduce",
            "tree_dag_analysis": "adjacency-shard-map/sqlite-dag-state-reduce",
            "matching_coloring_analysis": "edge-map/sqlite-matching-color-reduce",
            "distance_measures": "source-batch-map/disk-metric-reduce",
        }
        result_kinds = {
            "shortest_path": "path", "all_shortest_paths": "paths",
            "maximum_flow": "edge_flow", "minimum_cut": "cut",
            "global_minimum_cut": "cut", "minimum_spanning_tree": "edge_set",
            "community_detection": "node_partition", "node_centrality": "node_score",
            "graph_connectivity": "node_partition", "find_cycles": "cycles",
            "maximum_cycle": "cycle", "topological_sort": "node_order",
            "degree_analysis": "node_score", "subgraph_extraction": "subgraph",
            "link_prediction_score": "edge_score", "path_analysis": "paths",
            "clique_analysis": "cliques", "reachability": "node_set",
            "global_metrics": "scalar",
            "graph_structure_analysis": "analysis",
            "connectivity_analysis": "analysis",
            "tree_dag_analysis": "analysis",
            "matching_coloring_analysis": "analysis",
            "distance_measures": "analysis",
        }
        for algorithm in sorted(LargeGraphAlgorithms.SUPPORTED):
            self.capabilities[algorithm] = LargeGraphCapability(
                algorithm=algorithm,
                engine=self.ENGINE_NAME,
                engine_version=self.ENGINE_VERSION,
                supports_undirected=algorithm != "topological_sort",
                supports_weighted=True,
                result_kind=result_kinds[algorithm],
                modes=modes.get(algorithm, ()),
                approximate_modes=approximations.get(algorithm, ()),
                execution_mode=execution_modes.get(algorithm, "exact"),
                complexity_class=complexity_classes.get(algorithm, "O(V+E)"),
                parallel_strategy=parallel_strategies.get(
                    algorithm, "adjacency-shard-map/disk-state-reduce"
                ),
            )

    def is_large(self, node_count: int, edge_count: int) -> bool:
        return (
            int(node_count) >= self.large_node_threshold
            or int(edge_count) >= self.large_edge_threshold
        )

    def _validate_parameters(
        self,
        algorithm: str,
        capability: LargeGraphCapability,
        parameters: dict[str, Any],
    ) -> tuple[bool, str, str]:
        """Validate method-level capability before a durable job is queued."""
        try:
            encoded_size = len(
                json.dumps(
                    parameters,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, OverflowError):
            return False, "算法参数必须是可序列化 JSON 对象", "INVALID_ALGORITHM_PARAMETERS"
        max_parameter_bytes = int(
            os.getenv("LARGE_GRAPH_MAX_PARAMETER_BYTES", str(64 * 1024))
        )
        if encoded_size > max_parameter_bytes:
            return (
                False,
                f"算法参数大小 {encoded_size} 字节超过控制面上限 {max_parameter_bytes}",
                "ALGORITHM_PARAMETERS_TOO_LARGE",
            )
        boolean_parameters = {
            "allow_expensive", "strict_semantics", "use_weight",
            "treat_as_undirected", "normalized", "create_version",
            "maxcardinality",
        }
        if "result_detail" in parameters and str(
            parameters["result_detail"]
        ).strip().lower() not in {"distances", "paths"}:
            return (
                False,
                "参数 result_detail 必须是 distances 或 paths",
                "INVALID_ALGORITHM_PARAMETERS",
            )
        if "result_detail" in parameters and not (
            algorithm == "shortest_path"
            and str(parameters.get("target", "")).strip().lower() == "all"
        ):
            return (
                False,
                "result_detail 仅适用于 shortest_path 且 target=all",
                "INVALID_ALGORITHM_PARAMETERS",
            )
        for name in boolean_parameters.intersection(parameters):
            if not isinstance(parameters[name], bool):
                return False, f"参数 {name} 必须为布尔值", "INVALID_ALGORITHM_PARAMETERS"
        for name, value in parameters.items():
            if isinstance(value, str) and len(value.encode("utf-8")) > 4096:
                return False, f"参数 {name} 的字符串过长", "ALGORITHM_PARAMETERS_TOO_LARGE"
        mode_key = self._MODE_PARAMETER.get(algorithm)
        mode = ""
        if capability.modes and mode_key:
            raw_mode = parameters.get(mode_key, self._MODE_DEFAULT.get(algorithm))
            mode = str(raw_mode or "").strip().lower()
            if not mode:
                return False, f"{algorithm} 缺少必填参数 {mode_key}", "ALGORITHM_MODE_REQUIRED"
            if mode not in capability.modes:
                return (
                    False,
                    f"并行引擎不支持 {algorithm}/{mode}；可用模式: {', '.join(capability.modes)}",
                    "LARGE_GRAPH_MODE_UNSUPPORTED",
                )
        # Tool schemas protect normal MCP calls, but the HTTP dispatcher and
        # recovered durable payloads must not be able to turn malformed
        # numeric input into a worker-side ValueError after queueing.
        integer_bounds = {
            "top_k": (1, 200),
            "limit": (1, 200),
            "k": (1, None),
            "cutoff": (1, 10_000),
            # The community kernel has a deliberately smaller hard loop cap
            # than iterative centralities. Reject rather than silently clamp a
            # caller's requested semantics in the worker.
            "max_iterations": (
                1,
                500 if algorithm == "community_detection" else 2_000,
            ),
            "max_cycles": (1, 100_000),
            "depth_limit": (0, None),
            "iterations": (1, 1_000),
            "num_colors": (1, None),
            "rounds": (1, 1_000),
        }
        for name, (minimum, maximum) in integer_bounds.items():
            if name not in parameters or parameters[name] is None:
                continue
            value = parameters[name]
            if (
                name in {"limit", "top_k"}
                and isinstance(value, str)
                and value.strip().lower() == "all"
            ):
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return (
                    False,
                    f"参数 {name} 必须为整数",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
            if isinstance(value, bool) or parsed < minimum or (
                maximum is not None and parsed > maximum
            ):
                upper = f" 且不大于 {maximum}" if maximum is not None else ""
                return (
                    False,
                    f"参数 {name} 必须不小于 {minimum}{upper}",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        brandes_mode = (
            algorithm == "node_centrality"
            and mode in {"betweenness", "load", "percolation"}
        )
        if "sample_sources" in parameters or brandes_mode:
            # This is a deployment hard limit, not an expensive-algorithm
            # warning: allow_expensive must never permit an oversized source
            # container.  Validate the configured default too, otherwise a
            # missing request field could enqueue a job that the worker must
            # reject later.
            raw_sources = parameters.get(
                "sample_sources",
                os.getenv("LARGE_GRAPH_BRANDES_SOURCES", "64"),
            )
            try:
                sample_sources = int(raw_sources)
            except (TypeError, ValueError, OverflowError):
                sample_sources = 0
            sample_limit = brandes_sample_sources_limit()
            if (
                isinstance(raw_sources, bool)
                or sample_sources < 1
                or sample_sources > sample_limit
            ):
                return (
                    False,
                    "参数 sample_sources 必须在 1 到 "
                    f"{sample_limit} 之间（LARGE_GRAPH_MAX_SAMPLE_SOURCES）",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        if algorithm == "shortest_path" and mode == "yen_k_shortest":
            try:
                yen_k = int(parameters.get("k", 3))
            except (TypeError, ValueError, OverflowError):
                yen_k = 0
            if isinstance(parameters.get("k"), bool) or not 1 <= yen_k <= 200:
                return (
                    False,
                    "shortest_path/yen_k_shortest 的参数 k 必须在 1 到 200 之间",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        for name in ("tolerance", "alpha", "beta"):
            if name not in parameters or parameters[name] is None:
                continue
            if isinstance(parameters[name], bool):
                return False, f"参数 {name} 必须为有限数值", "INVALID_ALGORITHM_PARAMETERS"
            try:
                value = float(parameters[name])
            except (TypeError, ValueError, OverflowError):
                return False, f"参数 {name} 必须为有限数值", "INVALID_ALGORITHM_PARAMETERS"
            if not math.isfinite(value):
                return False, f"参数 {name} 必须为有限数值", "INVALID_ALGORITHM_PARAMETERS"
            if name == "tolerance" and value <= 0:
                return False, f"参数 {name} 必须为正数", "INVALID_ALGORITHM_PARAMETERS"
        if "damping" in parameters:
            if isinstance(parameters["damping"], bool):
                return False, "参数 damping 必须为 0 到 1 之间的数", "INVALID_ALGORITHM_PARAMETERS"
            try:
                damping = float(parameters["damping"])
            except (TypeError, ValueError, OverflowError):
                return False, "参数 damping 必须为 0 到 1 之间的数", "INVALID_ALGORITHM_PARAMETERS"
            if not math.isfinite(damping) or not 0 < damping < 1:
                return False, "参数 damping 必须大于 0 且小于 1", "INVALID_ALGORITHM_PARAMETERS"

        required: tuple[str, ...] = ()
        if algorithm in {"shortest_path", "all_shortest_paths"}:
            required = ("source", "target")
        elif algorithm in {"maximum_flow", "minimum_cut"}:
            required = ("source", "sink")
        elif algorithm == "subgraph_extraction":
            if mode == "ego_graph":
                required = ("node",)
            elif mode == "node_subset":
                nodes = parameters.get("nodes")
                if not isinstance(nodes, list):
                    return False, "node_subset 需要 nodes 数组", "INVALID_ALGORITHM_PARAMETERS"
                max_nodes = int(os.getenv("LARGE_GRAPH_MAX_PARAMETER_NODES", "10000"))
                if len(nodes) > max_nodes:
                    return (
                        False,
                        f"node_subset 的 nodes 数量超过控制面上限 {max_nodes}",
                        "ALGORITHM_PARAMETERS_TOO_LARGE",
                    )
        elif algorithm == "community_detection" and "focus_nodes" in parameters:
            focus_nodes = parameters.get("focus_nodes")
            if not isinstance(focus_nodes, list):
                return (
                    False,
                    "community_detection 的 focus_nodes 必须为节点数组",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
            if len(focus_nodes) > 200:
                return (
                    False,
                    "community_detection 的 focus_nodes 最多允许 200 个节点",
                    "ALGORITHM_PARAMETERS_TOO_LARGE",
                )
            if any(
                not isinstance(node, str)
                or not node
                or len(node.encode("utf-8")) > 4096
                for node in focus_nodes
            ):
                return (
                    False,
                    "community_detection 的 focus_nodes 必须是非空且长度有效的节点字符串",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        elif algorithm == "link_prediction_score":
            has_source = parameters.get("source") is not None
            has_target = parameters.get("target") is not None
            if has_source != has_target:
                return (
                    False,
                    "链路预测的 source 与 target 必须同时提供或同时省略",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        elif algorithm == "path_analysis" and mode in {"all_simple_paths", "has_path"}:
            required = ("source", "target")
        elif algorithm == "reachability" and mode in {"descendants", "ancestors"}:
            required = ("node",)
        elif algorithm == "graph_structure_analysis" and mode in {"bfs_tree", "dfs_tree"}:
            required = ("source",)
        elif algorithm == "connectivity_analysis" and mode in {
            "node_connectivity", "edge_connectivity", "minimum_node_cut", "minimum_edge_cut",
        }:
            has_source = parameters.get("source") not in (None, "")
            has_target = parameters.get("target") not in (None, "")
            if has_source != has_target:
                return (
                    False,
                    "连通度/割集分析的 source 与 target 必须同时提供或同时省略",
                    "INVALID_ALGORITHM_PARAMETERS",
                )
        elif algorithm == "tree_dag_analysis":
            if mode == "lowest_common_ancestor":
                required = ("node1", "node2")
            elif mode in {"immediate_dominators", "dominance_frontiers"}:
                required = ("source",)
        elif algorithm == "matching_coloring_analysis":
            if mode == "equitable_color":
                required = ("num_colors",)
            elif mode == "is_matching" and not isinstance(parameters.get("edges", []), list):
                return False, "is_matching 需要 edges 数组", "INVALID_ALGORITHM_PARAMETERS"
        elif algorithm == "personalized_pagerank":
            required = ("source",)
        elif algorithm == "k_shell" and "k" not in parameters:
            required = ("k",)
        elif algorithm == "motif_counting" and mode == "k_clique":
            required = ("k",)

        if algorithm == "motif_counting" and mode == "k_clique" and "k" in parameters:
            try:
                clique_size = int(parameters["k"])
            except (TypeError, ValueError, OverflowError):
                clique_size = 0
            if isinstance(parameters["k"], bool) or not 2 <= clique_size <= 8:
                return (
                    False,
                    "motif_counting/k_clique 的参数 k 必须在 2 到 8 之间",
                    "INVALID_ALGORITHM_PARAMETERS",
                )

        missing = [name for name in required if parameters.get(name) in (None, "")]
        if missing:
            return (
                False,
                f"{algorithm}/{mode or 'default'} 缺少必填参数: {', '.join(missing)}",
                "INVALID_ALGORITHM_PARAMETERS",
            )
        return True, "算法参数有效", "ALGORITHM_PARAMETERS_VALID"

    def route(
        self,
        *,
        operation_scope: str,
        node_count: int,
        edge_count: int,
        algorithm: str = "",
        directed: bool = True,
        weighted: bool = False,
        snapshot_available: bool = False,
        partitions: int = 1,
        workers: int | None = None,
        force_large: bool = False,
        parameters: dict[str, Any] | None = None,
        snapshot_storage_bytes: int | dict[str, Any] | None = None,
    ) -> RouteDecision:
        scope = str(operation_scope or "").strip().lower()
        is_large = force_large or self.is_large(node_count, edge_count)
        if scope in {"interactive", "bounded"}:
            return RouteDecision(
                path="small_graph",
                accepted=True,
                reason="点边查询或有界计算继续使用现有查询路径",
                code="BOUNDED_QUERY",
            )
        if scope != "global":
            return RouteDecision("reject", False, "未知计算范围", "INVALID_SCOPE")
        if not is_large:
            return RouteDecision(
                "small_graph", True, "图规模低于大图阈值", "SMALL_GRAPH"
            )

        algorithm = str(algorithm).strip().lower()
        params = dict(parameters or {})
        capability = self.capabilities.get(algorithm)
        if capability is None:
            return RouteDecision(
                "reject",
                False,
                "并行引擎没有该算法的明确语义映射，禁止回退到 NetworkX",
                "LARGE_GRAPH_ALGORITHM_UNSUPPORTED",
            )
        parameters_ok, parameters_reason, parameters_code = self._validate_parameters(
            algorithm, capability, params
        )
        if not parameters_ok:
            return RouteDecision(
                "reject",
                False,
                parameters_reason,
                parameters_code,
                capability=capability,
            )
        if directed and not capability.supports_directed:
            return RouteDecision("reject", False, "算法不支持有向图", "DIRECTED_UNSUPPORTED")
        if not directed and not capability.supports_undirected:
            return RouteDecision("reject", False, "算法不支持无向图", "UNDIRECTED_UNSUPPORTED")
        if weighted and not capability.supports_weighted:
            return RouteDecision("reject", False, "算法不支持加权语义", "WEIGHTED_UNSUPPORTED")
        # Added: callers that require byte-for-byte small-path semantics can
        # opt out of scalable substitutions.  The default remains compatible
        # with the public catalogue, but the decision is now explicit.
        if _true_flag(params.get("strict_semantics", False)):
            mode_key = self._MODE_PARAMETER.get(algorithm)
            requested_mode = str(
                params.get(mode_key, self._MODE_DEFAULT.get(algorithm, ""))
                if mode_key
                else ""
            ).strip().lower()
            if (
                capability.execution_mode == "approximate"
                or requested_mode in capability.approximate_modes
            ):
                return RouteDecision(
                    "reject",
                    False,
                    f"大图路径的 {algorithm}/{requested_mode} 目前是近似实现，strict_semantics=true 时拒绝",
                    "APPROXIMATION_NOT_ALLOWED",
                    capability=capability,
                )
            # Some capabilities are explicitly bounded as a whole, while a
            # few mixed capabilities only truncate in particular modes.  A
            # strict request must never silently receive the first K paths,
            # cycles, cliques, or closure edges in place of the full result.
            bounded_mode = capability.execution_mode == "bounded" or (
                algorithm == "shortest_path"
                and (
                    requested_mode == "yen_k_shortest"
                    or (
                        str(params.get("target", "")).strip().lower() == "all"
                        and str(params.get("result_detail", "paths")).strip().lower()
                        != "distances"
                    )
                )
            ) or (
                algorithm == "path_analysis"
                and requested_mode == "all_simple_paths"
            ) or (
                algorithm == "clique_analysis"
                and requested_mode == "find_cliques"
            ) or (
                algorithm == "reachability"
                and requested_mode == "transitive_closure"
            )
            if bounded_mode:
                mode_label = requested_mode or "default"
                return RouteDecision(
                    "reject",
                    False,
                    f"大图路径的 {algorithm}/{mode_label} 会进行有界枚举或截断结果，"
                    "strict_semantics=true 时拒绝",
                    "BOUNDED_SEMANTICS_NOT_ALLOWED",
                    capability=capability,
                )
        if (
            str(algorithm).strip().lower() == "global_minimum_cut"
            and directed
            and parameters is not None
            and str(parameters.get("treat_as_undirected", "true")).strip().lower()
            in {"0", "false", "no", "off"}
        ):
            return RouteDecision(
                "reject",
                False,
                "当前大图全局最小割仅支持将有向图按无向图处理",
                "DIRECTED_SEMANTICS_UNSUPPORTED",
                capability=capability,
            )
        if not snapshot_available:
            return RouteDecision(
                "reject",
                False,
                "当前图版本没有已发布的分区快照",
                "LARGE_GRAPH_SNAPSHOT_REQUIRED",
                capability=capability,
            )

        complexity_ok, complexity_reason, complexity_code = self.resources.complexity_guard(
            node_count,
            edge_count,
            algorithm,
            params,
        )
        if not complexity_ok:
            return RouteDecision(
                "reject",
                False,
                complexity_reason,
                complexity_code,
                capability=capability,
                resource_estimate=self.resources.estimate_algorithm(
                    node_count,
                    edge_count,
                    max(1, partitions),
                    algorithm,
                    params,
                    workers,
                    snapshot_storage_bytes,
                ),
            )

        estimate = self.resources.estimate_algorithm(
            node_count,
            edge_count,
            max(1, partitions),
            algorithm,
            params,
            workers,
            snapshot_storage_bytes,
        )
        accepted, reason, observed = self.resources.admit(estimate)
        decision_code = "LARGE_GRAPH_ADMITTED" if accepted else "INSUFFICIENT_RESOURCES"
        mode_key = self._MODE_PARAMETER.get(algorithm)
        mode = str(
            params.get(mode_key, self._MODE_DEFAULT.get(algorithm, ""))
            if mode_key
            else ""
        ).strip().lower()
        if (
            not accepted
            and reason == "估算计算工作量超过本机大图任务上限"
            and algorithm == "node_centrality"
            and mode in {"betweenness", "load", "percolation"}
        ):
            source_limit = self.resources.brandes_workload_source_limit(
                node_count, edge_count
            )
            requested_sources = int(
                params.get(
                    "sample_sources",
                    os.getenv("LARGE_GRAPH_BRANDES_SOURCES", "64"),
                )
            )
            if source_limit is not None and source_limit > 0:
                reason = (
                    f"{mode} 在当前图和部署预算下最多允许 sample_sources={source_limit}，"
                    f"当前请求为 {requested_sources}；请降低采样数，或省略该参数使用"
                    "工作量安全默认值"
                )
            else:
                reason = (
                    f"{mode} 对当前图的单个采样源工作量已超过本机大图任务上限；"
                    "请缩小图范围或提高 LARGE_GRAPH_MAX_ESTIMATED_WORK_UNITS"
                )
            decision_code = "BRANDES_SAMPLE_WORK_LIMIT"
        return RouteDecision(
            "large_graph" if accepted else "reject",
            accepted,
            reason,
            decision_code,
            capability=capability,
            resource_estimate=observed,
        )

    def capability_catalog(self) -> list[dict[str, Any]]:
        return [capability.to_dict() for capability in self.capabilities.values()]
