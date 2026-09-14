"""Canonical graph-tool names and trace normalization.

The benchmark questions use a mixture of concrete actions (for example
``compute_ego_density``), NetworkX-style concepts (``nx.density``), and
combined ML concepts (``structural_node_embedding_classification``).  The
actual Codex graph skill exposes a fixed set of command-line actions.  This
module is the boundary between those two representations:

* :func:`expected_tool_path` converts a question's ``required_tools`` into an
  ordered canonical action path.
* :func:`normalize_trace_tool_calls` converts structured tool calls and shell
  commands into the same namespace while retaining the raw call and status.

Unknown question requirements are rejected by default.  Unknown observed
calls are deliberately retained in the strict path so that an evaluator does
not accidentally reward an extra or misspelled call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


GRAPH_RETRIEVAL_ACTIONS: tuple[str, ...] = (
    "inspect_pt_with_pyg",
    "inspect_pt_without_pyg",
    "extract_key_with_pyg",
    "extract_key_without_pyg",
)

GRAPH_IO_ACTIONS: tuple[str, ...] = (
    "extract_key_to_numpy_with_pyg",
    "extract_key_to_numpy_without_pyg",
    "extract_graph_by_id_with_pyg",
)

GRAPH_THEORY_ACTIONS: tuple[str, ...] = (
    "connectivity_checking",
    "get_undirected_graph_no_selfloops",
    "extract_n_hop_ego_subgraph",
    "sample_n_hop_subgraph",
    "check_edge_existence",
    "get_node_degree",
    "get_node_neighbors",
    "get_common_neighbors",
    "get_node_number",
    "get_edge_number",
    "calculate_shortest_path",
    "get_bfs_edges",
    "get_dfs_edges",
    "has_path",
    "get_triangles",
    "get_density",
    "get_closeness_centrality",
    "get_betweenness_centrality",
    "get_harmonic_centrality",
    "get_degree_centrality",
    "get_avg_neighbor_degree",
    "get_wiener_index",
    "get_barycenter",
    "get_center",
    "get_diameter",
    "get_periphery",
    "get_radius",
    "get_global_efficiency",
    "get_transitivity",
    "get_clustering_coefficient",
    "get_constraint",
    "get_effective_size",
    "get_degree_assortativity",
    "get_degree_connectivity",
    "get_isolate_number",
    "get_connected_components_number",
    "get_strongly_connected_components_number",
    "get_weakly_connected_components_number",
    "is_tournament",
    "is_bipartite",
    "is_eulerian",
    "is_regular",
    "is_connected",
    "is_weakly_connected",
    "is_strongly_connected",
    "get_topological_sort",
    "find_cycle",
    "get_dominating_set",
    "get_maximal_independent_set",
    "get_min_edge_cover",
    "get_max_weight_matching",
    "get_min_weight_matching",
    "get_bridges",
    "get_minimum_spanning_edges",
    "get_maximum_flow",
    "get_k_core",
    "get_maximal_cliques",
    "get_min_vertex_cover",
    "get_resource_allocation_index",
    "get_jaccard_coefficient",
    "get_adamic_adar_index",
)

GRAPH_INSIGHT_ACTIONS: tuple[str, ...] = (
    "get_local_neighbors_by_hop",
    "extract_ego_boundary",
    "filter_nodes_by_all_degree",
    "filter_nodes_by_in_degree",
    "filter_nodes_by_out_degree",
    "detect_local_cycles",
    "compute_local_clustering",
    "find_stepped_shortest_path",
    "detect_path_convergence",
    "find_local_bridge_nodes",
    "extract_local_k_core",
    "enumerate_local_cliques",
    "compute_ego_density",
    "count_common_neighbors",
    "local_label_propagation",
    "simulate_edge_perturbation",
    "check_local_reachability",
)

GRAPH_ML_ACTIONS: tuple[str, ...] = (
    "structural_node_embedding_classification_train",
    "structural_node_embedding_classification_infer",
    "structural_node_embedding_link_prediction_train",
    "structural_node_embedding_link_prediction_infer",
    "structural_graph_embedding_classification_train",
    "structural_graph_embedding_classification_infer",
)

GRAPH_TASK_SOLVER_ACTIONS: tuple[str, ...] = ("graph_task_solver",)

CANONICAL_TOOL_GROUPS: Mapping[str, tuple[str, ...]] = {
    "retrieval": GRAPH_RETRIEVAL_ACTIONS,
    "io": GRAPH_IO_ACTIONS,
    "theory": GRAPH_THEORY_ACTIONS,
    "insight": GRAPH_INSIGHT_ACTIONS,
    "ml": GRAPH_ML_ACTIONS,
    "solver": GRAPH_TASK_SOLVER_ACTIONS,
}

CANONICAL_TOOL_NAMES: frozenset[str] = frozenset(
    name for group in CANONICAL_TOOL_GROUPS.values() for name in group
)


# ``required_tools`` values found in the benchmark question corpus.  Values are
# tuples because a conceptual ML operation represents the required train/infer
# pair rather than a single action.
_QUESTION_ALIASES: dict[str, tuple[str, ...]] = {
    # Retrieval-style concepts.
    "extract_subgraph": ("extract_n_hop_ego_subgraph",),
    "subgraph_extraction": ("extract_n_hop_ego_subgraph",),
    "graph.degree": ("get_node_degree",),
    "graph.has_edge": ("check_edge_existence",),
    "graph.neighbors": ("get_node_neighbors",),
    "len(graph.edges())": ("get_edge_number",),
    "len(graph.nodes())": ("get_node_number",),
    # Local catalog concepts that occur in insight/prediction questions.
    "shortest_path": ("calculate_shortest_path",),
    "graph_connectivity": ("connectivity_checking",),
    "minimum_spanning_tree": ("get_minimum_spanning_edges",),
    "min_vertex_cover": ("get_min_vertex_cover",),
    # NetworkX concepts used by theory questions.
    "nx.adamic_adar_index": ("get_adamic_adar_index",),
    "nx.average_neighbor_degree": ("get_avg_neighbor_degree",),
    "nx.barycenter": ("get_barycenter",),
    "nx.betweenness_centrality": ("get_betweenness_centrality",),
    "nx.bfs_edges": ("get_bfs_edges",),
    "nx.bridges": ("get_bridges",),
    "nx.center": ("get_center",),
    "nx.closeness_centrality": ("get_closeness_centrality",),
    "nx.clustering": ("get_clustering_coefficient",),
    "nx.common_neighbors": ("get_common_neighbors",),
    "nx.constraint": ("get_constraint",),
    "nx.degree_assortativity_coefficient": ("get_degree_assortativity",),
    "nx.degree_centrality": ("get_degree_centrality",),
    "nx.density": ("get_density",),
    "nx.dfs_edges": ("get_dfs_edges",),
    "nx.diameter": ("get_diameter",),
    "nx.dominating_set": ("get_dominating_set",),
    "nx.effective_size": ("get_effective_size",),
    "nx.find_cliques": ("get_maximal_cliques",),
    "nx.find_cycle": ("find_cycle",),
    "nx.global_efficiency": ("get_global_efficiency",),
    "nx.harmonic_centrality": ("get_harmonic_centrality",),
    "nx.has_path": ("has_path",),
    "nx.is_bipartite": ("is_bipartite",),
    "nx.is_eulerian": ("is_eulerian",),
    "nx.is_regular": ("is_regular",),
    "nx.is_strongly_connected": ("is_strongly_connected",),
    "nx.is_tournament": ("is_tournament",),
    "nx.is_weakly_connected": ("is_weakly_connected",),
    "nx.jaccard_coefficient": ("get_jaccard_coefficient",),
    "nx.k_core": ("get_k_core",),
    "nx.maximal_independent_set": ("get_maximal_independent_set",),
    "nx.maximum_flow": ("get_maximum_flow",),
    "nx.max_weight_matching": ("get_max_weight_matching",),
    "nx.min_edge_cover": ("get_min_edge_cover",),
    "nx.minimum_spanning_edges": ("get_minimum_spanning_edges",),
    "nx.min_weight_matching": ("get_min_weight_matching",),
    "nx.number_connected_components": ("get_connected_components_number",),
    "nx.number_of_isolates": ("get_isolate_number",),
    "nx.number_strongly_connected_components": (
        "get_strongly_connected_components_number",
    ),
    "nx.number_weakly_connected_components": (
        "get_weakly_connected_components_number",
    ),
    "nx.periphery": ("get_periphery",),
    "nx.radius": ("get_radius",),
    "nx.resource_allocation_index": ("get_resource_allocation_index",),
    "nx.shortest_path": ("calculate_shortest_path",),
    "nx.transitivity": ("get_transitivity",),
    "nx.triangles": ("get_triangles",),
    "nx.wiener_index": ("get_wiener_index",),
    # Combined ML concepts in prediction questions.
    "structural_node_embedding_classification": (
        "structural_node_embedding_classification_train",
        "structural_node_embedding_classification_infer",
    ),
    "structural_node_embedding_link_prediction": (
        "structural_node_embedding_link_prediction_train",
        "structural_node_embedding_link_prediction_infer",
    ),
    "structural_graph_embedding_classification": (
        "structural_graph_embedding_classification_train",
        "structural_graph_embedding_classification_infer",
    ),
    # Short task aliases are useful for generated/custom question sets.
    "node_classification": (
        "structural_node_embedding_classification_train",
        "structural_node_embedding_classification_infer",
    ),
    "link_prediction": (
        "structural_node_embedding_link_prediction_train",
        "structural_node_embedding_link_prediction_infer",
    ),
    "graph_classification": (
        "structural_graph_embedding_classification_train",
        "structural_graph_embedding_classification_infer",
    ),
    "node_classification_train": (
        "structural_node_embedding_classification_train",
    ),
    "node_classification_infer": (
        "structural_node_embedding_classification_infer",
    ),
    "link_prediction_train": (
        "structural_node_embedding_link_prediction_train",
    ),
    "link_prediction_infer": (
        "structural_node_embedding_link_prediction_infer",
    ),
    "graph_classification_train": (
        "structural_graph_embedding_classification_train",
    ),
    "graph_classification_infer": (
        "structural_graph_embedding_classification_infer",
    ),
    "graph_task_solver.py": ("graph_task_solver",),
}

_ML_TASK_PREFIXES: Mapping[str, str] = {
    "nc": "structural_node_embedding_classification",
    "node": "structural_node_embedding_classification",
    "node_classification": "structural_node_embedding_classification",
    "node_embedding_classification": "structural_node_embedding_classification",
    "lp": "structural_node_embedding_link_prediction",
    "link": "structural_node_embedding_link_prediction",
    "link_prediction": "structural_node_embedding_link_prediction",
    "node_embedding_link_prediction": "structural_node_embedding_link_prediction",
    "gc": "structural_graph_embedding_classification",
    "graph": "structural_graph_embedding_classification",
    "graph_classification": "structural_graph_embedding_classification",
    "graph_embedding_classification": "structural_graph_embedding_classification",
}

_SCRIPT_GROUPS: Mapping[str, frozenset[str]] = {
    "graph_retrieval_tools.py": frozenset(GRAPH_RETRIEVAL_ACTIONS),
    "graph_io_tools.py": frozenset(GRAPH_IO_ACTIONS),
    "graph_theory_tools.py": frozenset(GRAPH_THEORY_ACTIONS),
    "graph_insight_tools.py": frozenset(GRAPH_INSIGHT_ACTIONS),
    "graph_ml_tools.py": frozenset(GRAPH_ML_ACTIONS),
}

_SCRIPT_RE = re.compile(
    r"(?P<script>graph_(?:retrieval|io|theory|insight|ml)_tools\.py|"
    r"graph_task_solver\.py)[\"']?(?:\s+(?P<action>[A-Za-z_][A-Za-z0-9_.-]*))?",
    re.IGNORECASE,
)

_SUCCESS_STATUSES = {
    "success",
    "succeeded",
    "ok",
    "complete",
    "completed",
    "pass",
    "passed",
    "done",
}
_FAILURE_STATUSES = {
    "error",
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "rejected",
}


@dataclass(frozen=True)
class NormalizedTrace:
    """JSON-friendly normalized tool trace.

    ``strict_path`` contains every observed tool call, including failed and
    unknown calls.  Known aliases are represented by their canonical names;
    unknown calls use their raw name so they remain visible to exact matching.
    ``successful_path`` applies the same rule but keeps only calls whose
    normalized status is ``"success"``.
    """

    calls: tuple[Mapping[str, Any], ...]
    strict_path: tuple[str, ...]
    successful_path: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": [dict(call) for call in self.calls],
            "strict_path": list(self.strict_path),
            "successful_path": list(self.successful_path),
        }


def _alias_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s/-]+", "_", text)
    return text


def _ml_task_prefix(value: Any) -> str | None:
    return _ML_TASK_PREFIXES.get(_alias_key(value))


def _task_hint(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                return _task_hint(parsed)
    if isinstance(value, Mapping):
        for key in ("task", "task_type", "ml_task", "model_task"):
            prefix = _ml_task_prefix(value.get(key))
            if prefix:
                return prefix
        for key in ("arguments", "args", "input", "parameters", "context"):
            nested = value.get(key)
            if isinstance(nested, (Mapping, str)):
                prefix = _task_hint(nested)
                if prefix:
                    return prefix
    else:
        for key in ("task", "task_type", "ml_task", "model_task"):
            prefix = _ml_task_prefix(getattr(value, key, None))
            if prefix:
                return prefix
    return None


def canonicalize_tool_name(
    name: Any,
    *,
    task: Any = None,
    strict: bool = True,
) -> tuple[str, ...]:
    """Return one or more canonical actions for a concrete or concept name.

    Combined ML names expand to their ordered training and inference actions.
    Generic legacy ``train``/``infer`` names are accepted only when ``task``
    supplies an unambiguous NC/LP/GC task hint.
    """

    raw = str(name or "").strip()
    if not raw:
        if strict:
            raise ValueError("tool name must be a non-empty string")
        return ()

    key = _alias_key(raw)
    if key in CANONICAL_TOOL_NAMES:
        return (key,)
    if key in _QUESTION_ALIASES:
        return _QUESTION_ALIASES[key]

    if key in {"train", "infer"}:
        prefix = _task_hint(task)
        if prefix:
            return (f"{prefix}_{key}",)

    if strict:
        raise ValueError(f"unknown benchmark graph tool: {raw}")
    return ()


def _required_tools_value(task: Any) -> Any:
    if isinstance(task, (str, bytes)):
        return task
    if isinstance(task, Sequence) and not isinstance(task, Mapping):
        return task
    if isinstance(task, Mapping):
        if "required_tools" in task:
            return task.get("required_tools")
        for parent in ("evaluation", "metadata", "context", "ground_truth"):
            nested = task.get(parent)
            if isinstance(nested, Mapping) and "required_tools" in nested:
                return nested.get("required_tools")
        return ()
    if hasattr(task, "required_tools"):
        return getattr(task, "required_tools")
    return ()


def _split_required_tool_string(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    # A single concept may contain punctuation such as ``len(graph.nodes())``;
    # split only on explicit path separators used by hand-authored tasks.
    if "->" in text or "," in text or "|" in text:
        return [item.strip() for item in re.split(r"\s*(?:->|,|\|)\s*", text) if item.strip()]
    return [text]


def _required_entry_name(entry: Any) -> Any:
    if not isinstance(entry, Mapping):
        return entry
    for key in ("canonical_name", "action", "tool_name", "name", "tool"):
        if entry.get(key):
            return entry[key]
    return ""


def expected_tool_path(task: Any, *, strict: bool = True) -> list[str]:
    """Normalize a question's ordered ``required_tools`` path.

    Args:
        task: A question mapping/object, a ``required_tools`` sequence, or one
            tool string.
        strict: Raise ``ValueError`` for unknown concepts when true.  When
            false, unknown requirements are skipped.

    Returns:
        A flat ordered list containing only canonical action names.  ML base
        concepts contribute two entries: training followed by inference.  An
        empty requirement on an NC/LP/GC task implies that same two-step path.
    """

    required = _required_tools_value(task)
    if isinstance(required, str):
        entries: list[Any] = _split_required_tool_string(required)
    elif isinstance(required, Mapping):
        entries = [required]
    elif required is None:
        entries = []
    else:
        try:
            entries = list(required)
        except TypeError as exc:
            raise ValueError("required_tools must be a string or sequence") from exc

    # Pure ML question files commonly leave ``required_tools`` empty because
    # the task type itself defines the mandatory two-step virtual workflow.
    # Infer that workflow only when the requirements are genuinely empty;
    # unknown/non-empty requirements must still follow the normal strictness
    # rules below.
    if not entries:
        ml_prefix = _task_hint(task)
        if ml_prefix:
            return [f"{ml_prefix}_train", f"{ml_prefix}_infer"]
        return []

    path: list[str] = []
    for entry in entries:
        names = (
            _split_required_tool_string(entry)
            if isinstance(entry, str)
            else [_required_entry_name(entry)]
        )
        for name in names:
            hint_source = entry if isinstance(entry, Mapping) else task
            path.extend(
                canonicalize_tool_name(name, task=hint_source, strict=strict)
            )
    return path


def _mapping_from_json(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _call_arguments(call: Mapping[str, Any]) -> Any:
    function = call.get("function")
    if isinstance(function, Mapping) and "arguments" in function:
        return function.get("arguments")
    for key in ("arguments", "args", "input", "parameters", "tool_args"):
        if key in call:
            return call.get(key)
    return {}


def _raw_call_name(call: Mapping[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, Mapping) and function.get("name"):
        return str(function["name"])
    for key in ("canonical_name", "raw_name", "tool_name", "name", "tool"):
        value = call.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested_name = value.get("name") or value.get("tool_name")
            if nested_name:
                return str(nested_name).strip()
    if any(key in call for key in ("cmd", "command", "shell_command")):
        return "shell"
    return "unknown_tool"


def _find_command_values(value: Any, *, _depth: int = 0) -> list[Any]:
    if _depth > 3:
        return []
    parsed = _mapping_from_json(value)
    if parsed is not None and parsed is not value:
        return _find_command_values(parsed, _depth=_depth + 1)
    if isinstance(value, Mapping):
        found: list[Any] = []
        for key in ("cmd", "command", "shell_command", "argv"):
            if key in value:
                found.append(value[key])
        for key in ("arguments", "args", "input", "parameters", "tool_args"):
            if key in value:
                found.extend(_find_command_values(value[key], _depth=_depth + 1))
        return found
    return []


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(str(part) for part in value)
    return str(value or "")


def _extract_command_actions(command: Any) -> list[tuple[str, str]]:
    """Return ``(script, action)`` pairs found in one shell command."""

    text = _command_text(command)
    extracted: list[tuple[str, str]] = []
    for match in _SCRIPT_RE.finditer(text):
        script = match.group("script").lower()
        if script == "graph_task_solver.py":
            extracted.append((script, "graph_task_solver"))
            continue
        action = str(match.group("action") or "").strip()
        if action:
            extracted.append((script, action))
    return extracted


def _status_value(call: Mapping[str, Any]) -> Any:
    if "status" in call:
        return call.get("status")
    for key in ("success", "ok"):
        if key in call and isinstance(call[key], bool):
            return "success" if call[key] else "failed"
    for key in ("is_error", "isError"):
        if key in call and isinstance(call[key], bool):
            return "failed" if call[key] else "success"
    for key in ("exit_code", "returncode", "return_code"):
        if key in call and call[key] is not None:
            try:
                return "success" if int(call[key]) == 0 else "failed"
            except (TypeError, ValueError):
                pass
    for key in ("result", "output", "response"):
        nested = _mapping_from_json(call.get(key))
        if nested is not None:
            status = _status_value(nested)
            if status is not None:
                return status
    return None


def _normalize_status(call: Mapping[str, Any], default_status: str) -> tuple[str, Any]:
    raw_status = _status_value(call)
    if raw_status is None:
        return _alias_key(default_status) or "unknown", None
    if isinstance(raw_status, bool):
        return ("success" if raw_status else "failed"), raw_status
    normalized = _alias_key(raw_status)
    if normalized in _SUCCESS_STATUSES:
        return "success", raw_status
    if normalized in _FAILURE_STATUSES:
        return "failed", raw_status
    return normalized or "unknown", raw_status


def _call_id(call: Mapping[str, Any]) -> str | None:
    for key in ("call_id", "tool_call_id", "id"):
        value = call.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _unwrap_call(item: Any) -> Mapping[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    for key in ("tool_call", "tool_execution", "function_call"):
        nested = item.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            for inherited in (
                "status",
                "success",
                "ok",
                "is_error",
                "isError",
                "exit_code",
                "returncode",
                "call_id",
                "tool_call_id",
            ):
                if inherited in item and inherited not in merged:
                    merged[inherited] = item[inherited]
            return merged
    return item


def _trace_items(trace: Any) -> list[Any]:
    if trace is None:
        return []
    if isinstance(trace, (str, bytes)):
        return [trace]
    if isinstance(trace, Mapping):
        for key in ("tool_calls", "calls", "events", "trace", "trajectory", "steps"):
            value = trace.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return list(value)
        return [trace]
    try:
        return list(trace)
    except TypeError as exc:
        raise ValueError("trace must be a tool call, mapping, or iterable") from exc


def _looks_like_result_only(call: Mapping[str, Any]) -> bool:
    event_type = _alias_key(call.get("type"))
    return event_type in {"tool_result", "function_call_output", "command_result"}


def _result_statuses(items: Sequence[Any]) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    for item in items:
        call = _unwrap_call(item)
        if call is None:
            continue
        event_type = _alias_key(call.get("type"))
        if event_type not in {
            "tool_result",
            "function_call_output",
            "command_result",
        }:
            continue
        call_id = _call_id(call)
        status = _status_value(call)
        if call_id and status is not None:
            statuses[call_id] = status
    return statuses


def normalize_trace_tool_calls(
    trace: Any,
    *,
    default_status: str = "unknown",
) -> NormalizedTrace:
    """Normalize structured and command-backed tool calls.

    Supported observations include direct ``name``/``tool_name`` calls,
    OpenAI-style ``function`` calls, nested ``tool_call``/``tool_execution``
    records, and exec/shell calls containing one or more graph skill scripts.
    A shell command containing multiple graph actions yields one normalized
    entry per action in command order.

    Args:
        trace: A single call, an iterable of calls, or a mapping containing
            ``tool_calls``, ``calls``, ``events``, ``trace``, ``trajectory`` or
            ``steps``.
        default_status: Status assigned when the trace supplies no success or
            failure evidence.  The conservative default is ``"unknown"``.
    """

    items = _trace_items(trace)
    result_statuses = _result_statuses(items)
    calls: list[Mapping[str, Any]] = []

    for source_index, item in enumerate(items):
        if isinstance(item, str):
            call: Mapping[str, Any] = {"name": item}
        else:
            unwrapped = _unwrap_call(item)
            if unwrapped is None or _looks_like_result_only(unwrapped):
                continue
            call = unwrapped

        raw_name = _raw_call_name(call)
        arguments = _call_arguments(call)
        if raw_name == "unknown_tool" and not _find_command_values(call):
            # A trace container commonly interleaves assistant/user messages,
            # reasoning events and tool calls.  Only tool-shaped observations
            # belong in a tool path.
            continue
        call_id = _call_id(call)
        status_call = dict(call)
        if _status_value(status_call) is None and call_id in result_statuses:
            status_call["status"] = result_statuses[call_id]
        status, raw_status = _normalize_status(status_call, default_status)

        expanded: list[tuple[str | None, str, str]] = []
        command_actions: list[tuple[str, str]] = []
        # A captured authoritative trajectory may itself be normalized and
        # later replayed for evaluation. Preserve that canonical observation
        # instead of trying to reconstruct a shell command whose arguments
        # were deliberately omitted from the normalized record.
        if "canonical_name" in call:
            recorded = call.get("canonical_name")
            if isinstance(recorded, str) and recorded in CANONICAL_TOOL_NAMES:
                expanded.append(
                    (
                        recorded,
                        "normalized",
                        str(call.get("extracted_name") or recorded),
                    )
                )
            else:
                expanded.append(
                    (
                        None,
                        "normalized",
                        str(call.get("extracted_name") or raw_name),
                    )
                )
        else:
            command_values = _find_command_values(call)
            for value in command_values:
                command_actions.extend(_extract_command_actions(value))
            if not command_actions and raw_name in {
                "shell",
                "exec",
                "exec_command",
                "bash",
                "command",
            }:
                command_actions.extend(_extract_command_actions(arguments))

        if not expanded and command_actions:
            for script, action in command_actions:
                allowed = _SCRIPT_GROUPS.get(script)
                if action == "graph_task_solver":
                    expanded.append((action, "command", action))
                elif allowed is not None and action in allowed:
                    expanded.append((action, "command", action))
                else:
                    canonical = canonicalize_tool_name(action, task=arguments, strict=False)
                    if canonical and all(name in (allowed or ()) for name in canonical):
                        expanded.extend((name, "command_alias", action) for name in canonical)
                    else:
                        expanded.append((None, "command", action or script))
        elif not expanded:
            direct_name = raw_name
            raw_basename = raw_name.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if raw_basename in _SCRIPT_GROUPS:
                args_map = _mapping_from_json(arguments) or {}
                direct_name = str(args_map.get("action") or "")
                if (
                    not direct_name
                    and isinstance(args_map.get("argv"), Sequence)
                    and not isinstance(args_map.get("argv"), (str, bytes))
                ):
                    argv = args_map["argv"]
                    direct_name = str(argv[0]) if argv else ""
            elif raw_basename in {"graph_task_solver", "graph_task_solver.py"}:
                direct_name = "graph_task_solver"
            canonical_names = canonicalize_tool_name(
                direct_name,
                task=arguments if arguments else call,
                strict=False,
            )
            if canonical_names:
                expanded.extend((name, "structured", direct_name) for name in canonical_names)
            else:
                expanded.append((None, "structured", direct_name or raw_name))

        for expansion_index, (canonical_name, source, extracted_name) in enumerate(expanded):
            record: dict[str, Any] = {
                "index": len(calls),
                "source_index": source_index,
                "raw_name": raw_name,
                "canonical_name": canonical_name,
                "status": status,
                "source": source,
            }
            if extracted_name and extracted_name != raw_name:
                record["extracted_name"] = extracted_name
            if len(expanded) > 1:
                record["expansion_index"] = expansion_index
            if call_id is not None:
                record["call_id"] = call_id
            if raw_status is not None and str(raw_status) != status:
                record["raw_status"] = raw_status
            calls.append(record)

    def path_name(call: Mapping[str, Any]) -> str:
        canonical = call.get("canonical_name")
        return str(canonical if canonical else call.get("extracted_name") or call["raw_name"])

    strict_path = tuple(path_name(call) for call in calls)
    successful_path = tuple(
        path_name(call) for call in calls if call.get("status") == "success"
    )
    return NormalizedTrace(tuple(calls), strict_path, successful_path)


__all__ = [
    "CANONICAL_TOOL_GROUPS",
    "CANONICAL_TOOL_NAMES",
    "GRAPH_INSIGHT_ACTIONS",
    "GRAPH_IO_ACTIONS",
    "GRAPH_ML_ACTIONS",
    "GRAPH_RETRIEVAL_ACTIONS",
    "GRAPH_TASK_SOLVER_ACTIONS",
    "GRAPH_THEORY_ACTIONS",
    "NormalizedTrace",
    "canonicalize_tool_name",
    "expected_tool_path",
    "normalize_trace_tool_calls",
]
