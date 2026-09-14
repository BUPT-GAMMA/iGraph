"""Deterministic answer and canonical tool-path scoring."""

from __future__ import annotations

import json
import math
import hashlib
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from .tool_names import expected_tool_path, normalize_trace_tool_calls


_CITATION_SOLVER_DATASETS = frozenset({"arxiv", "cora", "pubmed"})
_SOLVER_QUESTION_PATTERNS = (
    "what are the neighbors",
    "neighbors of node",
    "direct citation neighbor",
    "local neighborhood",
    "cohesion",
    "frontier",
    "top 5%",
    "top-5%",
    "shortest path",
    "reachability",
    "local connector",
    "articulation",
    "maximal clique",
    "overlapping",
    "shared citation",
    "shared direct",
    "common neighbors",
    "relationship between",
)
_SOLVER_COVERED_ACTIONS = frozenset(
    {
        "extract_n_hop_ego_subgraph",
        "get_node_degree",
        "get_node_neighbors",
        "get_common_neighbors",
        "calculate_shortest_path",
        "has_path",
        "get_local_neighbors_by_hop",
        "extract_ego_boundary",
        "filter_nodes_by_all_degree",
        "compute_local_clustering",
        "find_stepped_shortest_path",
        "detect_path_convergence",
        "find_local_bridge_nodes",
        "enumerate_local_cliques",
        "compute_ego_density",
        "count_common_neighbors",
        "check_local_reachability",
    }
)


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _task_value(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _parse_json_like(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str):
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            return value, False
        return value, True
    stripped = value.strip()
    if not stripped:
        return value, False
    fenced = False
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1]).strip()
            fenced = True
    try:
        parsed = json.loads(stripped, parse_constant=_reject_nonfinite_json)
        return parsed, not fenced
    except (TypeError, ValueError):
        return value.strip(), False


def _unwrap_actual_scalar(actual: Any, task_type: str) -> Any:
    if not isinstance(actual, Mapping):
        return actual
    preferred = ["answer"]
    if task_type == "LP":
        preferred.extend(["has_edge", "prediction", "label"])
    else:
        preferred.extend(["label", "prediction", "has_edge"])
    for key in preferred:
        if key in actual:
            return actual[key]
    predictions = actual.get("predictions")
    if isinstance(predictions, list) and len(predictions) == 1:
        return predictions[0]
    return actual


def _scalar_equal(expected: Any, actual: Any, tolerance: float) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected is actual
    if isinstance(expected, Real) and isinstance(actual, Real):
        expected_float = float(expected)
        actual_float = float(actual)
        return (
            math.isfinite(expected_float)
            and math.isfinite(actual_float)
            and math.isclose(
                expected_float,
                actual_float,
                rel_tol=tolerance,
                abs_tol=tolerance,
            )
        )
    if expected is None or actual is None:
        return expected is actual
    if isinstance(expected, str) or isinstance(actual, str):
        return (
            isinstance(expected, str)
            and isinstance(actual, str)
            and expected.strip() == actual.strip()
        )
    return expected == actual


def _match_tree(expected: Any, actual: Any, tolerance: float) -> tuple[int, int]:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return 0, max(1, _leaf_count(expected))
        if not expected:
            return (1, 1) if not actual else (0, 1)
        matched = 0
        total = 0
        for key, expected_value in expected.items():
            if key not in actual:
                total += max(1, _leaf_count(expected_value))
                continue
            child_matched, child_total = _match_tree(
                expected_value,
                actual[key],
                tolerance,
            )
            matched += child_matched
            total += child_total
        return matched, max(total, 1)
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return 0, max(1, _leaf_count(expected))
        if not expected:
            return 1, 1
        matched = 0
        total = 0
        for expected_value, actual_value in zip(expected, actual):
            child_matched, child_total = _match_tree(
                expected_value,
                actual_value,
                tolerance,
            )
            matched += child_matched
            total += child_total
        return matched, max(total, 1)
    return (1, 1) if _scalar_equal(expected, actual, tolerance) else (0, 1)


def _leaf_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(_leaf_count(item) for item in value.values()) or 1
    if isinstance(value, list):
        return sum(_leaf_count(item) for item in value) or 1
    return 1


def _expected_metadata(value: Any) -> dict[str, Any]:
    # Attempts can run concurrently and their artifacts may be visible to
    # sibling processes.  Never include a ground-truth preview here, even for
    # tiny values; expose only comparison-safe metadata.
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded = serialized.encode("utf-8")
    return {
        "expected_sha256": hashlib.sha256(encoded).hexdigest(),
        "expected_bytes": len(encoded),
        "expected_type": (
            "null"
            if value is None
            else "boolean"
            if isinstance(value, bool)
            else "object"
            if isinstance(value, Mapping)
            else "array"
            if isinstance(value, list)
            else "number"
            if isinstance(value, Real)
            else "string"
            if isinstance(value, str)
            else type(value).__name__
        ),
    }


def score_ground_truth(
    task: Any,
    final_answer: Any,
    *,
    numeric_tolerance: float = 1e-6,
) -> dict[str, Any]:
    expected = _task_value(task, "ground_truth")
    task_type = str(_task_value(task, "task_type", "") or "").upper()
    parsed_answer, json_valid = _parse_json_like(final_answer)

    if expected is None:
        return {
            "available": False,
            "protocol_valid": json_valid,
            "json_valid": json_valid,
            "matched_leaves": None,
            "total_leaves": None,
            "accuracy": None,
            "macro_accuracy": None,
            "top_level_accuracy": {},
            "answer_match": None,
            "exact": None,
        }

    evaluation_target = expected
    if isinstance(expected, Mapping) and "answer" in expected:
        evaluation_target = expected["answer"]
    if not isinstance(evaluation_target, (Mapping, list)):
        parsed_answer = _unwrap_actual_scalar(parsed_answer, task_type)

    matched, total = _match_tree(
        evaluation_target,
        parsed_answer,
        numeric_tolerance,
    )
    accuracy = matched / total if total else 0.0
    answer_match = matched == total
    field_scores: dict[str, float] = {}
    if isinstance(evaluation_target, Mapping):
        actual_mapping = parsed_answer if isinstance(parsed_answer, Mapping) else {}
        for key, expected_value in evaluation_target.items():
            field_matched, field_total = _match_tree(
                expected_value,
                actual_mapping.get(key, object()),
                numeric_tolerance,
            )
            field_scores[str(key)] = field_matched / field_total if field_total else 0.0
    macro_accuracy = (
        sum(field_scores.values()) / len(field_scores)
        if field_scores
        else accuracy
    )
    return {
        "available": expected is not None,
        "protocol_valid": json_valid,
        "json_valid": json_valid,
        "matched_leaves": matched,
        "total_leaves": total,
        "accuracy": accuracy,
        "macro_accuracy": macro_accuracy,
        "top_level_accuracy": field_scores,
        "answer_match": answer_match,
        "exact": bool(json_valid and answer_match),
        **_expected_metadata(evaluation_target),
    }


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for left_value in left:
        current = [0]
        for index, right_value in enumerate(right, start=1):
            if left_value == right_value:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def _path_metrics(expected: list[str], actual: list[str]) -> dict[str, Any]:
    common = _lcs_length(expected, actual)
    precision = common / len(actual) if actual else (1.0 if not expected else 0.0)
    recall = common / len(expected) if expected else (1.0 if not actual else 0.0)
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    denominator = max(len(expected), len(actual), 1)
    tsa_denominator = len(expected)
    return {
        "exact": expected == actual,
        "ordered_matches": common,
        "accuracy": common / denominator,
        # GraphAgentBench's published tool-selection accuracy (TSA) is a
        # micro-averaged LCS recall: sum(LCS) / sum(len(reference)).  Keep the
        # additive contribution here so dataset/category aggregation can use
        # the ratio of sums instead of averaging per-attempt percentages.
        "tsa_numerator": common,
        "tsa_denominator": tsa_denominator,
        "tool_selection_accuracy": (
            common / tsa_denominator if tsa_denominator else None
        ),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _declared_alternative_paths(task: Any) -> list[list[str]]:
    candidates: Any = None
    if isinstance(task, Mapping):
        candidates = task.get("accepted_tool_paths")
        if candidates is None:
            for key in ("evaluation", "context"):
                nested = task.get(key)
                if isinstance(nested, Mapping) and "accepted_tool_paths" in nested:
                    candidates = nested["accepted_tool_paths"]
                    break
    else:
        candidates = getattr(task, "accepted_tool_paths", None)
        if candidates is None:
            context = getattr(task, "context", None)
            if isinstance(context, Mapping):
                candidates = context.get("accepted_tool_paths")
    if candidates is None:
        return []
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("accepted_tool_paths must be an array of tool paths")
    alternatives: list[list[str]] = []
    for candidate in candidates:
        if not isinstance(candidate, Sequence) or isinstance(candidate, (str, bytes)):
            raise ValueError("each accepted tool path must be an array")
        normalized = expected_tool_path(list(candidate))
        if not normalized:
            raise ValueError("accepted tool paths cannot be empty")
        alternatives.append(normalized)
    return alternatives


def _solver_alternative(task: Any, primary: list[str]) -> list[str] | None:
    dataset = str(_task_value(task, "dataset", "") or "").casefold()
    category = str(_task_value(task, "category", "") or "").casefold()
    question = str(_task_value(task, "question", "") or "").casefold()
    if not primary or category not in {"theory", "retrieval", "insight"}:
        return None
    if dataset not in _CITATION_SOLVER_DATASETS:
        return None
    if not any(pattern in question for pattern in _SOLVER_QUESTION_PATTERNS):
        return None
    remainder = [name for name in primary if name not in _SOLVER_COVERED_ACTIONS]
    return ["graph_task_solver", *remainder]


def _accepted_tool_paths(task: Any, primary: list[str]) -> list[list[str]]:
    candidates = [primary, *_declared_alternative_paths(task)]
    solver = _solver_alternative(task, primary)
    if solver is not None:
        candidates.append(solver)
    unique: list[list[str]] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def validate_tool_path_spec(task: Any) -> list[list[str]]:
    """Canonicalize every declared reference path without scoring a trace."""

    primary = list(expected_tool_path(task))
    return _accepted_tool_paths(task, primary)


def _best_path_metrics(
    accepted: Sequence[list[str]], actual: list[str]
) -> tuple[list[str], dict[str, Any]]:
    scored = [(candidate, _path_metrics(candidate, actual)) for candidate in accepted]
    return max(
        scored,
        key=lambda item: (
            bool(item[1]["exact"]),
            (
                -1.0
                if item[1]["tool_selection_accuracy"] is None
                else float(item[1]["tool_selection_accuracy"])
            ),
            int(item[1]["ordered_matches"]),
            float(item[1]["f1"]),
        ),
    )


def score_tool_path(task: Any, raw_tool_calls: Sequence[Any]) -> dict[str, Any]:
    expected = list(expected_tool_path(task))
    accepted = _accepted_tool_paths(task, expected)
    normalized = normalize_trace_tool_calls(raw_tool_calls)
    strict_path = list(normalized.strict_path)
    successful_path = list(normalized.successful_path)
    strict_expected, strict_metrics = _best_path_metrics(accepted, strict_path)
    successful_expected, successful_metrics = _best_path_metrics(
        accepted, successful_path
    )
    return {
        "available": bool(expected),
        "expected": expected,
        "accepted_paths": accepted,
        "strict_actual": strict_path,
        "successful_actual": successful_path,
        "strict": {**strict_metrics, "matched_expected": strict_expected},
        "successful": {
            **successful_metrics,
            "matched_expected": successful_expected,
        },
        "normalized_tool_calls": [dict(call) for call in normalized.calls],
    }


def score_attempt(
    task: Any,
    final_answer: Any,
    raw_tool_calls: Sequence[Any],
    *,
    numeric_tolerance: float = 1e-6,
) -> dict[str, Any]:
    return {
        "ground_truth": score_ground_truth(
            task,
            final_answer,
            numeric_tolerance=numeric_tolerance,
        ),
        "tool_path": score_tool_path(task, raw_tool_calls),
    }
