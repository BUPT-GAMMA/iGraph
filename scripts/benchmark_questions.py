#!/usr/bin/env python3
"""Headless CLI for parallel question-based iGraph benchmark evaluation."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark_platform.questions import (  # noqa: E402
    QuestionDiscoveryError,
    QuestionSchemaError,
    load_questions,
)
from benchmark_platform.runner import (  # noqa: E402
    RunnerConfig,
    benchmark_type_key,
    expected_benchmark_type_keys,
    run_questions,
)


RECEIPT_SCHEMA = "igraph.benchmark.receipt.v1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_DATA = 4
EXIT_EVALUATION = 5
EXIT_INTERNAL = 6
EXIT_INTERRUPTED = 130

DEFAULT_QUESTIONS_ROOT = Path(
    os.environ.get("IGRAPH_QUESTIONS_ROOT", str(PROJECT_ROOT / "questions"))
)
DEFAULT_DATASET_ROOT = Path(
    os.environ.get("IGRAPH_DATASET_ROOT", str(PROJECT_ROOT / "datasets"))
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "question-benchmarks"


class CLIUsageError(ValueError):
    """An argparse failure that can be returned as a JSON receipt."""


class ReceiptArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def _positive_int(raw: str) -> int:
    value = _non_negative_int(raw)
    if value == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return value


def _non_negative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return value


def _agent_command(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            "must be a JSON array containing the agent command and its arguments"
        ) from exc
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(part, str) or not part for part in value)
    ):
        raise argparse.ArgumentTypeError(
            "must be a non-empty JSON array of non-empty strings"
        )
    return tuple(value)


def _environment_name(raw: str) -> str:
    value = raw.strip()
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise argparse.ArgumentTypeError("must be an environment variable name")
    return value


def _split_filter_values(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    flattened = [part.strip() for value in values for part in value.split(",")]
    if any(not part for part in flattened):
        raise CLIUsageError("category and dataset filters cannot contain empty values")
    return flattened


def add_questions_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the questions command arguments to an existing subparser."""

    parser.add_argument(
        "--questions-root",
        type=Path,
        default=DEFAULT_QUESTIONS_ROOT,
        help="root containing category/dataset/questions.json trees",
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=DEFAULT_DATASET_ROOT,
        help=(
            "directory containing <dataset>.pt base graphs; ML/prediction "
            "masked inputs are resolved beside each questions.json"
        ),
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        metavar="CATEGORY",
        help="category filters; values may also be comma-separated",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        metavar="DATASET",
        help="dataset filters; values may also be comma-separated",
    )
    parser.add_argument(
        "--question-count",
        type=_non_negative_int,
        default=None,
        help="global question cap; 0 selects all (default: 1 to limit LLM use)",
    )
    parser.add_argument(
        "--questions-per-file",
        type=_non_negative_int,
        default=0,
        help="per-file question cap; 0 selects all",
    )
    parser.add_argument(
        "--questions-per-type",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "select exactly N questions from every selected formal benchmark cell; "
            "cannot be combined with positive global or per-file caps"
        ),
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help=(
            "enable deterministic seeded sampling before applying caps; in balanced "
            "mode, sample within each selected cell and then shuffle globally"
        ),
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="sampling seed used only with --shuffle"
    )
    parser.add_argument(
        "--task-parallelism",
        type=_positive_int,
        default=4,
        help="number of questions processed concurrently",
    )
    parser.add_argument(
        "--intra-task-parallelism",
        type=_positive_int,
        default=1,
        help="maximum concurrent attempts for each question",
    )
    parser.add_argument(
        "--attempts-per-question",
        type=_positive_int,
        default=1,
        help="independent attempts made for each selected question",
    )

    execution = parser.add_mutually_exclusive_group(required=True)
    execution.add_argument(
        "--agent-command-json",
        type=_agent_command,
        metavar="JSON_ARRAY",
        help='agent argv encoded as JSON, e.g. ["python","agent.py"]',
    )
    execution.add_argument(
        "--replay-file",
        type=Path,
        help=(
            "JSON or JSONL agent responses used without invoking an agent; "
            "--evaluate may still invoke the configured judge"
        ),
    )
    execution.add_argument(
        "--validate-only",
        action="store_true",
        help="validate questions and dataset paths without running an agent",
    )
    parser.add_argument(
        "--recover-cleanup-failures",
        action="store_true",
        help=(
            "in replay mode only, recover a captured non-empty answer whose "
            "sole recorded failure is ContainerCleanupError; the original "
            "failure remains recorded as a warning"
        ),
    )
    parser.add_argument(
        "--reuse-successful-judge-results",
        action="store_true",
        help=(
            "in replay evaluation only, reuse prior successful binary Judge "
            "results and invoke the configured Judge only for missing or "
            "unsuccessful prior decisions"
        ),
    )
    parser.add_argument(
        "--agent-env",
        action="append",
        type=_environment_name,
        default=[],
        metavar="NAME",
        help=(
            "inherit one named environment variable in the agent process; "
            "repeat for API credentials instead of putting values in argv"
        ),
    )
    parser.add_argument(
        "--evaluate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "evaluate captured tool paths and final answers (default: disabled; "
            "--evaluate requires --judge-command-json)"
        ),
    )
    parser.add_argument(
        "--judge-command-json",
        type=_agent_command,
        metavar="JSON_ARRAY",
        help=(
            "binary LLM-as-a-Judge adapter argv encoded as JSON; it receives "
            "question, ground truth, and final response on stdin"
        ),
    )
    parser.add_argument(
        "--judge-env",
        action="append",
        type=_environment_name,
        default=[],
        metavar="NAME",
        help=(
            "inherit one named environment variable only in the judge process; "
            "repeat for API credentials"
        ),
    )
    parser.add_argument(
        "--judge-timeout-seconds",
        type=_positive_float,
        default=600.0,
        help="per-judge timeout in seconds",
    )

    parser.add_argument(
        "--virtual-graph-ml",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "enable the attempt-scoped virtual graph-ML broker and bundled "
            "tool (default: enabled); disabling does not supply a real ML tool"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--timeout",
        "--timeout-seconds",
        dest="timeout_seconds",
        type=_positive_float,
        default=600.0,
        help="per-agent-attempt timeout in seconds",
    )
    parser.add_argument(
        "--tolerance",
        "--numeric-tolerance",
        dest="numeric_tolerance",
        type=_non_negative_float,
        default=1e-6,
        help="absolute and relative tolerance for numeric ground truth",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = ReceiptArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    questions = subcommands.add_parser(
        "questions",
        help="evaluate question datasets with inter- and intra-task parallelism",
    )
    add_questions_arguments(questions)
    return parser


def _receipt(
    *,
    status: str,
    exit_code: int,
    run_id: str | None = None,
    result_file: str | None = None,
    question_count: int | None = None,
    available_question_count: int | None = None,
    summary: Any = None,
    sampling: Any = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "command": "questions",
        "status": status,
        "exit_code": exit_code,
        "run_id": run_id,
        "result_file": result_file,
        "question_count": question_count,
        "available_question_count": available_question_count,
        "summary": summary,
        "sampling": sampling,
    }
    if error is not None:
        value["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
    return value


def _emit_receipt(value: dict[str, Any]) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
        flush=True,
    )


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.validate_only and args.evaluate:
        raise CLIUsageError("--validate-only cannot be combined with --evaluate")
    if args.evaluate and args.judge_command_json is None:
        raise CLIUsageError("--evaluate requires --judge-command-json")
    if not args.evaluate and args.judge_command_json is not None:
        raise CLIUsageError("--judge-command-json requires --evaluate")
    if not args.evaluate and args.judge_env:
        raise CLIUsageError("--judge-env requires --evaluate")
    if args.recover_cleanup_failures and args.replay_file is None:
        raise CLIUsageError("--recover-cleanup-failures requires --replay-file")
    if args.reuse_successful_judge_results and (
        args.replay_file is None or not args.evaluate
    ):
        raise CLIUsageError(
            "--reuse-successful-judge-results requires --replay-file and --evaluate"
        )
    if args.questions_per_type is not None:
        if args.question_count not in (None, 0):
            raise CLIUsageError(
                "--questions-per-type cannot be combined with a positive "
                "--question-count"
            )
        if args.questions_per_file != 0:
            raise CLIUsageError(
                "--questions-per-type cannot be combined with a positive "
                "--questions-per-file"
            )
    categories = _split_filter_values(args.categories)
    datasets = _split_filter_values(args.datasets)
    balanced = args.questions_per_type is not None
    effective_question_count = (
        None
        if balanced
        else 1
        if args.question_count is None
        else args.question_count
    )
    selection = load_questions(
        args.questions_root,
        categories=categories,
        datasets=datasets,
        questions_per_file=(
            None
            if balanced or args.questions_per_file == 0
            else args.questions_per_file
        ),
        question_count=(
            None
            if balanced or effective_question_count == 0
            else effective_question_count
        ),
        shuffle_seed=args.seed if args.shuffle else None,
        questions_per_type=args.questions_per_type,
        type_key=benchmark_type_key if balanced else None,
        expected_type_keys=expected_benchmark_type_keys if balanced else None,
    )
    sampling: dict[str, Any]
    if balanced:
        sampling = {
            "mode": "balanced_benchmark_type",
            "questions_per_type": args.questions_per_type,
            "type_count": len(selection.selected_type_counts),
            "available_type_counts": dict(selection.available_type_counts),
            "selected_type_counts": dict(selection.selected_type_counts),
            "shuffle": bool(args.shuffle),
            "seed": args.seed if args.shuffle else None,
        }
    else:
        sampling = {
            "mode": "standard",
            "question_count": effective_question_count,
            "questions_per_file": args.questions_per_file,
            "shuffle": bool(args.shuffle),
            "seed": args.seed if args.shuffle else None,
        }
    config = RunnerConfig(
        output_dir=args.output_dir,
        dataset_root=args.dataset_root,
        questions_root=args.questions_root,
        task_parallelism=args.task_parallelism,
        intra_task_parallelism=args.intra_task_parallelism,
        attempts_per_question=args.attempts_per_question,
        timeout_seconds=args.timeout_seconds,
        numeric_tolerance=args.numeric_tolerance,
        judge_timeout_seconds=args.judge_timeout_seconds,
        virtual_graph_ml=args.virtual_graph_ml,
        evaluate=args.evaluate,
        agent_environment_names=tuple(args.agent_env),
        judge_environment_names=tuple(args.judge_env),
        agent_command=args.agent_command_json,
        judge_command=args.judge_command_json,
        replay_file=args.replay_file,
        recover_cleanup_failures=args.recover_cleanup_failures,
        reuse_successful_judge_results=args.reuse_successful_judge_results,
        validate_only=args.validate_only,
        selection_metadata=sampling,
    )
    result, exit_code = run_questions(selection.questions, config)
    return (
        _receipt(
            status=str(result.get("status", "unknown")),
            exit_code=exit_code,
            run_id=str(result.get("run_id")) if result.get("run_id") else None,
            result_file=(
                str(result.get("result_file")) if result.get("result_file") else None
            ),
            question_count=selection.question_count,
            available_question_count=sum(selection.file_counts.values()),
            summary=result.get("summary"),
            sampling=result.get("selection", {}).get("sampling", sampling),
        ),
        exit_code,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        # ``main.py`` may dispatch after consuming the subcommand, while direct
        # script execution retains it.  Supporting both keeps one parser and
        # one receipt boundary for the two entry points.
        if arguments and arguments[0] != "questions" and arguments[0].startswith("-"):
            arguments.insert(0, "questions")
        args = build_parser().parse_args(arguments)
        receipt, exit_code = _execute(args)
    except CLIUsageError as exc:
        exit_code = EXIT_USAGE
        receipt = _receipt(status="usage_error", exit_code=exit_code, error=exc)
    except (QuestionDiscoveryError, QuestionSchemaError, ValueError) as exc:
        exit_code = EXIT_INPUT
        receipt = _receipt(status="input_error", exit_code=exit_code, error=exc)
    except FileNotFoundError as exc:
        exit_code = EXIT_DATA
        receipt = _receipt(status="data_error", exit_code=exit_code, error=exc)
    except KeyboardInterrupt as exc:
        exit_code = EXIT_INTERRUPTED
        receipt = _receipt(status="interrupted", exit_code=exit_code, error=exc)
    except OSError as exc:
        exit_code = EXIT_INTERNAL
        receipt = _receipt(status="io_error", exit_code=exit_code, error=exc)
    except Exception as exc:  # pragma: no cover - defensive receipt boundary.
        exit_code = EXIT_INTERNAL
        receipt = _receipt(status="internal_error", exit_code=exit_code, error=exc)
    _emit_receipt(receipt)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
