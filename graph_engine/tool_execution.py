"""Shared resource contracts for graph-tool DAGs and service admission.

This module deliberately uses only the standard library. A scheduler must
qualify resources with their user/session identity and keep the contract held
until background work actually finishes, not merely until a job is queued.
Read/write conflicts establish ordering; they do not imply that a failed
predecessor produced a usable result. Explicit result dependencies handle that.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
from graph_engine.finance_catalog import FINANCE_TOOL_NAMES


@dataclass(frozen=True)
class ToolExecutionPolicy:
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    barrier: bool = False
    background: bool = False

    @property
    def read_only(self) -> bool:
        return not self.writes and not self.barrier

    def as_dict(self) -> dict[str, Any]:
        return {
            "reads": sorted(self.reads),
            "writes": sorted(self.writes),
            "barrier": self.barrier,
            "background": self.background,
        }


def _policy(*, reads=(), writes=(), barrier=False, background=False):
    return ToolExecutionPolicy(frozenset(reads), frozenset(writes), barrier, background)


# Graph reads include derived in-memory caches: rebuilding a version-keyed
# NetworkX/search cache does not mutate the graph. Reporting returns a payload
# and visualization returns a view; neither publishes a new graph version.
GRAPH_READ_TOOLS = FINANCE_TOOL_NAMES | frozenset({
    "get_graph_info", "list_graph_versions", "get_node_info", "get_edge_info",
    "check_edge_exists", "get_neighbors", "search_node", "search_edge",
    "shortest_path", "all_shortest_paths", "maximum_flow", "minimum_cut",
    "global_minimum_cut", "minimum_spanning_tree", "community_detection",
    "node_centrality", "graph_connectivity", "find_cycles", "maximum_cycle",
    "topological_sort", "degree_analysis", "link_prediction_score",
    "path_analysis", "clique_analysis", "reachability", "global_metrics",
    "graph_structure_analysis", "connectivity_analysis", "tree_dag_analysis",
    "matching_coloring_analysis", "distance_measures",
    "visualize_subgraph", "create_analysis_report",
})

GRAPH_WRITE_TOOLS = frozenset({
    "load_graph", "load_graph_from_upload", "load_graph_from_text",
    "load_graph_from_triples", "clear_graph", "undo_graph",
    "load_graph_version", "discard_graph_version", "to_undirected",
})

_POLICIES = {
    **{name: _policy(reads={"graph"}) for name in GRAPH_READ_TOOLS},
    **{name: _policy(writes={"graph"}) for name in GRAPH_WRITE_TOOLS},
    "subgraph_extraction": _policy(writes={"graph"}),
    # A dataset save also overwrites the session's dataset_binding.json.
    "list_datasets": _policy(reads={"datasets", "construction:*"}),
    "load_dataset": _policy(reads={"datasets", "construction:*"},
                            writes={"graph", "dataset_binding"}),
    "save_as_dataset": _policy(reads={"graph"}, writes={"datasets", "dataset_binding"}),
    "upload_document": _policy(writes={"documents"}),
    "retrieve_context": _policy(reads={"documents"}),
    "get_job_status": _policy(reads={"jobs"}),
    "cancel_job": _policy(writes={"jobs"}, barrier=True),
    # Large-graph operations use their own persistent snapshot/job catalog.
    # Keep their pre-existing conservative barrier behavior explicit until
    # that catalog exposes resource identities to the shared DAG scheduler.
    **{name: _policy(barrier=True) for name in {
        "get_job_result", "prepare_large_graph", "run_large_graph_algorithm",
        "list_large_graph_versions", "get_large_graph_artifact",
        "get_large_graph_capabilities",
    }},
    "nebula_status": _policy(reads={"graph", "nebula"}),
    "nebula_sync_graph": _policy(reads={"graph"}, writes={"nebula"}, background=True),
    "nebula_get_node_info": _policy(reads={"graph", "nebula"}),
    "nebula_get_neighbors": _policy(reads={"graph", "nebula"}),
    "nebula_shortest_path": _policy(reads={"graph", "nebula"}),
    # The classification bundle/binding is independent of the active graph.
    "load_graph_classification_dataset": _policy(
        reads={"uploads"}, writes={"graph_ml_dataset"}, background=True),
    "load_graph_classification_graph": _policy(
        reads={"graph_ml_dataset"}, writes={"graph"}),
    # Inference only reads existing model artifacts. Training creates models;
    # GPU/RNG concurrency remains a separate bounded heavy-work admission rule.
    "train": _policy(reads={"graph", "graph_ml_dataset", "models"},
                     writes={"models"}, background=True),
    "infer": _policy(reads={"graph", "graph_ml_dataset", "models"}, background=True),
    "visualize_model_architecture": _policy(reads={"models"}),
    "get_graph_construction_capabilities": _policy(),
    "get_graph_construction_status": _policy(reads={"construction:*"}),
    "get_constructed_graph": _policy(reads={"construction:*"}),
    "preview_graph_optimization": _policy(reads={"construction:*"}),
    "commit_graph_optimization": _policy(writes={"construction:*"}),
    "load_constructed_graph": _policy(reads={"construction:*"},
                                      writes={"graph", "dataset_binding"}),
    # These change request identity or session lifecycle, so ordering only a
    # graph resource would miss documents, jobs, models, and dataset bindings.
    "register_session": _policy(barrier=True),
    "delete_graph_session": _policy(barrier=True),
    "set_session_config": _policy(writes={"session_config"}, barrier=True),
}

for _name in ("load_graph_from_upload", "load_graph_from_text"):
    _POLICIES[_name] = replace(
        _POLICIES[_name], reads=frozenset({"uploads", "session_config"}), background=True)

# Upload autodetection can bind a classification bundle instead of a graph.
_POLICIES["load_graph_from_upload"] = replace(
    _POLICIES["load_graph_from_upload"], writes=frozenset({"graph", "graph_ml_dataset"}))

REGISTERED_TOOL_NAMES = frozenset(_POLICIES)
_UNKNOWN = _policy(barrier=True)


def execution_policy(
    name: str, arguments: Mapping[str, Any] | None = None,
) -> ToolExecutionPolicy:
    """Resolve a conservative contract, including argument-sensitive effects.

    Unknown tools remain barriers until reviewed and explicitly registered.
    Only literal ``False`` disables subgraph publication: missing, null, and
    malformed arguments must never accidentally grant read-only execution.
    """
    args = arguments if isinstance(arguments, Mapping) else {}
    policy = _POLICIES.get(name, _UNKNOWN)
    if name == "subgraph_extraction" and args.get("create_version") is False:
        return _policy(reads={"graph"})
    if "construction:*" in policy.reads or "construction:*" in policy.writes:
        dataset_id = args.get("dataset_id")
        if isinstance(dataset_id, str) and dataset_id.strip():
            # Escape the wildcard token so a literal ID cannot broaden scope.
            resource = "construction:" + dataset_id.replace("%", "%25").replace("*", "%2A")
            policy = replace(
                policy,
                reads=frozenset(resource if r == "construction:*" else r for r in policy.reads),
                writes=frozenset(resource if r == "construction:*" else r for r in policy.writes),
            )
    return policy


tool_execution_policy = execution_policy


def resource_scope(resource: str) -> str:
    """Return the owner used to qualify a resource before cross-call locking.

    Models stay session-scoped: ArtifactStore resolves and publishes under the
    runtime ID ``user:session``, including the isolated-worker artifact path.
    """
    return "user" if resource == "datasets" or resource.startswith("construction:") else "session"


def resources_overlap(left: str, right: str) -> bool:
    """Match an exact resource or a namespace wildcard (e.g. construction:*)."""
    return (
        left == right
        or (left.endswith(":*") and right.startswith(left[:-1]))
        or (right.endswith(":*") and left.startswith(right[:-1]))
    )


def tools_conflict(left: ToolExecutionPolicy, right: ToolExecutionPolicy) -> bool:
    """RAW, WAR and WAW create edges; independent reads can run together."""
    if left.barrier or right.barrier:
        return True
    return (
        any(resources_overlap(a, b) for a in left.writes for b in right.reads | right.writes)
        or any(resources_overlap(a, b) for a in left.reads for b in right.writes)
    )


def get_tool_execution_metadata(name: str) -> dict[str, Any]:
    """JSON-safe catalog metadata; runtime arguments may narrow the contract."""
    metadata = execution_policy(name).as_dict()
    if name == "subgraph_extraction":
        metadata["argument_rules"] = {"create_version=false": {"reads": ["graph"], "writes": []}}
    if any(r.startswith("construction:") for r in execution_policy(name).reads | execution_policy(name).writes):
        metadata["resource_arguments"] = {"construction:*": "dataset_id"}
    return metadata
