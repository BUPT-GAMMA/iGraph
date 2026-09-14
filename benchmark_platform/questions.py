"""Question discovery and loading for the headless benchmark platform.

The canonical on-disk layout is::

    <root>/<category>/<dataset>/questions.json

where ``category`` is one of the five GraphAgentBench categories.  The loader
is deliberately independent from the benchmark runner so it can be reused by
parallel workers without importing model or agent dependencies.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import random
from typing import Any, Callable, Iterable, Mapping, Sequence

from .tool_names import expected_tool_path


STANDARD_SCHEMA = "igraph.benchmark.questions"
STANDARD_SCHEMA_VERSION = "1.0"
QUESTION_CATEGORIES = (
    "theory",
    "retrieval",
    "ml",
    "insight",
    "prediction",
)


class QuestionError(ValueError):
    """Base class for invalid question inputs."""


class QuestionDiscoveryError(QuestionError):
    """Raised when question files cannot be discovered."""


class QuestionSchemaError(QuestionError):
    """Raised when a question file does not satisfy the supported schemas."""


@dataclass(frozen=True, slots=True)
class QuestionRecord:
    """One normalized question while retaining all evaluation payloads."""

    composite_id: str
    id: str
    question: str
    category: str
    dataset: str
    task_type: str | None
    required_tools: tuple[str, ...]
    ground_truth: Any
    context: Mapping[str, Any]
    source_file: str
    source_index: int
    raw: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation for worker payloads."""

        return {
            "composite_id": self.composite_id,
            "id": self.id,
            "question": self.question,
            "category": self.category,
            "dataset": self.dataset,
            "task_type": self.task_type,
            "required_tools": list(self.required_tools),
            "ground_truth": self.ground_truth,
            "context": dict(self.context),
            "source_file": self.source_file,
            "source_index": self.source_index,
        }


@dataclass(frozen=True, slots=True)
class LoadedQuestionFile:
    """Validated contents and metadata for one ``questions.json`` file."""

    path: Path
    category: str
    dataset: str
    metadata: Mapping[str, Any]
    questions: tuple[QuestionRecord, ...]

    @property
    def question_count(self) -> int:
        return len(self.questions)

    @property
    def key(self) -> str:
        return f"{self.category}/{self.dataset}"


@dataclass(frozen=True, slots=True)
class QuestionLoadResult:
    """Selected questions plus pre- and post-selection per-file counts."""

    questions: tuple[QuestionRecord, ...]
    file_counts: Mapping[str, int]
    selected_file_counts: Mapping[str, int]
    files: tuple[str, ...]
    available_type_counts: Mapping[str, int] = field(default_factory=dict)
    selected_type_counts: Mapping[str, int] = field(default_factory=dict)
    questions_per_type: int | None = None

    @property
    def question_count(self) -> int:
        return len(self.questions)


def _as_filter_values(
    value: str | Iterable[str] | None,
    *,
    field: str,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    values = (value,) if isinstance(value, str) else tuple(value)
    if not values:
        return tuple()
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise QuestionDiscoveryError(f"{field} filters must be non-empty strings")
    return tuple(item.strip() for item in values)


def discover_question_files(
    root: str | Path,
    categories: str | Iterable[str] | None = None,
    datasets: str | Iterable[str] | None = None,
    *,
    require_complete_grid: bool = False,
) -> list[Path]:
    """Discover canonical question files in deterministic order.

    Dataset filtering is case-insensitive so callers may request ``nba`` while
    the canonical dataset directory remains ``NBA``.  Header/path consistency
    is still checked case-sensitively when each file is loaded.
    """

    if not isinstance(require_complete_grid, bool):
        raise QuestionDiscoveryError("require_complete_grid must be a boolean")

    root_path = Path(root)
    if not root_path.is_dir():
        raise QuestionDiscoveryError(f"question root is not a directory: {root_path}")

    requested_categories = _as_filter_values(categories, field="category")
    if requested_categories is None:
        category_order = QUESTION_CATEGORIES
    else:
        unknown = sorted(set(requested_categories) - set(QUESTION_CATEGORIES))
        if unknown:
            raise QuestionDiscoveryError(
                "unsupported question categories: " + ", ".join(unknown)
            )
        requested = set(requested_categories)
        category_order = tuple(
            category for category in QUESTION_CATEGORIES if category in requested
        )

    requested_datasets = _as_filter_values(datasets, field="dataset")
    dataset_keys = (
        None
        if requested_datasets is None
        else {dataset.casefold() for dataset in requested_datasets}
    )

    discovered: list[Path] = []
    for category in category_order:
        category_dir = root_path / category
        if not category_dir.is_dir():
            continue
        dataset_dirs = sorted(
            (path for path in category_dir.iterdir() if path.is_dir()),
            key=lambda path: (path.name.casefold(), path.name),
        )
        for dataset_dir in dataset_dirs:
            if dataset_keys is not None and dataset_dir.name.casefold() not in dataset_keys:
                continue
            question_file = dataset_dir / "questions.json"
            if question_file.is_file():
                discovered.append(question_file)

    if requested_datasets is not None:
        matched_keys = {path.parent.name.casefold() for path in discovered}
        missing = [
            dataset
            for dataset in requested_datasets
            if dataset.casefold() not in matched_keys
        ]
        if missing:
            raise QuestionDiscoveryError(
                "requested datasets have no matching questions.json in the "
                "selected categories: " + ", ".join(sorted(set(missing), key=str.casefold))
            )
        if require_complete_grid:
            matched_pairs = {
                (path.parent.parent.name, path.parent.name.casefold())
                for path in discovered
            }
            missing_pairs = [
                f"{category}/{dataset}"
                for category in category_order
                for dataset in requested_datasets
                if (category, dataset.casefold()) not in matched_pairs
            ]
            if missing_pairs:
                raise QuestionDiscoveryError(
                    "balanced sampling requires questions.json for every selected "
                    "category/dataset pair; missing: " + ", ".join(missing_pairs)
                )
    return discovered


def _reject_nonfinite_json(value: str) -> None:
    raise QuestionSchemaError(f"non-finite JSON number is not allowed: {value}")


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream, parse_constant=_reject_nonfinite_json)
    except QuestionSchemaError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionSchemaError(f"cannot read valid JSON from {path}: {exc}") from exc


def _infer_path_scope(path: Path) -> tuple[str | None, str | None]:
    dataset = path.parent.name or None
    category = path.parent.parent.name or None
    if category not in QUESTION_CATEGORIES:
        return None, None
    return category, dataset


def _nonempty_string(value: Any, *, location: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionSchemaError(f"{location}: {field} must be a non-empty string")
    return value


def _validate_scope(
    *,
    category: Any,
    dataset: Any,
    location: str,
    path_category: str | None,
    path_dataset: str | None,
    expected_category: str | None,
    expected_dataset: str | None,
) -> tuple[str, str]:
    category_value = _nonempty_string(category, location=location, field="category")
    dataset_value = _nonempty_string(dataset, location=location, field="dataset")
    if category_value not in QUESTION_CATEGORIES:
        raise QuestionSchemaError(
            f"{location}: unsupported category {category_value!r}"
        )
    checks = (
        ("path category", path_category, category_value),
        ("path dataset", path_dataset, dataset_value),
        ("expected category", expected_category, category_value),
        ("expected dataset", expected_dataset, dataset_value),
    )
    for label, expected, actual in checks:
        if expected is not None and expected != actual:
            raise QuestionSchemaError(
                f"{location}: {label} {expected!r} does not match header {actual!r}"
            )
    return category_value, dataset_value


def _legacy_scope_value(
    payload: Mapping[str, Any],
    name: str,
    *,
    expected: str | None,
    inferred: str | None,
    location: str,
) -> str:
    candidates = [value for value in (payload.get(name), expected, inferred) if value is not None]
    if not candidates:
        raise QuestionSchemaError(
            f"{location}: legacy input needs {name!r} in the wrapper or canonical path"
        )
    first = _nonempty_string(candidates[0], location=location, field=name)
    for candidate in candidates[1:]:
        candidate_value = _nonempty_string(candidate, location=location, field=name)
        if candidate_value != first:
            raise QuestionSchemaError(
                f"{location}: inconsistent legacy {name}: {first!r} != {candidate_value!r}"
            )
    return first


def _alias_value(
    task: Mapping[str, Any],
    aliases: Sequence[str],
    *,
    location: str,
    required: bool,
) -> Any:
    found = [(name, task[name]) for name in aliases if name in task]
    if not found:
        if required:
            raise QuestionSchemaError(
                f"{location}: missing required field {aliases[0]!r}"
            )
        return None
    first_name, first_value = found[0]
    for other_name, other_value in found[1:]:
        if other_value != first_value:
            raise QuestionSchemaError(
                f"{location}: conflicting aliases {first_name!r} and {other_name!r}"
            )
    return first_value


def _validate_json_tree(value: Any, *, location: str) -> None:
    # Large insight/prediction answers may contain millions of scalar leaves.
    # Walk them iteratively and keep only the root diagnostic location; eagerly
    # constructing a distinct dotted path string for every valid leaf makes a
    # validate-only run unnecessarily CPU-heavy. JSON parsing already gives us
    # a tree without reference cycles.
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, float) and not math.isfinite(current):
            raise QuestionSchemaError(
                f"{location}: non-finite number is not allowed"
            )
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise QuestionSchemaError(
                        f"{location}: object keys must be strings"
                    )
                pending.append(nested)
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
        elif current is not None and not isinstance(
            current, (str, int, float, bool)
        ):
            raise QuestionSchemaError(
                f"{location}: unsupported value type {type(current).__name__}"
            )


def _normalize_task(
    task: Any,
    *,
    category: str,
    dataset: str,
    path: Path,
    index: int,
    strict_standard: bool,
) -> QuestionRecord:
    location = f"{path}: tasks[{index}]"
    if not isinstance(task, Mapping):
        raise QuestionSchemaError(f"{location}: task must be an object")

    if strict_standard:
        task_id_raw = _alias_value(task, ("id",), location=location, required=True)
        question_raw = _alias_value(
            task, ("question",), location=location, required=True
        )
        ground_truth = _alias_value(
            task, ("ground_truth",), location=location, required=True
        )
        for required_field in ("task_type", "required_tools", "context"):
            if required_field not in task:
                raise QuestionSchemaError(
                    f"{location}: missing required field {required_field!r}"
                )
    else:
        task_id_raw = _alias_value(
            task, ("id", "question_id", "task_id"), location=location, required=True
        )
        question_raw = _alias_value(
            task, ("question", "prompt"), location=location, required=True
        )
        ground_truth = _alias_value(
            task, ("ground_truth", "answer"), location=location, required=True
        )

    task_id = _nonempty_string(task_id_raw, location=location, field="id")
    if "/" in task_id or "\\" in task_id:
        raise QuestionSchemaError(
            f"{location}: id cannot contain path separators: {task_id!r}"
        )
    question = _nonempty_string(question_raw, location=location, field="question")

    task_category = task.get("category")
    task_dataset = task.get("dataset")
    if task_category is not None and task_category != category:
        raise QuestionSchemaError(
            f"{location}: task category {task_category!r} does not match {category!r}"
        )
    if task_dataset is not None and task_dataset != dataset:
        raise QuestionSchemaError(
            f"{location}: task dataset {task_dataset!r} does not match {dataset!r}"
        )

    task_type_raw = task.get("task_type")
    if task_type_raw is None:
        task_type = None
    else:
        task_type = _nonempty_string(
            task_type_raw, location=location, field="task_type"
        )

    required_tools_raw = task.get("required_tools", [])
    if not isinstance(required_tools_raw, list):
        raise QuestionSchemaError(f"{location}: required_tools must be an array")
    required_tools: list[str] = []
    for tool_index, tool_name in enumerate(required_tools_raw):
        required_tools.append(
            _nonempty_string(
                tool_name,
                location=location,
                field=f"required_tools[{tool_index}]",
            )
        )
    try:
        expected_tool_path(
            {
                "task_type": task_type,
                "required_tools": required_tools,
            }
        )
    except ValueError as exc:
        raise QuestionSchemaError(
            f"{location}: required_tools contains an unknown graph tool: {exc}"
        ) from exc

    context_raw = task.get("context", {})
    if not isinstance(context_raw, Mapping):
        raise QuestionSchemaError(f"{location}: context must be an object")

    if ground_truth is None or isinstance(ground_truth, (list, tuple)):
        raise QuestionSchemaError(
            f"{location}: ground_truth must be a non-null scalar or object"
        )
    if not isinstance(ground_truth, (Mapping, str, int, float, bool)):
        raise QuestionSchemaError(
            f"{location}: unsupported ground_truth type {type(ground_truth).__name__}"
        )
    _validate_json_tree(ground_truth, location=f"{location}.ground_truth")
    _validate_json_tree(context_raw, location=f"{location}.context")

    composite_id = f"{category}/{dataset}/{task_id}"
    return QuestionRecord(
        composite_id=composite_id,
        id=task_id,
        question=question,
        category=category,
        dataset=dataset,
        task_type=task_type,
        required_tools=tuple(required_tools),
        ground_truth=ground_truth,
        context=dict(context_raw),
        # load_question_file resolves its path once before normalizing tasks;
        # avoid repeating filesystem resolution for every question in a large
        # corpus.
        source_file=str(path),
        source_index=index,
        raw=dict(task),
    )


def load_question_file(
    path: str | Path,
    *,
    expected_category: str | None = None,
    expected_dataset: str | None = None,
) -> LoadedQuestionFile:
    """Load and validate one standard or legacy question file."""

    question_path = Path(path).expanduser().resolve()
    if not question_path.is_file():
        raise QuestionDiscoveryError(f"question file does not exist: {question_path}")
    payload = _read_json(question_path)
    path_category, path_dataset = _infer_path_scope(question_path)
    location = str(question_path)

    strict_standard = False
    metadata: Mapping[str, Any] = {}
    if isinstance(payload, Mapping) and (
        "schema" in payload or "schema_version" in payload or "tasks" in payload
    ):
        strict_standard = True
        if payload.get("schema") != STANDARD_SCHEMA:
            raise QuestionSchemaError(
                f"{location}: schema must be {STANDARD_SCHEMA!r}"
            )
        if payload.get("schema_version") != STANDARD_SCHEMA_VERSION:
            raise QuestionSchemaError(
                f"{location}: schema_version must be {STANDARD_SCHEMA_VERSION!r}"
            )
        category, dataset = _validate_scope(
            category=payload.get("category"),
            dataset=payload.get("dataset"),
            location=location,
            path_category=path_category,
            path_dataset=path_dataset,
            expected_category=expected_category,
            expected_dataset=expected_dataset,
        )
        metadata_raw = payload.get("metadata")
        if not isinstance(metadata_raw, Mapping):
            raise QuestionSchemaError(f"{location}: metadata must be an object")
        metadata = dict(metadata_raw)
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            raise QuestionSchemaError(f"{location}: tasks must be an array")
    else:
        wrapper = payload if isinstance(payload, Mapping) else {}
        category = _legacy_scope_value(
            wrapper,
            "category",
            expected=expected_category,
            inferred=path_category,
            location=location,
        )
        dataset = _legacy_scope_value(
            wrapper,
            "dataset",
            expected=expected_dataset,
            inferred=path_dataset,
            location=location,
        )
        if category not in QUESTION_CATEGORIES:
            raise QuestionSchemaError(f"{location}: unsupported category {category!r}")
        if isinstance(payload, list):
            tasks = payload
        elif isinstance(payload, Mapping) and "questions" in payload:
            tasks = payload["questions"]
        elif isinstance(payload, Mapping) and "benchmark" in payload:
            benchmark_value = payload["benchmark"]
            if isinstance(benchmark_value, Mapping):
                tasks = benchmark_value.get("tasks", benchmark_value.get("questions"))
            else:
                tasks = benchmark_value
        elif isinstance(payload, Mapping):
            tasks = [payload]
        else:
            raise QuestionSchemaError(
                f"{location}: top level must be an object or array"
            )
        if not isinstance(tasks, list):
            raise QuestionSchemaError(
                f"{location}: legacy questions/benchmark value must contain an array"
            )
        metadata_raw = wrapper.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise QuestionSchemaError(f"{location}: metadata must be an object")
        metadata = dict(metadata_raw)

    normalized = tuple(
        _normalize_task(
            task,
            category=category,
            dataset=dataset,
            path=question_path,
            index=index,
            strict_standard=strict_standard,
        )
        for index, task in enumerate(tasks)
    )
    seen: set[str] = set()
    for question in normalized:
        if question.composite_id in seen:
            raise QuestionSchemaError(
                f"{location}: duplicate composite id {question.composite_id!r}"
            )
        seen.add(question.composite_id)
    return LoadedQuestionFile(
        path=question_path,
        category=category,
        dataset=dataset,
        metadata=metadata,
        questions=normalized,
    )


def _validate_limit(value: int | None, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuestionDiscoveryError(f"{name} must be a non-negative integer")
    return value


def load_question_files(
    files: Iterable[str | Path],
    question_count: int | None = None,
    questions_per_file: int | None = None,
    shuffle: bool = False,
    seed: int = 42,
    questions_per_type: int | None = None,
    type_key: Callable[[QuestionRecord], str] | None = None,
    expected_type_keys: Callable[[LoadedQuestionFile], Iterable[str]] | None = None,
) -> QuestionLoadResult:
    """Validate and select questions from an explicit sequence of files.

    ``questions_per_file`` and ``question_count`` are caps.  Alternatively,
    ``questions_per_type`` selects exactly that many records for every key
    returned by ``type_key``.  Supplying ``shuffle=True`` makes either mode
    reproducible with ``seed``.  Duplicate IDs and the complete type inventory
    are checked before selection, preventing invalid or undersized groups from
    being hidden by sampling.
    """

    per_file_limit = _validate_limit(questions_per_file, name="questions_per_file")
    global_limit = _validate_limit(question_count, name="question_count")
    per_type_limit = _validate_limit(
        questions_per_type, name="questions_per_type"
    )
    balanced = per_type_limit is not None
    if balanced and per_type_limit == 0:
        raise QuestionDiscoveryError("questions_per_type must be a positive integer")
    if balanced and (per_file_limit is not None or global_limit is not None):
        raise QuestionDiscoveryError(
            "questions_per_type cannot be combined with question_count or "
            "questions_per_file"
        )
    if balanced != (type_key is not None):
        raise QuestionDiscoveryError(
            "questions_per_type and type_key must be provided together"
        )
    if expected_type_keys is not None and not balanced:
        raise QuestionDiscoveryError(
            "expected_type_keys requires questions_per_type"
        )
    if not isinstance(shuffle, bool):
        raise QuestionDiscoveryError("shuffle must be a boolean")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise QuestionDiscoveryError("seed must be an integer")

    paths = [Path(path) for path in files]
    if not paths:
        raise QuestionDiscoveryError("at least one question file is required")
    resolved_paths: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in resolved_paths:
            raise QuestionDiscoveryError(f"question file was provided twice: {path}")
        resolved_paths.add(resolved)

    seen: dict[str, str] = {}
    rng = random.Random(seed) if shuffle else None
    selected: list[QuestionRecord] = []
    selected_by_type: dict[str, list[tuple[int, QuestionRecord]]] = {}
    available_type_counter: Counter[str] = Counter()
    expected_types: set[str] = set()
    encounter_index = 0
    file_counts: dict[str, int] = {}
    file_keys: list[str] = []
    for path in paths:
        # Process one JSON file at a time.  With a small global cap this keeps
        # unselected graph/subgraph ground truth out of long-lived memory while
        # still validating every file and every composite ID before execution.
        loaded_file = load_question_file(path)
        file_keys.append(loaded_file.key)
        file_counts[loaded_file.key] = loaded_file.question_count
        for question in loaded_file.questions:
            previous = seen.get(question.composite_id)
            if previous is not None:
                raise QuestionSchemaError(
                    "duplicate composite id "
                    f"{question.composite_id!r} in {previous} and {question.source_file}"
                )
            seen[question.composite_id] = question.source_file

        if balanced:
            declared_type_set: set[str] | None = None
            if expected_type_keys is not None:
                try:
                    declared_keys = tuple(expected_type_keys(loaded_file))
                except (TypeError, ValueError) as exc:
                    raise QuestionDiscoveryError(
                        f"cannot derive benchmark types for {loaded_file.key}: {exc}"
                    ) from exc
                declared_type_set = set()
                for key in declared_keys:
                    if not isinstance(key, str) or not key.strip():
                        raise QuestionDiscoveryError(
                            f"invalid benchmark type key for {loaded_file.key}: {key!r}"
                        )
                    expected_types.add(key)
                    declared_type_set.add(key)

            assert type_key is not None
            assert per_type_limit is not None
            for question in loaded_file.questions:
                encounter_index += 1
                try:
                    key = type_key(question)
                except (TypeError, ValueError) as exc:
                    raise QuestionDiscoveryError(
                        "cannot classify question "
                        f"{question.composite_id!r} for balanced sampling: {exc}"
                    ) from exc
                if not isinstance(key, str) or not key.strip():
                    raise QuestionDiscoveryError(
                        "balanced sampling type keys must be non-empty strings; "
                        f"got {key!r} for {question.composite_id!r}"
                    )
                if declared_type_set is not None and key not in declared_type_set:
                    raise QuestionDiscoveryError(
                        f"question {question.composite_id!r} belongs to unexpected "
                        f"benchmark type {key!r}"
                    )

                available_type_counter[key] += 1
                seen_in_type = available_type_counter[key]
                bucket = selected_by_type.setdefault(key, [])
                candidate = (encounter_index, question)
                if len(bucket) < per_type_limit:
                    bucket.append(candidate)
                elif rng is not None:
                    replacement = rng.randrange(seen_in_type)
                    if replacement < per_type_limit:
                        bucket[replacement] = candidate
            continue

        file_questions = list(loaded_file.questions)
        if rng is not None:
            rng.shuffle(file_questions)
        if per_file_limit is not None:
            file_questions = file_questions[:per_file_limit]
        if rng is not None or global_limit is None:
            selected.extend(file_questions)
        else:
            remaining = max(0, global_limit - len(selected))
            if remaining:
                selected.extend(file_questions[:remaining])

    selected_type_counts: dict[str, int] = {}
    if balanced:
        all_types = expected_types or set(available_type_counter)
        shortages = [
            (key, available_type_counter.get(key, 0))
            for key in sorted(all_types)
            if available_type_counter.get(key, 0) < int(per_type_limit)
        ]
        if shortages:
            details = "; ".join(
                f"{key}: available={available}, required={per_type_limit}"
                for key, available in shortages
            )
            raise QuestionDiscoveryError(
                "insufficient questions for balanced sampling: " + details
            )
        ordered = [
            candidate
            for key in sorted(all_types)
            for candidate in selected_by_type.get(key, [])
        ]
        if rng is not None:
            selected = [question for _, question in ordered]
            rng.shuffle(selected)
        else:
            selected = [question for _, question in sorted(ordered)]
        selected_type_counts = {
            key: len(selected_by_type.get(key, [])) for key in sorted(all_types)
        }
    else:
        if rng is not None:
            rng.shuffle(selected)
        if global_limit is not None:
            selected = selected[:global_limit]

    selected_counter = Counter(
        f"{question.category}/{question.dataset}" for question in selected
    )
    selected_file_counts = {
        key: selected_counter.get(key, 0)
        for key in file_keys
    }
    return QuestionLoadResult(
        questions=tuple(selected),
        file_counts=file_counts,
        selected_file_counts=selected_file_counts,
        files=tuple(str(path.resolve()) for path in paths),
        available_type_counts=dict(sorted(available_type_counter.items())),
        selected_type_counts=selected_type_counts,
        questions_per_type=per_type_limit,
    )


def load_questions(
    root: str | Path,
    *,
    categories: str | Iterable[str] | None = None,
    datasets: str | Iterable[str] | None = None,
    questions_per_file: int | None = None,
    question_count: int | None = None,
    shuffle_seed: int | None = None,
    questions_per_type: int | None = None,
    type_key: Callable[[QuestionRecord], str] | None = None,
    expected_type_keys: Callable[[LoadedQuestionFile], Iterable[str]] | None = None,
) -> QuestionLoadResult:
    """Discover question files and load them with optional seeded shuffling."""

    if shuffle_seed is not None and (
        isinstance(shuffle_seed, bool) or not isinstance(shuffle_seed, int)
    ):
        raise QuestionDiscoveryError("shuffle_seed must be an integer")
    paths = discover_question_files(
        root,
        categories=categories,
        datasets=datasets,
        require_complete_grid=questions_per_type is not None,
    )
    if not paths:
        raise QuestionDiscoveryError("no questions.json files matched the selection")
    return load_question_files(
        paths,
        question_count=question_count,
        questions_per_file=questions_per_file,
        shuffle=shuffle_seed is not None,
        seed=42 if shuffle_seed is None else shuffle_seed,
        questions_per_type=questions_per_type,
        type_key=type_key,
        expected_type_keys=expected_type_keys,
    )


# Backward-compatible descriptive name; runner-facing code should use
# ``QuestionRecord``.
BenchmarkQuestion = QuestionRecord


__all__ = [
    "BenchmarkQuestion",
    "LoadedQuestionFile",
    "QuestionDiscoveryError",
    "QuestionError",
    "QuestionLoadResult",
    "QuestionRecord",
    "QuestionSchemaError",
    "QUESTION_CATEGORIES",
    "STANDARD_SCHEMA",
    "STANDARD_SCHEMA_VERSION",
    "discover_question_files",
    "load_question_file",
    "load_question_files",
    "load_questions",
]
