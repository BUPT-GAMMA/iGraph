"""Concurrent, headless question benchmark orchestration."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent_protocol import AgentExecution, execute_agent_command, normalize_agent_payload
from .judge_protocol import execute_judge_command
from .masked_dataset import (
    DatasetBinding,
    MaterializedDataset,
    clear_masked_cache,
    infer_ml_task_kind,
    materialize_task_dataset,
    resolve_dataset_binding,
)
from .scoring import score_attempt, validate_tool_path_spec
from .tool_names import (
    GRAPH_ML_ACTIONS,
    expected_tool_path,
    normalize_trace_tool_calls,
)
from .virtual_ml import build_virtual_ml_context
from .virtual_ml_broker import VirtualMLBroker


RESULT_SCHEMA = "igraph.benchmark.results.v3"
TASK_SCHEMA = "igraph.benchmark.agent_task.v1"
CAPTURE_SCHEMA = "igraph.benchmark.question_capture.v1"
JUDGE_TASK_SCHEMA = "igraph.benchmark.judge_task.v1"
_CATEGORY_DATASET_FILENAMES = {
    "ml": "pure_ml_bench_masked_data.pt",
    "prediction": "intermediate_ground_truth_masked_data.pt",
}
_GRAPH_FAMILY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "NAG": {
        "label": "Numerical-Attribute Graph",
        "datasets": ("DGraph-Fin", "pokec", "NBA", "credit", "bail"),
    },
    "TAG": {
        "label": "Text-Attributed Graph",
        "datasets": ("Reddit", "Instagram", "history", "product", "arxiv", "cora"),
    },
    "TPG": {
        "label": "Text-Paired Graph",
        "datasets": ("hiv", "pcba"),
    },
}
_PROBLEM_TASK_SUBTYPES: dict[str, tuple[str, ...]] = {
    "retrieval": ("Node", "Edge", "Graph"),
    "theory": ("Node", "Edge", "Graph"),
    "ml": ("NC", "LP", "GC"),
    "open_qa": ("Insight", "Prediction"),
}
_EXPECTED_MATRIX_CELLS: dict[str, dict[str, tuple[str, ...]]] = {
    "retrieval": {
        family: ("Node", "Edge", "Graph")
        for family in _GRAPH_FAMILY_DEFINITIONS
    },
    "theory": {
        family: ("Node", "Edge", "Graph")
        for family in _GRAPH_FAMILY_DEFINITIONS
    },
    "ml": {
        "NAG": ("NC", "LP"),
        "TAG": ("NC", "LP"),
        "TPG": ("GC",),
    },
    "open_qa": {
        family: ("Insight", "Prediction")
        for family in _GRAPH_FAMILY_DEFINITIONS
    },
}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_SAFE_DATASET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PRINT_LOCK = threading.Lock()
_UNEVALUABLE_FAILURE_ORIGINS = frozenset(
    ("data_preparation", "data_cleanup", "runner_internal")
)
_DATA_FAILURE_ORIGINS = frozenset(
    ("data_preparation", "data_cleanup", "runner_internal")
)

_SAFE_AGENT_ENVIRONMENT_NAMES = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_EXE",
    "CONDA_PYTHON_EXE",
    "PYTHONPATH",
    "LD_LIBRARY_PATH",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


def _reject_nonfinite_replay(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


@dataclass(frozen=True)
class RunnerConfig:
    output_dir: Path
    dataset_root: Path
    questions_root: Path | None = None
    task_parallelism: int = 1
    intra_task_parallelism: int = 1
    attempts_per_question: int = 1
    timeout_seconds: float = 600.0
    numeric_tolerance: float = 1e-6
    virtual_graph_ml: bool = True
    evaluate: bool = False
    agent_environment_names: tuple[str, ...] = ()
    judge_environment_names: tuple[str, ...] = ()
    agent_command: tuple[str, ...] | None = None
    judge_command: tuple[str, ...] | None = None
    judge_timeout_seconds: float = 600.0
    replay_file: Path | None = None
    recover_cleanup_failures: bool = False
    reuse_successful_judge_results: bool = False
    validate_only: bool = False
    selection_metadata: Mapping[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def _task_dict(task: Any) -> dict[str, Any]:
    if hasattr(task, "to_dict") and callable(task.to_dict):
        value = task.to_dict()
    elif is_dataclass(task):
        value = asdict(task)
    elif isinstance(task, Mapping):
        value = dict(task)
    else:
        raise TypeError(f"unsupported question record: {type(task).__name__}")
    if not isinstance(value, dict):
        raise TypeError("question to_dict() must return a dictionary")
    return value


def _taxonomy_dataset_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


_GRAPH_FAMILY_BY_DATASET = {
    _taxonomy_dataset_key(dataset): family
    for family, definition in _GRAPH_FAMILY_DEFINITIONS.items()
    for dataset in definition["datasets"]
}


def _graph_family(dataset: Any) -> str:
    return _GRAPH_FAMILY_BY_DATASET.get(_taxonomy_dataset_key(dataset), "UNKNOWN")


def _problem_group(category: Any) -> str:
    normalized = str(category or "").strip().casefold()
    if normalized in {"retrieval", "theory", "ml"}:
        return normalized
    if normalized in {"insight", "prediction"}:
        return "open_qa"
    return "UNKNOWN"


def _structural_task_subtype(value: Mapping[str, Any]) -> str:
    context = value.get("context", {})
    candidates: list[Any] = [value.get("task_type")]
    if isinstance(context, Mapping):
        candidates.extend(
            context.get(name)
            for name in (
                "task_type",
                "task_level",
                "level",
                "target_type",
                "entity_type",
                "question_type",
                "scope",
            )
        )
    inferred: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", candidate.casefold())
            if token
        }
        if tokens.intersection({"node", "nodes", "vertex", "vertices"}):
            inferred.add("Node")
        if tokens.intersection({"edge", "edges", "link", "links"}):
            inferred.add("Edge")
        if tokens.intersection({"graph", "graphs", "global"}):
            inferred.add("Graph")
    if len(inferred) == 1:
        return next(iter(inferred))
    return "UNKNOWN"


def _task_subtype(
    value: Mapping[str, Any],
    problem: str,
    *,
    allow_reference_hints: bool = True,
) -> str:
    if problem == "open_qa":
        category = str(value.get("category") or "").strip().casefold()
        return {"insight": "Insight", "prediction": "Prediction"}.get(
            category, "UNKNOWN"
        )
    if problem == "ml":
        task_type = _taxonomy_dataset_key(value.get("task_type"))
        aliases = {
            "nc": "NC",
            "nodeclassification": "NC",
            "lp": "LP",
            "linkprediction": "LP",
            "gc": "GC",
            "graphclassification": "GC",
        }
        public_subtype = aliases.get(task_type, "UNKNOWN")
        if not allow_reference_hints:
            return public_subtype
        try:
            return infer_ml_task_kind(value)
        except (TypeError, ValueError) as exc:
            # Compatibility aliases are safe only when the canonical inference
            # found no hint.  Conflicting task/tool/ground-truth hints and an
            # invalid canonical path must remain visibly unclassified.
            if not str(exc).startswith("cannot infer masked task kind"):
                return "UNKNOWN"
            return public_subtype
    if problem in {"retrieval", "theory"}:
        return _structural_task_subtype(value)
    return "UNKNOWN"


def _benchmark_dimensions(
    task: Any, *, allow_reference_hints: bool = True
) -> dict[str, str]:
    value = _task_dict(task)
    problem = _problem_group(value.get("category"))
    return {
        "problem_group": problem,
        "graph_family": _graph_family(value.get("dataset")),
        "task_subtype": _task_subtype(
            value,
            problem,
            allow_reference_hints=allow_reference_hints,
        ),
    }


def _benchmark_type_name(problem: str, family: str, subtype: str) -> str:
    return f"{problem}/{family}/{subtype}"


def benchmark_type_key(task: Any) -> str:
    """Return the authoritative balanced-sampling key for one question.

    Balanced selection is written to the result before agents start, so it may
    use only the public category/dataset/task_type classification.  A second
    full inference pass rejects conflicting hidden reference hints instead of
    allowing selection and final aggregation to disagree.
    """

    public = _benchmark_dimensions(task, allow_reference_hints=False)
    inferred = _benchmark_dimensions(task, allow_reference_hints=True)
    if public != inferred:
        raise ValueError(
            "public task_type does not agree with canonical reference hints: "
            f"public={public}, inferred={inferred}"
        )
    problem = public["problem_group"]
    family = public["graph_family"]
    subtype = public["task_subtype"]
    expected = _EXPECTED_MATRIX_CELLS.get(problem, {}).get(family, ())
    if subtype not in expected:
        raise ValueError(
            "question is outside the formal GABench taxonomy: "
            + _benchmark_type_name(problem, family, subtype)
        )
    return _benchmark_type_name(problem, family, subtype)


def expected_benchmark_type_keys(source: Any) -> tuple[str, ...]:
    """Return formal cells expected from one category/dataset question file."""

    if isinstance(source, Mapping):
        category = source.get("category")
        dataset = source.get("dataset")
    else:
        category = getattr(source, "category", None)
        dataset = getattr(source, "dataset", None)
    problem = _problem_group(category)
    family = _graph_family(dataset)
    if problem == "UNKNOWN" or family == "UNKNOWN":
        raise ValueError(
            "category/dataset is outside the formal GABench taxonomy: "
            f"category={category!r}, dataset={dataset!r}"
        )
    normalized_category = str(category or "").strip().casefold()
    if normalized_category == "insight":
        subtypes = ("Insight",)
    elif normalized_category == "prediction":
        subtypes = ("Prediction",)
    else:
        subtypes = _EXPECTED_MATRIX_CELLS.get(problem, {}).get(family, ())
    if not subtypes:
        raise ValueError(
            f"no formal GABench cells for category={category!r}, dataset={dataset!r}"
        )
    return tuple(
        _benchmark_type_name(problem, family, subtype) for subtype in subtypes
    )


def _strict_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _audit_task(task: Any) -> dict[str, Any]:
    """Return provenance safe to expose while agents still run.

    Even unsalted hashes of low-entropy labels or tool paths are omitted here:
    another concurrent attempt could enumerate them from a checkpoint. Raw GT
    and its integrity hash are written only after every agent has exited.
    """

    value = _task_dict(task)
    return {
        "composite_id": _composite_id(task),
        "id": value.get("id"),
        "question": value.get("question"),
        "category": value.get("category"),
        "dataset": value.get("dataset"),
        "task_type": value.get("task_type"),
        **_benchmark_dimensions(value, allow_reference_hints=False),
    }


def _source_provenance(task: Any) -> dict[str, Any]:
    """Return source pointers only after all agent processes have exited."""

    value = _task_dict(task)
    return {
        "source_file": value.get("source_file"),
        "source_index": value.get("source_index"),
    }


def _task_value(task: Any, name: str, default: Any = None) -> Any:
    if isinstance(task, Mapping):
        return task.get(name, default)
    return getattr(task, name, default)


def _composite_id(task: Any) -> str:
    value = _task_value(task, "composite_id")
    if value:
        return str(value)
    return "/".join(
        str(_task_value(task, field, "unknown"))
        for field in ("category", "dataset", "id")
    )


def _safe_task_component(composite_id: str) -> str:
    readable = _SAFE_COMPONENT.sub("-", composite_id).strip("-.")[:80] or "task"
    digest = hashlib.sha256(composite_id.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _validate_config(config: RunnerConfig) -> None:
    for name in (
        "task_parallelism",
        "intra_task_parallelism",
        "attempts_per_question",
    ):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name.replace('_', '-')} must be a positive integer")
    maximum_concurrent = config.task_parallelism * min(
        config.intra_task_parallelism,
        config.attempts_per_question,
    )
    if maximum_concurrent > 256:
        raise ValueError("maximum concurrent agent attempts cannot exceed 256")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("timeout-seconds must be a positive finite number")
    if not math.isfinite(config.numeric_tolerance) or config.numeric_tolerance < 0:
        raise ValueError("numeric-tolerance must be a non-negative finite number")
    if (
        not math.isfinite(config.judge_timeout_seconds)
        or config.judge_timeout_seconds <= 0
    ):
        raise ValueError("judge-timeout-seconds must be a positive finite number")
    modes = sum(
        (
            bool(config.agent_command),
            config.replay_file is not None,
            config.validate_only,
        )
    )
    if modes != 1:
        raise ValueError(
            "choose exactly one execution mode: agent-command, replay-file, or validate-only"
        )
    if config.validate_only and config.evaluate:
        raise ValueError("validate-only cannot be combined with evaluate")
    if config.evaluate and not config.judge_command:
        raise ValueError("evaluation requires judge-command")
    if config.judge_command and not config.evaluate:
        raise ValueError("judge-command requires evaluation to be enabled")
    if not isinstance(config.recover_cleanup_failures, bool):
        raise ValueError("recover-cleanup-failures must be a boolean")
    if config.recover_cleanup_failures and config.replay_file is None:
        raise ValueError("recover-cleanup-failures requires replay-file")
    if not isinstance(config.reuse_successful_judge_results, bool):
        raise ValueError("reuse-successful-judge-results must be a boolean")
    if config.reuse_successful_judge_results and (
        config.replay_file is None or not config.evaluate or not config.judge_command
    ):
        raise ValueError(
            "reuse-successful-judge-results requires replay-file, evaluation, "
            "and judge-command"
        )
    if config.selection_metadata is not None:
        if not isinstance(config.selection_metadata, Mapping):
            raise ValueError("selection-metadata must be a mapping")
        try:
            json.dumps(config.selection_metadata, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("selection-metadata must be strict JSON data") from exc
    for field_name, names in (
        ("agent-environment-names", config.agent_environment_names),
        ("judge-environment-names", config.judge_environment_names),
    ):
        if not isinstance(names, tuple):
            raise ValueError(f"{field_name} must be a tuple")
        for name in names:
            if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            if name not in os.environ:
                raise ValueError(
                    f"requested environment variable is not set: {name}"
                )


def _base_agent_environment(config: RunnerConfig) -> dict[str, str]:
    names = set(_SAFE_AGENT_ENVIRONMENT_NAMES)
    names.update(config.agent_environment_names)
    return {name: os.environ[name] for name in names if name in os.environ}


def _base_judge_environment(config: RunnerConfig) -> dict[str, str]:
    names = set(_SAFE_AGENT_ENVIRONMENT_NAMES)
    names.update(config.judge_environment_names)
    return {name: os.environ[name] for name in names if name in os.environ}


@dataclass(frozen=True)
class _ReplayJudgeResult:
    score: int
    input_location: str
    original_source: str | None = None
    original_duration_seconds: float | None = None
    protocol: str | None = None


def _strict_binary_judge_score(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


def _complete_judge_decision(value: Any) -> bool:
    if not isinstance(value, Mapping) or not _strict_binary_judge_score(
        value.get("score")
    ):
        return False
    if value.get("status") == "success":
        return True
    return (
        value.get("status") == "not_called"
        and value.get("source") == "agent_execution_failure"
        and value.get("score") == 0
    )


def _successful_replay_judge(
    value: Any,
    *,
    input_location: str,
) -> _ReplayJudgeResult | None:
    """Return a reusable Judge decision only for a strict completed result."""

    if not isinstance(value, Mapping) or value.get("status") != "success":
        return None
    score = value.get("score")
    if not _strict_binary_judge_score(score) or value.get("error") not in (
        None,
        "",
        {},
    ):
        return None
    duration = value.get("duration_seconds")
    normalized_duration = (
        float(duration)
        if not isinstance(duration, bool)
        and isinstance(duration, (int, float))
        and math.isfinite(float(duration))
        and float(duration) >= 0
        else None
    )
    source = value.get("source")
    protocol = value.get("protocol")
    return _ReplayJudgeResult(
        score=int(score),
        input_location=input_location,
        original_source=(
            str(source) if isinstance(source, str) and source.strip() else None
        ),
        original_duration_seconds=normalized_duration,
        protocol=(
            str(protocol)
            if isinstance(protocol, str) and protocol.strip()
            else None
        ),
    )


@dataclass(frozen=True)
class _ReplayRecord:
    """One replayed agent result plus an optional captured canonical trace.

    ``agent_payload`` retains the historical replay contract exposed by
    :meth:`ReplayStore.resolve`.  ``trajectory_calls`` is kept separately so
    an authoritative trace captured by a previous benchmark run is not merged
    with, or reconstructed from, the agent transcript.
    """

    agent_payload: Any
    trajectory_calls: tuple[Any, ...] | None = None
    successful_judge: _ReplayJudgeResult | None = None


class ReplayStore:
    """Read deterministic agent outputs without invoking an LLM."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(f"replay file not found: {self.path}")
        raw = self.path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw, parse_constant=_reject_nonfinite_replay)
        except json.JSONDecodeError:
            data = [
                json.loads(line, parse_constant=_reject_nonfinite_replay)
                for line in raw.splitlines()
                if line.strip()
            ]
        self._mapping: dict[str, _ReplayRecord | list[_ReplayRecord]] = {}
        self._ordered: list[_ReplayRecord] = []
        self._ingest(data)

    @staticmethod
    def _record_from_payload(
        payload: Any,
        *,
        trajectory: Any = None,
        successful_judge: _ReplayJudgeResult | None = None,
    ) -> _ReplayRecord:
        calls: tuple[Any, ...] | None = None
        if isinstance(trajectory, Mapping):
            raw_calls = trajectory.get("calls")
            if isinstance(raw_calls, list):
                # An explicitly captured empty list is authoritative too: it
                # must override tool calls reconstructed from the transcript.
                calls = tuple(raw_calls)
        return _ReplayRecord(
            agent_payload=payload,
            trajectory_calls=calls,
            successful_judge=successful_judge,
        )

    def _ingest(self, data: Any) -> None:
        if isinstance(data, Mapping):
            if isinstance(data.get("questions"), list):
                for question in data["questions"]:
                    if not isinstance(question, Mapping):
                        continue
                    question_value = question.get("question", {})
                    composite_id = (
                        question_value.get("composite_id")
                        if isinstance(question_value, Mapping)
                        else None
                    )
                    for attempt in question.get("attempts", []):
                        if not isinstance(attempt, Mapping):
                            continue
                        payload = attempt.get("agent")
                        trajectory = attempt.get("trajectory")
                        judge_candidate = _successful_replay_judge(
                            attempt.get("judge"),
                            input_location="result.attempt.judge",
                        )
                        artifact = attempt.get("attempt_result_file")
                        artifact_path = Path(artifact) if isinstance(artifact, str) else None
                        if artifact_path is not None and not artifact_path.is_absolute():
                            artifact_path = (self.path.parent / artifact_path).resolve()
                        if artifact_path is not None and artifact_path.is_file():
                            detail = json.loads(
                                artifact_path.read_text(encoding="utf-8"),
                                parse_constant=_reject_nonfinite_replay,
                            )
                            payload = detail.get("agent", payload)
                            trajectory = detail.get("trajectory", trajectory)
                            evaluation = detail.get("evaluation")
                            # Once the artifact supplies the agent payload, its
                            # Judge is the only result that can be paired with
                            # that payload.  Never mix a root summary decision
                            # with a different physical attempt artifact.
                            judge_candidate = (
                                _successful_replay_judge(
                                    evaluation.get("judge"),
                                    input_location=(
                                        "attempt_artifact.evaluation.judge"
                                    ),
                                )
                                if isinstance(evaluation, Mapping)
                                else None
                            )
                        if (
                            composite_id
                            and isinstance(payload, Mapping)
                            and any(
                                key in payload
                                for key in (
                                    "final_answer",
                                    "answer",
                                    "response",
                                    "output",
                                    "transcript",
                                    "messages",
                                )
                            )
                        ):
                            self._add_record(
                                {
                                    "composite_id": composite_id,
                                    "attempt": attempt.get("attempt", 1),
                                    "agent": payload,
                                    "trajectory": trajectory,
                                },
                                successful_judge=judge_candidate,
                            )
                return
            for key in ("responses", "results", "attempts"):
                if key in data:
                    self._ingest(data[key])
                    return
            if any(key in data for key in ("question_id", "composite_id", "task_id")):
                self._add_record(data)
                return
            for key, value in data.items():
                if isinstance(value, list):
                    # Preserve the legacy mapping form in which a list holds
                    # one payload per attempt for the mapped question id.
                    self._mapping[str(key)] = [
                        self._record_from_payload(item) for item in value
                    ]
                else:
                    self._mapping[str(key)] = self._record_from_payload(value)
            return
        if isinstance(data, list):
            for value in data:
                if isinstance(value, Mapping) and any(
                    key in value for key in ("question_id", "composite_id", "task_id")
                ):
                    self._add_record(value)
                else:
                    self._ordered.append(self._record_from_payload(value))
            return
        raise ValueError("replay file must contain an object, array, or JSONL records")

    def _add_record(
        self,
        record: Mapping[str, Any],
        *,
        successful_judge: _ReplayJudgeResult | None = None,
    ) -> None:
        key = str(
            record.get("composite_id", record.get("question_id", record.get("task_id")))
        )
        payload = record.get("agent", record.get("response", record))
        if successful_judge is None:
            evaluation = record.get("evaluation")
            if isinstance(evaluation, Mapping) and "judge" in evaluation:
                successful_judge = _successful_replay_judge(
                    evaluation.get("judge"),
                    input_location="record.evaluation.judge",
                )
            else:
                successful_judge = _successful_replay_judge(
                    record.get("judge"),
                    input_location="record.judge",
                )
        replay_record = self._record_from_payload(
            payload,
            trajectory=record.get("trajectory"),
            successful_judge=successful_judge,
        )
        attempt = record.get("attempt")
        if attempt is None:
            existing = self._mapping.get(key)
            if existing is None:
                self._mapping[key] = replay_record
            elif isinstance(existing, list):
                existing.append(replay_record)
            else:
                self._mapping[key] = [existing, replay_record]
        else:
            self._mapping[f"{key}#{int(attempt)}"] = replay_record

    def _resolve_record(
        self,
        task: Any,
        attempt: int,
        flat_index: int,
    ) -> _ReplayRecord:
        composite_id = _composite_id(task)
        raw_id = str(_task_value(task, "id", ""))
        for key in (
            f"{composite_id}#{attempt}",
            f"{raw_id}#{attempt}" if raw_id else "",
        ):
            if not key or key not in self._mapping:
                continue
            value = self._mapping[key]
            if isinstance(value, list):
                raise ValueError(f"replay attempt key has multiple values: {key}")
            return value
        for key in (composite_id, raw_id):
            if not key or key not in self._mapping:
                continue
            value = self._mapping[key]
            if isinstance(value, list):
                if attempt - 1 >= len(value):
                    raise KeyError(f"replay has no attempt {attempt} for {composite_id}")
                return value[attempt - 1]
            if attempt != 1:
                raise KeyError(f"replay has only one attempt for {composite_id}")
            return value
        if flat_index < len(self._ordered):
            return self._ordered[flat_index]
        raise KeyError(f"no replay response for {composite_id} attempt {attempt}")

    def resolve(
        self,
        task: Any,
        attempt: int,
        flat_index: int,
    ) -> Any:
        """Resolve the historical agent-payload-only replay interface."""

        return self._resolve_record(task, attempt, flat_index).agent_payload


def _nonempty_replay_answer(value: Any) -> bool:
    """Return whether a captured answer contains an intentional value.

    Numeric zero and ``False`` are valid benchmark answers.  Empty textual or
    container values are not sufficient evidence to recover a failed run.
    """

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return bool(value)
    return True


def _container_cleanup_error(error: Any) -> dict[str, str] | None:
    """Recognize current and legacy structured ContainerCleanupError values.

    Older agent normalization wrapped an error mapping as ``type=dict`` and a
    Python dictionary literal in ``message``.  Parse only that literal with
    ``ast.literal_eval``; never recover based on a substring match.
    """

    if not isinstance(error, Mapping):
        return None
    candidate: Any = error
    if candidate.get("type") == "dict":
        legacy_message = candidate.get("message")
        if not isinstance(legacy_message, str) or len(legacy_message) > 16_384:
            return None
        try:
            candidate = ast.literal_eval(legacy_message)
        except (SyntaxError, ValueError, TypeError, RecursionError):
            return None
        if not isinstance(candidate, Mapping):
            return None
    if candidate.get("type") != "ContainerCleanupError":
        return None
    message = candidate.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    return {
        "type": "ContainerCleanupError",
        "message": message,
    }


def _recover_replayed_cleanup_failure(
    execution: AgentExecution,
    original_payload: Any = None,
) -> dict[str, Any] | None:
    """Recover a completed answer invalidated only by container cleanup.

    Recovery is deliberately narrow and is called only behind the explicit
    replay option.  The original error remains auditable in the returned
    provenance and as a structured warning.
    """

    cleanup_error = _container_cleanup_error(execution.error)
    recorded_execution_error = (
        original_payload.get("execution_error")
        if isinstance(original_payload, Mapping)
        else None
    )
    if (
        execution.status != "failed"
        or cleanup_error is None
        or recorded_execution_error not in (None, "")
        or not _nonempty_replay_answer(execution.final_answer)
        or not execution.transcript
    ):
        return None
    original_status = execution.status
    original_error = dict(cleanup_error)
    execution.status = "success"
    execution.error = None
    execution.warnings.append(
        {
            "type": "RecoveredContainerCleanupError",
            "message": cleanup_error["message"],
        }
    )
    return {
        "applied": True,
        "reason": "container_cleanup_after_captured_answer",
        "original_status": original_status,
        "original_error": original_error,
    }


def _dataset_path(config: RunnerConfig, task: Any) -> Path:
    dataset = str(_task_value(task, "dataset", "") or "").strip()
    category = str(_task_value(task, "category", "") or "").strip()
    if not dataset:
        raise ValueError(f"question {_composite_id(task)} has no dataset")
    if not _SAFE_DATASET.fullmatch(dataset) or dataset in {".", ".."}:
        raise ValueError(
            f"question {_composite_id(task)} has an unsafe dataset name: {dataset!r}"
        )
    masked_filename = _CATEGORY_DATASET_FILENAMES.get(category)
    source_file = _task_value(task, "source_file")
    if masked_filename and source_file:
        source_path = Path(str(source_file)).expanduser().resolve()
        if source_path.name != "questions.json":
            raise ValueError(
                f"question source must end with questions.json: {source_path}"
            )
        if source_path.parent.name != dataset or source_path.parent.parent.name != category:
            raise ValueError(
                "question source scope does not match its category/dataset: "
                f"{source_path}"
            )
        if config.questions_root is not None:
            questions_root = config.questions_root.expanduser().resolve()
            if not source_path.is_relative_to(questions_root):
                raise ValueError(
                    f"question source escapes questions root: {source_path}"
                )
        dataset_parent = source_path.parent
        path = (dataset_parent / masked_filename).resolve()
        if not path.is_relative_to(dataset_parent):
            raise ValueError(f"dataset path escapes question directory: {path}")
    elif masked_filename and config.questions_root is not None:
        questions_root = config.questions_root.expanduser().resolve()
        dataset_parent = (questions_root / category / dataset).resolve()
        if not dataset_parent.is_relative_to(questions_root):
            raise ValueError(f"dataset path escapes questions root: {dataset_parent}")
        path = (dataset_parent / masked_filename).resolve()
    elif masked_filename:
        raise ValueError(
            f"{category} questions require questions-root or source-file "
            "provenance for category-local masked dataset routing"
        )
    else:
        dataset_root = config.dataset_root.expanduser().resolve()
        path = (dataset_root / f"{dataset}.pt").resolve()
        if not path.is_relative_to(dataset_root):
            raise ValueError(f"dataset path escapes dataset root: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"dataset file not found: {path}")
    return path


def _dataset_binding(config: RunnerConfig, task: Any) -> DatasetBinding:
    routed_path = _dataset_path(config, task)
    category = str(_task_value(task, "category", "") or "").strip()
    dataset = str(_task_value(task, "dataset", "") or "").strip()
    masked_filename = _CATEGORY_DATASET_FILENAMES.get(category)
    if masked_filename is not None and routed_path.name == masked_filename:
        return resolve_dataset_binding(
            task,
            routed_path=routed_path,
            dataset_root=config.dataset_root,
        )
    binding = DatasetBinding(
        base_path=routed_path,
        category=category or None,
        dataset=dataset or None,
    )
    binding.validate_files()
    return binding


def _public_task(
    task: Any,
    dataset_path: Path,
    attempt: int,
    intra_task_parallelism: int,
    *,
    materialized_dataset: MaterializedDataset | None = None,
) -> dict[str, Any]:
    value = _task_dict(task)
    context = value.get("context", {})
    public_context = dict(context) if isinstance(context, Mapping) else {}
    public_context.pop("accepted_tool_paths", None)
    # This corpus bookkeeping index points directly into the masked answer
    # mapping.  The already-materialized input is sufficient for the agent.
    public_context.pop("task_index", None)
    return {
        "schema": TASK_SCHEMA,
        "question_id": _composite_id(task),
        "id": value.get("id"),
        "category": value.get("category"),
        "dataset": value.get("dataset"),
        "task_type": value.get("task_type"),
        "question": value.get("question"),
        "context": public_context,
        "dataset_path": str(dataset_path),
        "dataset_input": {
            "mode": (
                "per_attempt_masked_materialization"
                if materialized_dataset is not None
                else "direct"
            ),
            "task_kind": (
                materialized_dataset.task_kind
                if materialized_dataset is not None
                else None
            ),
        },
        "attempt": attempt,
        "execution": {
            "intra_task_parallelism": intra_task_parallelism,
        },
    }


def _link_dataset(workspace: Path, dataset_path: Path) -> Path:
    linked = workspace / dataset_path.name
    if linked.exists() or linked.is_symlink():
        return linked
    try:
        linked.symlink_to(dataset_path)
    except OSError as exc:
        raise OSError(
            f"unable to link dataset into attempt workspace: {dataset_path}"
        ) from exc
    return linked


def _failed_execution(exc: BaseException, duration: float = 0.0) -> AgentExecution:
    return AgentExecution(
        status="failed",
        duration_seconds=duration,
        error={"type": type(exc).__name__, "message": str(exc)},
    )


def _preflight_tasks(
    tasks: Sequence[Any],
    config: RunnerConfig,
) -> list[DatasetBinding]:
    """Validate all inputs before the first potentially costly agent call."""

    seen: set[str] = set()
    binding_cache: dict[tuple[str, str, str], DatasetBinding] = {}
    bindings: list[DatasetBinding] = []
    for task in tasks:
        composite_id = _composite_id(task)
        if composite_id in seen:
            raise ValueError(f"duplicate benchmark question id: {composite_id}")
        seen.add(composite_id)
        binding_key = (
            str(_task_value(task, "category", "") or ""),
            str(_task_value(task, "dataset", "") or ""),
            str(_task_value(task, "source_file", "") or ""),
        )
        binding = binding_cache.get(binding_key)
        if binding is None:
            binding = _dataset_binding(config, task)
            binding_cache[binding_key] = binding
        bindings.append(binding)
        # Canonicalize every reference path before an agent can consume API
        # tokens. This is validation, not evaluation, and is safe in capture
        # mode where the scorers must never be called.
        validate_tool_path_spec(task)
        if config.virtual_graph_ml and _requires_virtual_ml(task):
            build_virtual_ml_context(task)
    return bindings


def _requires_virtual_ml(task: Any) -> bool:
    task_type = str(_task_value(task, "task_type", "") or "").upper()
    if task_type in {"NC", "LP", "GC"}:
        return True
    return any(name in GRAPH_ML_ACTIONS for name in expected_tool_path(task))


_SENSITIVE_COMMAND_OPTIONS = frozenset(
    {
        "--api-key",
        "--apikey",
        "--access-token",
        "--authorization",
        "--bearer",
        "--client-secret",
        "--credential",
        "--credentials",
        "--password",
        "--provider-profile",
        "--refresh-token",
        "--secret",
        "--secret-key",
        "--token",
    }
)


def _redacted_command(command: Sequence[str] | None) -> list[str] | None:
    if not command:
        return None
    redacted: list[str] = []
    hide_next = False
    for part in command:
        option, separator, _value = part.partition("=")
        normalized_option = option.casefold().replace("_", "-")
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
        elif normalized_option in _SENSITIVE_COMMAND_OPTIONS:
            if separator:
                redacted.append(option + "=<redacted>")
            else:
                redacted.append(part)
                hide_next = True
        else:
            redacted.append(part)
    return redacted


def _merge_authoritative_ml_trace(
    reported_calls: Sequence[Any],
    broker_calls: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Replace self-reported ML entries with the broker-observed sequence."""

    normalized = normalize_trace_tool_calls(reported_calls)
    broker_values = [dict(call) for call in broker_calls]
    merged: list[dict[str, Any]] = []
    broker_index = 0
    for call in normalized.calls:
        value = dict(call)
        if value.get("canonical_name") in GRAPH_ML_ACTIONS:
            # Replace self-reported ML observations one-for-one so a mixed
            # train -> non-ML -> infer trajectory keeps its real interleave.
            # Extra self-reported ML calls are intentionally discarded: only
            # broker-observed ML calls are authoritative.
            if broker_index < len(broker_values):
                merged.append(broker_values[broker_index])
                broker_index += 1
            continue
        merged.append(value)

    # When an adapter omits some/all ML calls from its own transcript there is
    # no shared clock with which to interleave the remaining broker calls.
    # Append them in broker order. This is deterministic and, importantly,
    # never moves a reported non-ML observation across another call.
    remaining = broker_values[broker_index:]
    if remaining:
        merged.extend(remaining)
    for index, call in enumerate(merged):
        call["index"] = index
    return merged


def _run_attempt(
    task: Any,
    task_index: int,
    attempt: int,
    config: RunnerConfig,
    run_dir: Path,
    replay_store: ReplayStore | None,
    *,
    prepared_dataset: MaterializedDataset | None = None,
    dataset_binding: DatasetBinding | None = None,
) -> dict[str, Any]:
    composite_id = _composite_id(task)
    component = _safe_task_component(composite_id)
    attempt_dir = run_dir / "workspaces" / component / f"attempt-{attempt:03d}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    started_at = _utc_now()
    started_clock = time.perf_counter()
    authoritative_ml_trace: list[dict[str, Any]] = []
    authoritative_ml_enabled = False
    captured_replay_trace: list[Any] | None = None
    replay_recovery: dict[str, Any] | None = None
    replay_judge_candidate: dict[str, Any] | None = None
    dataset_path: Path | None = None
    active_binding: DatasetBinding | None = None
    active_prepared = prepared_dataset
    ephemeral_prepared: MaterializedDataset | None = None
    failure_origin: str | None = None
    cleanup_error: dict[str, str] | None = None
    setup_complete = False

    try:
        active_binding = dataset_binding or _dataset_binding(config, task)
        if (
            active_prepared is None
            and config.agent_command is not None
            and active_binding.requires_materialization
        ):
            # Each concurrent attempt receives a different writable inode. The
            # enormous masked mapping is still mmap-cached process-wide, while
            # agent writes cannot contaminate a sibling attempt.
            active_prepared = materialize_task_dataset(
                active_binding,
                task,
                attempt_dir / active_binding.base_path.name,
            )
            ephemeral_prepared = active_prepared
        dataset_path = (
            active_prepared.path
            if active_prepared is not None
            else active_binding.masked_path or active_binding.base_path
        )
        linked_dataset = _link_dataset(attempt_dir, dataset_path)
        envelope = _public_task(
            task,
            linked_dataset,
            attempt,
            config.intra_task_parallelism,
            materialized_dataset=active_prepared,
        )
        task_path = attempt_dir / "task.json"
        _atomic_write_json(task_path, envelope)

        agent_environment = {
            **_base_agent_environment(config),
            "IGRAPH_BENCHMARK_TASK_FILE": str(task_path),
            "IGRAPH_DATASET_PATH": str(linked_dataset),
            "IGRAPH_ATTEMPT_PARALLELISM": str(config.intra_task_parallelism),
            "IGRAPH_INTRA_TASK_PARALLELISM": str(config.intra_task_parallelism),
        }
        setup_complete = True
        virtual_ml_for_attempt = bool(
            config.virtual_graph_ml and _requires_virtual_ml(task)
        )
        if virtual_ml_for_attempt:
            agent_environment["IGRAPH_GRAPH_ML_TOOL"] = str(
                Path(__file__).resolve().parents[1]
                / "graph_analysis_skills"
                / "graph_ml_tools.py"
            )

        if replay_store is not None:
            flat_index = task_index * config.attempts_per_question + (attempt - 1)
            replay_record = replay_store._resolve_record(task, attempt, flat_index)
            execution = normalize_agent_payload(replay_record.agent_payload)
            if replay_record.trajectory_calls is not None:
                captured_replay_trace = list(replay_record.trajectory_calls)
            if config.recover_cleanup_failures:
                replay_recovery = _recover_replayed_cleanup_failure(
                    execution,
                    replay_record.agent_payload,
                )
            if (
                config.reuse_successful_judge_results
                and replay_record.successful_judge is not None
            ):
                replay_judge_candidate = asdict(replay_record.successful_judge)
            execution.duration_seconds = time.perf_counter() - started_clock
        elif virtual_ml_for_attempt:
            assert config.agent_command is not None
            virtual_task = _task_dict(task)
            virtual_task["attempt_id"] = f"{composite_id}#{attempt}"
            authoritative_ml_enabled = True
            broker = VirtualMLBroker(virtual_task)
            try:
                with broker:
                    broker_environment = broker.build_environment(agent_environment)
                    execution = execute_agent_command(
                        config.agent_command,
                        envelope,
                        cwd=attempt_dir,
                        environment=broker_environment,
                        timeout_seconds=config.timeout_seconds,
                        inherit_environment=False,
                    )
            finally:
                # __exit__ drains in-flight broker requests before this final
                # authoritative snapshot is taken.
                authoritative_ml_trace = [dict(call) for call in broker.trace]
        else:
            assert config.agent_command is not None
            execution = execute_agent_command(
                config.agent_command,
                envelope,
                cwd=attempt_dir,
                environment=agent_environment,
                timeout_seconds=config.timeout_seconds,
                inherit_environment=False,
            )
    except Exception as exc:
        failure_origin = "agent_execution" if setup_complete else "data_preparation"
        execution = _failed_execution(exc, time.perf_counter() - started_clock)
    finally:
        if ephemeral_prepared is not None:
            try:
                ephemeral_prepared.path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                if failure_origin is None:
                    failure_origin = "data_cleanup"

    if captured_replay_trace is not None:
        # A prior run may have combined transcript-reported calls with an
        # authoritative virtual-ML broker trace.  Replay that finished trace
        # verbatim instead of rebuilding it from the transcript, which could
        # lose broker-only calls or duplicate self-reported ones.
        authoritative_trace = captured_replay_trace
    elif authoritative_ml_enabled:
        authoritative_trace = _merge_authoritative_ml_trace(
            execution.tool_calls,
            authoritative_ml_trace,
        )
    else:
        authoritative_trace = list(execution.tool_calls)
    trajectory = normalize_trace_tool_calls(authoritative_trace).to_dict()
    attempt_result_path = (
        run_dir / "attempts" / component / f"attempt-{attempt:03d}.json"
    )
    result = {
        "question_id": composite_id,
        "attempt": attempt,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_seconds": execution.duration_seconds,
        "dataset_path": str(dataset_path) if dataset_path is not None else None,
        "dataset_input": {
            "mode": (
                "per_attempt_masked_materialization"
                if active_prepared is not None
                else "direct"
            ),
            "masked_index": None,
            "task_kind": (
                active_prepared.task_kind
                if active_prepared is not None
                else None
            ),
            "applied_field": (
                active_prepared.applied_field
                if active_prepared is not None
                else None
            ),
            # Source paths are attached only after the complete agent executor
            # has stopped; otherwise sibling agents could discover GT files via
            # the live result checkpoint or an already-finished attempt file.
            "base_path": None,
            "masked_path": None,
            "provenance_deferred": active_binding is not None,
            "ephemeral": active_prepared is not None,
            "removed_after_attempt": (
                ephemeral_prepared is not None and cleanup_error is None
            ),
            "masked_cache_hit": (
                active_prepared.masked_cache_hit
                if active_prepared is not None
                else None
            ),
        },
        "failure_origin": failure_origin,
        "cleanup_error": cleanup_error,
        "replay_recovery": replay_recovery,
        "virtual_graph_ml": bool(config.virtual_graph_ml),
        "virtual_graph_ml_broker_used": authoritative_ml_enabled,
        "tool_trace_provenance": {
            "reported_by_agent": len(execution.tool_calls),
            "authoritative_virtual_ml": authoritative_ml_trace,
            "trajectory_uses_authoritative_virtual_ml": authoritative_ml_enabled,
            "captured_replay_trajectory_used": captured_replay_trace is not None,
            "captured_replay_call_count": (
                len(captured_replay_trace)
                if captured_replay_trace is not None
                else 0
            ),
            "trajectory_source": (
                "captured_replay"
                if captured_replay_trace is not None
                else "authoritative_virtual_ml"
                if authoritative_ml_enabled
                else "agent_reported"
            ),
            "scoring_uses_authoritative_virtual_ml": (
                authoritative_ml_enabled if config.evaluate else False
            ),
            "scoring_uses_captured_replay_trajectory": (
                captured_replay_trace is not None and config.evaluate
            ),
        },
        "attempt_result_file": str(attempt_result_path),
        "agent": execution.to_dict(),
        "trajectory": trajectory,
    }
    _atomic_write_json(attempt_result_path, result)
    summary = {
        "question_id": composite_id,
        "attempt": attempt,
        "duration_seconds": execution.duration_seconds,
        "attempt_result_file": str(attempt_result_path),
        "agent": {
            "status": execution.status,
            "error": execution.error,
            "warnings": list(execution.warnings),
        },
        "failure_origin": failure_origin,
        "replay_recovery": replay_recovery,
        "trajectory": {
            "strict_path": trajectory["strict_path"],
            "successful_path": trajectory["successful_path"],
            "call_count": len(trajectory["calls"]),
        },
    }
    if replay_judge_candidate is not None:
        # This transient field is removed by _evaluate_attempt before the
        # finalized result is written.  Keeping it out of the detailed agent
        # artifact avoids presenting an unconsumed candidate as a new score.
        summary["_replay_judge_candidate"] = replay_judge_candidate
    return summary


def _run_question(
    task: Any,
    task_index: int,
    config: RunnerConfig,
    run_dir: Path,
    replay_store: ReplayStore | None,
    dataset_binding: DatasetBinding | None = None,
) -> dict[str, Any]:
    composite_id = _composite_id(task)
    with _PRINT_LOCK:
        print(
            f"[benchmark] question {task_index + 1}: {composite_id}",
            file=sys.stderr,
        )
    workers = min(config.intra_task_parallelism, config.attempts_per_question)
    attempts: list[dict[str, Any]] = []
    binding = dataset_binding or _dataset_binding(config, task)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {}
        for attempt in range(1, config.attempts_per_question + 1):
            if config.agent_command is not None and binding.requires_materialization:
                future = pool.submit(
                    _run_attempt,
                    task,
                    task_index,
                    attempt,
                    config,
                    run_dir,
                    replay_store,
                    dataset_binding=binding,
                )
            else:
                future = pool.submit(
                    _run_attempt,
                    task,
                    task_index,
                    attempt,
                    config,
                    run_dir,
                    replay_store,
                )
            future_map[future] = attempt
        for future in as_completed(future_map):
            attempt = future_map[future]
            try:
                attempts.append(future.result())
            except Exception as exc:  # last-resort isolation between attempts
                attempts.append(
                    {
                        "question_id": composite_id,
                        "attempt": attempt,
                        "agent": _failed_execution(exc).to_dict(),
                        "failure_origin": "runner_internal",
                        "trajectory": {
                            "strict_path": [],
                            "successful_path": [],
                            "call_count": 0,
                        },
                    }
                )
    attempts.sort(key=lambda item: int(item["attempt"]))
    successful_attempts = [
        attempt
        for attempt in attempts
        if attempt.get("agent", {}).get("status") == "success"
    ]
    selected = successful_attempts[0] if successful_attempts else None
    return {
        "question": _audit_task(task),
        "attempts": attempts,
        # Selection is deterministic and never uses the reference answer or a
        # judge score. This avoids pass@K/oracle selection leaking into TSA/SR.
        "selected_attempt": (
            int(selected["attempt"]) if selected is not None else None
        ),
        "best_attempt": int(selected["attempt"]) if selected is not None else None,
        "execution_success": bool(successful_attempts),
    }


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _attempt_detail(
    task: Any,
    attempt: dict[str, Any],
    run_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    raw_path = attempt.get("attempt_result_file")
    if raw_path:
        path = Path(str(raw_path)).expanduser().resolve()
        if not path.is_relative_to(run_dir):
            raise ValueError(f"attempt result escapes run directory: {path}")
        if path.is_file():
            value = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite_replay,
            )
            if not isinstance(value, dict):
                raise ValueError(f"attempt result must be a JSON object: {path}")
            return path, value
    component = _safe_task_component(_composite_id(task))
    attempt_number = int(attempt.get("attempt", 1))
    path = run_dir / "attempts" / component / f"attempt-{attempt_number:03d}.json"
    value = {
        "question_id": _composite_id(task),
        "attempt": attempt_number,
        "attempt_result_file": str(path),
        "agent": dict(attempt.get("agent", {})),
        "trajectory": {
            "calls": [],
            "strict_path": list(
                attempt.get("trajectory", {}).get("strict_path", [])
            ),
            "successful_path": list(
                attempt.get("trajectory", {}).get("successful_path", [])
            ),
        },
    }
    attempt["attempt_result_file"] = str(path)
    return path, value


def _attach_final_provenance(
    tasks: Sequence[Any],
    question_results: Sequence[dict[str, Any]],
    bindings: Sequence[DatasetBinding],
    run_dir: Path,
) -> None:
    """Publish source paths only after every agent subprocess has stopped."""

    for task, question_result, binding in zip(
        tasks, question_results, bindings, strict=True
    ):
        source = _source_provenance(task)
        question_audit = question_result.get("question")
        if isinstance(question_audit, dict):
            question_audit.update(source)
            question_audit.update(
                _benchmark_dimensions(task, allow_reference_hints=True)
            )
            question_audit["benchmark_dimensions_finalized"] = True
        context = _task_value(task, "context", {})
        masked_index = source.get("source_index")
        if masked_index is None and isinstance(context, Mapping):
            masked_index = context.get("task_index")
        for attempt in question_result.get("attempts", []):
            detail_path, detail = _attempt_detail(task, attempt, run_dir)
            dataset_input = detail.get("dataset_input")
            if not isinstance(dataset_input, dict):
                dataset_input = {}
                detail["dataset_input"] = dataset_input
            dataset_input.update(
                {
                    "base_path": str(binding.base_path),
                    "masked_path": (
                        str(binding.masked_path)
                        if binding.masked_path is not None
                        else None
                    ),
                    "masked_index": (
                        masked_index if binding.masked_path is not None else None
                    ),
                    "provenance_deferred": False,
                }
            )
            _atomic_write_json(detail_path, detail)


def _judge_reference_answer(task: Any) -> Any:
    ground_truth = _task_value(task, "ground_truth")
    if isinstance(ground_truth, Mapping) and "answer" in ground_truth:
        return ground_truth["answer"]
    return ground_truth


def _judge_envelope(task: Any, final_answer: Any) -> dict[str, Any]:
    return {
        "schema": JUDGE_TASK_SCHEMA,
        "question_id": _composite_id(task),
        "original_question": _task_value(task, "question"),
        "ground_truth": _judge_reference_answer(task),
        "final_answer": final_answer,
        "rubric": (
            "Return binary score 1 only when the final response is semantically "
            "consistent with the ground truth and completes the requested task; "
            "otherwise return 0. Output one JSON object with numeric field score."
        ),
    }


def _reused_judge_execution(candidate: Any) -> dict[str, Any] | None:
    """Build a fresh-run Judge record from a validated replay candidate."""

    if not isinstance(candidate, Mapping):
        return None
    score = candidate.get("score")
    location = candidate.get("input_location")
    if (
        isinstance(score, bool)
        or not isinstance(score, int)
        or score not in (0, 1)
        or not isinstance(location, str)
        or not location
    ):
        return None
    original_source = candidate.get("original_source")
    original_duration = candidate.get("original_duration_seconds")
    protocol = candidate.get("protocol")
    judge = {
        "status": "success",
        "score": score,
        "source": "llm_as_a_judge",
        "duration_seconds": 0.0,
        "return_code": None,
        "raw_summary": {},
        "error": None,
        "reused": True,
        "provenance": {
            "result_origin": "replay",
            "reused": True,
            "input_location": location,
            "original_source": (
                original_source if isinstance(original_source, str) else None
            ),
            "original_duration_seconds": (
                original_duration
                if isinstance(original_duration, (int, float))
                and not isinstance(original_duration, bool)
                else None
            ),
        },
    }
    if isinstance(protocol, str) and protocol:
        judge["protocol"] = protocol
    return judge


def _evaluate_attempt(
    task: Any,
    attempt: dict[str, Any],
    config: RunnerConfig,
    run_dir: Path,
) -> bool:
    """Score one captured attempt after every agent process has stopped.

    Returns true only when the external judge was requested but did not produce
    a valid binary decision.
    """

    replay_judge_candidate = attempt.pop("_replay_judge_candidate", None)
    detail_path, detail = _attempt_detail(task, attempt, run_dir)
    failure_origin = attempt.get("failure_origin")
    if failure_origin in _UNEVALUABLE_FAILURE_ORIGINS:
        attempt["evaluation_eligible"] = False
        detail["evaluation"] = {
            "enabled": True,
            "eligible": False,
            "reason": str(failure_origin),
            "tool_path_method": None,
            "final_answer_method": None,
            "judge": None,
        }
        _atomic_write_json(detail_path, detail)
        return False
    attempt["evaluation_eligible"] = True
    agent = detail.get("agent", {})
    if not isinstance(agent, Mapping):
        agent = {}
    trajectory = detail.get("trajectory", {})
    trace_calls = (
        trajectory.get("calls", []) if isinstance(trajectory, Mapping) else []
    )
    if not isinstance(trace_calls, list):
        trace_calls = []
    scores = score_attempt(
        task,
        agent.get("final_answer"),
        trace_calls,
        numeric_tolerance=config.numeric_tolerance,
    )

    judge: dict[str, Any] | None = None
    judge_failed = False
    if config.judge_command:
        if agent.get("status") != "success" or agent.get("final_answer") is None:
            # An execution without a final response is a benchmark failure, not
            # a judge infrastructure failure, and costs no LLM call.
            judge = {
                "status": "not_called",
                "score": 0,
                "source": "agent_execution_failure",
                "duration_seconds": 0.0,
                "error": None,
            }
        else:
            judge = (
                _reused_judge_execution(replay_judge_candidate)
                if config.reuse_successful_judge_results
                else None
            )
            if judge is None:
                component = _safe_task_component(_composite_id(task))
                judge_dir = (
                    run_dir
                    / "judge-workspaces"
                    / component
                    / f"attempt-{int(attempt.get('attempt', 1)):03d}"
                )
                try:
                    judge_dir.mkdir(parents=True, exist_ok=False)
                    execution = execute_judge_command(
                        config.judge_command,
                        _judge_envelope(task, agent.get("final_answer")),
                        cwd=judge_dir,
                        environment=_base_judge_environment(config),
                        timeout_seconds=config.judge_timeout_seconds,
                        inherit_environment=False,
                        include_reason=False,
                    )
                    judge = execution.to_dict()
                    judge["source"] = "llm_as_a_judge"
                    judge["reused"] = False
                    judge["provenance"] = {
                        "result_origin": "fresh_execution",
                        "reused": False,
                    }
                    judge_failed = execution.status != "success"
                except Exception as exc:
                    judge = {
                        "status": "failed",
                        "score": None,
                        "source": "llm_as_a_judge",
                        "reused": False,
                        "provenance": {
                            "result_origin": "fresh_execution",
                            "reused": False,
                        },
                        "duration_seconds": 0.0,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                    judge_failed = True

    detail["scores"] = scores
    detail["evaluation"] = {
        "enabled": True,
        "eligible": True,
        "tool_path_method": "lcs_tool_selection_accuracy",
        "final_answer_method": (
            "llm_as_a_judge" if config.judge_command else "deterministic_diagnostic"
        ),
        "judge": judge,
    }
    _atomic_write_json(detail_path, detail)

    tool_path_score = dict(scores["tool_path"])
    tool_path_score.pop("normalized_tool_calls", None)
    attempt["scores"] = {
        "ground_truth": scores["ground_truth"],
        "tool_path": tool_path_score,
    }
    if judge is not None:
        attempt["judge"] = judge
    return judge_failed


def _evaluate_question_results(
    tasks: Sequence[Any],
    question_results: Sequence[dict[str, Any]],
    config: RunnerConfig,
    run_dir: Path,
) -> bool:
    def evaluate_question(task: Any, question_result: dict[str, Any]) -> bool:
        question_result["evaluation_enabled"] = True
        attempts = question_result.get("attempts", [])
        workers = min(config.intra_task_parallelism, max(1, len(attempts)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            attempt_failures = list(
                pool.map(
                    lambda attempt: _evaluate_attempt(
                        task, attempt, config, run_dir
                    ),
                    attempts,
                )
            )
        successful = [
            attempt
            for attempt in attempts
            if attempt.get("agent", {}).get("status") == "success"
        ]
        scored_successful = [
            attempt for attempt in successful if isinstance(attempt.get("scores"), Mapping)
        ]
        question_result["ground_truth_pass"] = (
            any(
                bool(attempt["scores"]["ground_truth"].get("exact"))
                for attempt in scored_successful
            )
            if scored_successful
            else None
        )
        question_result["tool_path_pass"] = (
            any(
                bool(attempt["scores"]["tool_path"]["strict"].get("exact"))
                for attempt in scored_successful
            )
            if scored_successful
            else None
        )
        judge_attempts = [
            attempt for attempt in attempts if isinstance(attempt.get("judge"), Mapping)
        ]
        if judge_attempts:
            complete = all(
                _complete_judge_decision(attempt["judge"])
                for attempt in judge_attempts
            )
            question_result["judge_pass"] = (
                any(attempt["judge"].get("score") == 1 for attempt in judge_attempts)
                if complete
                else None
            )
        return any(attempt_failures)

    judge_failed = False
    with ThreadPoolExecutor(max_workers=config.task_parallelism) as pool:
        futures = [
            pool.submit(evaluate_question, task, question_result)
            for task, question_result in zip(tasks, question_results, strict=True)
        ]
        for future in as_completed(futures):
            judge_failed = future.result() or judge_failed
    return judge_failed


def _persist_capture_results(
    tasks: Sequence[Any],
    question_results: Sequence[dict[str, Any]],
    run_dir: Path,
) -> None:
    """Persist raw GT only after all concurrently running agents have exited."""

    for task, question_result in zip(tasks, question_results, strict=True):
        value = _task_dict(task)
        ground_truth = value.pop("ground_truth", None)
        capture_attempts: list[dict[str, Any]] = []
        for attempt in question_result.get("attempts", []):
            _, detail = _attempt_detail(task, attempt, run_dir)
            # Captures are deliberately self-contained: solution/reasoning,
            # transcript, raw calls, and authoritative normalized trajectory.
            detail.pop("scores", None)
            detail.pop("evaluation", None)
            capture_attempts.append(detail)
        component = _safe_task_component(_composite_id(task))
        capture_path = run_dir / "captures" / f"{component}.json"
        capture = {
            "schema": CAPTURE_SCHEMA,
            "question": value,
            "ground_truth": ground_truth,
            "ground_truth_sha256": _strict_json_sha256(ground_truth),
            "attempts": capture_attempts,
        }
        _atomic_write_json(capture_path, capture)
        question_result["capture_file"] = str(capture_path)
        question_result["ground_truth_saved"] = True


def _aggregate_core(question_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    attempts = [
        attempt
        for question in question_results
        for attempt in question.get("attempts", [])
    ]
    successful_attempts = [
        attempt
        for attempt in attempts
        if attempt.get("agent", {}).get("status") == "success"
    ]
    data_error_attempts = [
        attempt
        for attempt in attempts
        if attempt.get("failure_origin") in _DATA_FAILURE_ORIGINS
    ]
    agent_warning_attempts = [
        attempt
        for attempt in attempts
        if attempt.get("agent", {}).get("warnings")
    ]
    core: dict[str, Any] = {
        "questions": len(question_results),
        "attempts": len(attempts),
        "successful_attempts": len(successful_attempts),
        "agent_warning_attempt_count": len(agent_warning_attempts),
        "question_execution_success_rate": _mean(
            [float(bool(item.get("execution_success"))) for item in question_results]
        ),
        "data_error_attempt_count": len(data_error_attempts),
        "evaluation_enabled": any(
            bool(question.get("evaluation_enabled"))
            for question in question_results
        )
        or any(isinstance(attempt.get("scores"), Mapping) for attempt in attempts),
    }
    if not core["evaluation_enabled"]:
        return core

    scored_attempts = [
        attempt for attempt in attempts if isinstance(attempt.get("scores"), Mapping)
    ]
    ground_truth_scores = [
        float(attempt["scores"]["ground_truth"]["accuracy"])
        for attempt in scored_attempts
        if attempt["scores"]["ground_truth"]["available"]
    ]
    ground_truth_macro_scores = [
        float(attempt["scores"]["ground_truth"]["macro_accuracy"])
        for attempt in scored_attempts
        if attempt["scores"]["ground_truth"]["available"]
        and attempt["scores"]["ground_truth"].get("macro_accuracy") is not None
    ]
    tool_path_scores = [
        float(attempt["scores"]["tool_path"]["successful"]["accuracy"])
        for attempt in scored_attempts
        if attempt["scores"]["tool_path"]["available"]
    ]
    strict_path_scores = [
        attempt["scores"]["tool_path"]["strict"]
        for attempt in scored_attempts
        if attempt["scores"]["tool_path"]["available"]
    ]
    tsa_numerator = sum(
        int(score.get("tsa_numerator", score.get("ordered_matches", 0)))
        for score in strict_path_scores
    )
    tsa_denominator = sum(
        int(
            score.get(
                "tsa_denominator",
                len(score.get("matched_expected", [])),
            )
        )
        for score in strict_path_scores
    )
    path_questions = [
        item for item in question_results if item.get("tool_path_pass") is not None
    ]
    ground_truth_questions = [
        item for item in question_results if item.get("ground_truth_pass") is not None
    ]
    judge_targets = [
        attempt["judge"]
        for attempt in attempts
        if isinstance(attempt.get("judge"), Mapping)
    ]
    judge_completed = [
        judge for judge in judge_targets if _complete_judge_decision(judge)
    ]
    judge_successes = sum(judge.get("score") == 1 for judge in judge_completed)
    judge_reused_count = sum(
        judge.get("reused") is True for judge in judge_completed
    )
    judge_invocation_count = sum(
        isinstance(judge.get("provenance"), Mapping)
        and judge["provenance"].get("result_origin") == "fresh_execution"
        for judge in judge_targets
    )
    judge_target_count = len(judge_targets)
    judge_completed_count = len(judge_completed)
    judge_failed_count = judge_target_count - judge_completed_count
    judge_coverage = (
        judge_completed_count / judge_target_count if judge_target_count else None
    )
    provisional_tsa = (
        tsa_numerator / tsa_denominator if tsa_denominator else None
    )
    evaluation_complete = not data_error_attempts and judge_failed_count == 0
    core.update({
        "question_ground_truth_pass_rate": _mean(
            [float(bool(item.get("ground_truth_pass"))) for item in ground_truth_questions]
        ),
        "question_tool_path_pass_rate": _mean(
            [float(bool(item.get("tool_path_pass"))) for item in path_questions]
        ),
        "attempt_ground_truth_accuracy": _mean(ground_truth_scores),
        "attempt_ground_truth_macro_accuracy": _mean(ground_truth_macro_scores),
        "attempt_tool_path_accuracy": _mean(tool_path_scores),
        "tool_selection_accuracy": (
            provisional_tsa if not data_error_attempts else None
        ),
        "provisional_tool_selection_accuracy": provisional_tsa,
        "tool_selection_lcs_sum": tsa_numerator,
        "tool_selection_expected_length_sum": tsa_denominator,
        "success_rate": (
            judge_successes / judge_target_count
            if judge_target_count and evaluation_complete
            else None
        ),
        "provisional_success_rate": (
            judge_successes / judge_completed_count if judge_completed_count else None
        ),
        "judge_success_count": judge_successes,
        "judge_reused_count": judge_reused_count,
        "judge_invocation_count": judge_invocation_count,
        "judge_target_count": judge_target_count,
        "judge_completed_count": judge_completed_count,
        "judge_failed_count": judge_failed_count,
        "judge_coverage": judge_coverage,
        "evaluation_complete": evaluation_complete,
        "evaluation_ineligible_attempt_count": sum(
            attempt.get("evaluation_eligible") is False for attempt in attempts
        ),
        "question_judge_pass_at_k": _mean(
            [
                float(bool(item.get("judge_pass")))
                for item in question_results
                if item.get("judge_pass") is not None
            ]
        ),
    })
    return core


def _null_evaluation_metrics(
    core: dict[str, Any], *, evaluation_enabled: bool
) -> dict[str, Any]:
    """Give non-evaluated matrix cells explicit null metrics, never fake zeros."""

    core.update(
        {
            "evaluation_enabled": evaluation_enabled,
            "question_ground_truth_pass_rate": None,
            "question_tool_path_pass_rate": None,
            "attempt_ground_truth_accuracy": None,
            "attempt_ground_truth_macro_accuracy": None,
            "attempt_tool_path_accuracy": None,
            "tool_selection_accuracy": None,
            "provisional_tool_selection_accuracy": None,
            "tool_selection_lcs_sum": 0,
            "tool_selection_expected_length_sum": 0,
            "success_rate": None,
            "provisional_success_rate": None,
            "judge_success_count": 0,
            "judge_reused_count": 0,
            "judge_invocation_count": 0,
            "judge_target_count": 0,
            "judge_completed_count": 0,
            "judge_failed_count": 0,
            "judge_coverage": None,
            "evaluation_complete": None,
            "evaluation_ineligible_attempt_count": 0,
            "question_judge_pass_at_k": None,
        }
    )
    return core


def _matrix_cell_metrics(
    values: Sequence[Mapping[str, Any]], *, evaluation_enabled: bool
) -> dict[str, Any]:
    metrics = _aggregate_core(values)
    if not metrics.get("evaluation_enabled"):
        return _null_evaluation_metrics(
            metrics,
            evaluation_enabled=evaluation_enabled,
        )
    return metrics


def _result_dimensions(result: Mapping[str, Any]) -> tuple[str, str, str]:
    question = result.get("question", {})
    if not isinstance(question, Mapping):
        question = {}
    public = _benchmark_dimensions(question, allow_reference_hints=False)
    subtype = public["task_subtype"]
    if question.get("benchmark_dimensions_finalized") is True:
        subtype = str(question.get("task_subtype") or "UNKNOWN")
    return public["problem_group"], public["graph_family"], subtype


def _benchmark_matrix(
    question_results: Sequence[Mapping[str, Any]],
    *,
    evaluation_enabled: bool,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    unmapped_datasets: set[str] = set()
    unknown_task_types: dict[tuple[str, str], int] = {}
    unexpected_cells: dict[tuple[str, str, str], int] = {}

    for result in question_results:
        problem, family, subtype = _result_dimensions(result)
        grouped.setdefault((problem, family, subtype), []).append(result)
        question = result.get("question", {})
        if not isinstance(question, Mapping):
            question = {}
        if family == "UNKNOWN":
            unmapped_datasets.add(str(question.get("dataset") or "unknown"))
        if problem == "UNKNOWN" or subtype == "UNKNOWN":
            key = (problem, str(question.get("task_type") or "unknown"))
            unknown_task_types[key] = unknown_task_types.get(key, 0) + 1
        expected = _EXPECTED_MATRIX_CELLS.get(problem, {}).get(family, ())
        if subtype not in expected:
            key = (problem, family, subtype)
            unexpected_cells[key] = unexpected_cells.get(key, 0) + 1

    ordered_keys: list[tuple[str, str, str]] = []
    for problem, families in _EXPECTED_MATRIX_CELLS.items():
        for family, subtypes in families.items():
            ordered_keys.extend((problem, family, subtype) for subtype in subtypes)
    expected_key_set = set(ordered_keys)
    ordered_keys.extend(key for key in sorted(grouped) if key not in expected_key_set)

    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for problem, family, subtype in ordered_keys:
        cells.setdefault(problem, {}).setdefault(family, {})[subtype] = (
            _matrix_cell_metrics(
                grouped.get((problem, family, subtype), []),
                evaluation_enabled=evaluation_enabled,
            )
        )

    in_taxonomy_count = sum(
        len(values)
        for key, values in grouped.items()
        if key[2] in _EXPECTED_MATRIX_CELLS.get(key[0], {}).get(key[1], ())
    )
    return {
        "schema": "igraph.benchmark.matrix.v1",
        "taxonomy": {
            "version": "gabench.v1",
            "graph_families": {
                family: {
                    "label": str(definition["label"]),
                    "datasets": list(definition["datasets"]),
                }
                for family, definition in _GRAPH_FAMILY_DEFINITIONS.items()
            },
            "problem_task_subtypes": {
                problem: list(subtypes)
                for problem, subtypes in _PROBLEM_TASK_SUBTYPES.items()
            },
            "score_scale": "fraction_0_to_1",
            "tsa": "sum_lcs_over_sum_reference_path_length",
            "sr": "binary_judge_successes_over_targets",
        },
        "classification_complete": in_taxonomy_count == len(question_results),
        "classified_question_count": in_taxonomy_count,
        "out_of_taxonomy_question_count": len(question_results) - in_taxonomy_count,
        "unmapped_datasets": sorted(unmapped_datasets, key=str.casefold),
        "unknown_task_types": [
            {
                "problem_group": problem,
                "task_type": task_type,
                "questions": count,
            }
            for (problem, task_type), count in sorted(unknown_task_types.items())
        ],
        "unexpected_cells": [
            {
                "problem_group": problem,
                "graph_family": family,
                "task_subtype": subtype,
                "questions": count,
            }
            for (problem, family, subtype), count in sorted(unexpected_cells.items())
        ],
        "cells": cells,
    }


def _aggregate(question_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    overall = _aggregate_core(question_results)
    category_groups: dict[str, list[Mapping[str, Any]]] = {}
    dataset_groups: dict[str, list[Mapping[str, Any]]] = {}
    graph_family_groups: dict[str, list[Mapping[str, Any]]] = {}
    for result in question_results:
        question = result.get("question", {})
        category = str(question.get("category", "unknown"))
        dataset = str(question.get("dataset", "unknown"))
        category_groups.setdefault(category, []).append(result)
        dataset_groups.setdefault(dataset, []).append(result)
        _, graph_family, _ = _result_dimensions(result)
        graph_family_groups.setdefault(graph_family, []).append(result)
    overall["by_category"] = {
        name: _aggregate_core(values)
        for name, values in sorted(category_groups.items())
    }
    overall["by_dataset"] = {
        name: _aggregate_core(values)
        for name, values in sorted(dataset_groups.items())
    }
    overall["by_graph_family"] = {
        name: _aggregate_core(values)
        for name, values in sorted(graph_family_groups.items())
    }
    overall["benchmark_matrix"] = _benchmark_matrix(
        question_results,
        evaluation_enabled=bool(overall.get("evaluation_enabled")),
    )
    return overall


def run_questions(tasks: Sequence[Any], config: RunnerConfig) -> tuple[dict[str, Any], int]:
    _validate_config(config)
    if not tasks:
        raise ValueError("no benchmark questions selected")
    task_bindings = _preflight_tasks(tasks, config)
    run_id = f"questions-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = config.output_dir.expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    started_at = _utc_now()
    started_clock = time.perf_counter()

    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "duration_seconds": None,
        "result_file": str(result_path),
        "config": {
            "questions_root": (
                str(config.questions_root.expanduser().resolve())
                if config.questions_root is not None
                else None
            ),
            "dataset_root": str(config.dataset_root.expanduser().resolve()),
            "task_parallelism": config.task_parallelism,
            "intra_task_parallelism": config.intra_task_parallelism,
            "attempts_per_question": config.attempts_per_question,
            "maximum_concurrent_attempts": config.task_parallelism
            * min(config.intra_task_parallelism, config.attempts_per_question),
            "timeout_seconds": config.timeout_seconds,
            "numeric_tolerance": config.numeric_tolerance,
            "virtual_graph_ml": config.virtual_graph_ml,
            "evaluation_enabled": config.evaluate,
            "evaluation_method": (
                "llm_as_a_judge_and_lcs_tsa"
                if config.evaluate and config.judge_command
                else "deterministic_diagnostic_and_lcs_tsa"
                if config.evaluate
                else "capture_only"
            ),
            "judge_timeout_seconds": config.judge_timeout_seconds,
            "agent_environment_names": list(config.agent_environment_names),
            "judge_environment_names": list(config.judge_environment_names),
            "execution_mode": (
                "validate_only"
                if config.validate_only
                else "replay"
                if config.replay_file is not None
                else "command"
            ),
            "recover_cleanup_failures": config.recover_cleanup_failures,
            "reuse_successful_judge_results": (
                config.reuse_successful_judge_results
            ),
            "agent_command": _redacted_command(config.agent_command),
            "judge_command": _redacted_command(config.judge_command),
        },
        "evaluation": {
            "enabled": config.evaluate,
            "reuse_successful_judge_results": (
                config.reuse_successful_judge_results
            ),
            "tool_path_metric": (
                "TSA=sum(LCS(agent,ground_truth))/sum(len(ground_truth))"
                if config.evaluate
                else None
            ),
            "final_answer_metric": (
                "binary_llm_judge_success_rate"
                if config.evaluate and config.judge_command
                else "deterministic_diagnostic"
                if config.evaluate
                else None
            ),
            "raw_ground_truth_saved": False,
        },
        "selection": {
            "question_count": len(tasks),
            "question_ids": [_composite_id(task) for task in tasks],
            "sampling": dict(config.selection_metadata or {"mode": "standard"}),
        },
        "questions": [],
        "summary": None,
    }
    _atomic_write_json(result_path, result)

    if config.validate_only:
        result["status"] = "validated"
        result["questions"] = [
            {
                "question": {**_audit_task(task), **_source_provenance(task)},
                "dataset_path": str(binding.masked_path or binding.base_path),
                "dataset_binding": binding.to_dict(),
                "expected_tool_path": expected_tool_path(task),
            }
            for task, binding in zip(tasks, task_bindings, strict=True)
        ]
        result["summary"] = {
            "questions": len(tasks),
            "attempts": 0,
            "categories": sorted(
                {str(_task_value(task, "category", "unknown")) for task in tasks}
            ),
            "datasets": sorted(
                {str(_task_value(task, "dataset", "unknown")) for task in tasks}
            ),
            "evaluation_enabled": False,
        }
        exit_code = 0
    else:
        replay_store = ReplayStore(config.replay_file) if config.replay_file else None
        ordered: list[dict[str, Any] | None] = [None] * len(tasks)
        completed_count = 0
        checkpoint_every = max(1, math.ceil(len(tasks) / 100))
        try:
            with ThreadPoolExecutor(max_workers=config.task_parallelism) as pool:
                futures = {
                    pool.submit(
                        _run_question,
                        task,
                        index,
                        config,
                        run_dir,
                        replay_store,
                        task_bindings[index],
                    ): index
                    for index, task in enumerate(tasks)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        ordered[index] = future.result()
                    except Exception as exc:
                        task = tasks[index]
                        ordered[index] = {
                            "question": _audit_task(task),
                            "attempts": [
                                {
                                    "question_id": _composite_id(task),
                                    "attempt": attempt,
                                    "agent": _failed_execution(exc).to_dict(),
                                    "failure_origin": "runner_internal",
                                    "trajectory": {
                                        "strict_path": [],
                                        "successful_path": [],
                                        "call_count": 0,
                                    },
                                }
                                for attempt in range(
                                    1, config.attempts_per_question + 1
                                )
                            ],
                            "best_attempt": None,
                            "selected_attempt": None,
                            "execution_success": False,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        }
                    completed_count += 1
                    if (
                        completed_count % checkpoint_every == 0
                        or completed_count == len(tasks)
                    ):
                        completed_results = [
                            item for item in ordered if item is not None
                        ]
                        result["questions"] = completed_results
                        result["summary"] = _aggregate(completed_results)
                        _atomic_write_json(result_path, result)
        finally:
            # mmap-backed checkpoints must be released even if collection is
            # interrupted or a checkpoint write raises unexpectedly.
            clear_masked_cache()
        question_results = [item for item in ordered if item is not None]
        # At this point the outer executor and every nested attempt executor are
        # closed, so publishing dataset/question provenance can no longer help
        # a still-running sibling agent locate ground truth.
        _attach_final_provenance(
            tasks,
            question_results,
            task_bindings,
            run_dir,
        )
        result["questions"] = question_results
        evaluation_errors = False
        if config.evaluate:
            # This phase begins only after both nested executors have shut down,
            # so no concurrently running agent can inspect GT passed to a judge.
            evaluation_errors = _evaluate_question_results(
                tasks,
                question_results,
                config,
                run_dir,
            )
        # Capture-only and evaluated runs both retain the raw solution/tool
        # trajectory alongside its ground truth.  Keep this after evaluation:
        # every agent has exited before GT is exposed, and every judge has
        # finished reading the scored attempt before its raw capture is made.
        _persist_capture_results(tasks, question_results, run_dir)
        result["evaluation"]["raw_ground_truth_saved"] = True
        result["summary"] = _aggregate(question_results)
        has_execution_errors = any(
            not bool(item.get("execution_success")) for item in question_results
        ) or any(
            attempt.get("agent", {}).get("warnings")
            for item in question_results
            for attempt in item.get("attempts", [])
        )
        has_data_errors = any(
            attempt.get("failure_origin") in _DATA_FAILURE_ORIGINS
            for item in question_results
            for attempt in item.get("attempts", [])
        )
        if has_data_errors:
            result["status"] = "completed_with_data_errors"
        elif evaluation_errors:
            result["status"] = "completed_with_evaluation_errors"
        elif has_execution_errors:
            result["status"] = "completed_with_errors"
        else:
            result["status"] = "completed"
        exit_code = 4 if has_data_errors else 5 if has_execution_errors or evaluation_errors else 0

    result["finished_at"] = _utc_now()
    result["duration_seconds"] = time.perf_counter() - started_clock
    _atomic_write_json(result_path, result)
    return result, exit_code
