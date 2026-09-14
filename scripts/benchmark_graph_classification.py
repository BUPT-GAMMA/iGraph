#!/usr/bin/env python3
"""Run the standalone iGraph graph-classification benchmark.

This entry point deliberately uses the in-process GraphML module only. It does
not start the frontend, HTTP/MCP services, Agent Runtime, a database, or an
authentication flow.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Sequence

try:
    import resource
except ImportError:  # pragma: no cover - resource is available on the Linux runner.
    resource = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "benchmarks"
SCHEMA_VERSION = "igraph.graph_classification_benchmark.v1"
CANONICAL_DATASET_SHA256 = {
    "hiv": "680ccc639603c2230e4e61fb2e584cecb66c38b482a467be9227d9edd115b241",
    "pcba": "d863bfd9072ff6379392d9ed4c741cf258e45a599d19d2a1c057f134eac2b8a8",
}

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_DATASET = 4
EXIT_EVALUATION = 5
EXIT_OUTPUT = 6

_DEVICE_PATTERN = re.compile(r"^(?:auto|cpu|cuda|cuda:[0-9]+)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BenchmarkFailure(Exception):
    """A user-facing benchmark failure with a stable process exit code."""

    def __init__(self, exit_code: int, stage: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)
        self.stage = stage


class BenchmarkInterrupted(BaseException):
    """Raised by signal handlers so temporary artifacts unwind cleanly."""

    def __init__(self, signum: int) -> None:
        self.signum = int(signum)
        self.exit_code = 128 + self.signum
        super().__init__(f"benchmark interrupted by signal {self.signum}")


@dataclass(frozen=True)
class _UploadRecord:
    file_token: str
    original_filename: str
    saved_path: str


class _BenchmarkSession:
    """Minimal in-memory session contract required by GraphMLDatasetStore."""

    user_id = ""

    def __init__(self, session_id: str, source_path: Path) -> None:
        self.session_id = session_id
        self._upload = _UploadRecord(
            file_token=f"{session_id}-input",
            original_filename=source_path.name,
            saved_path=str(source_path),
        )
        self.dataset_binding: dict[str, str] = {}

    def resolve_upload_by_name(self, filename: str) -> _UploadRecord | None:
        return self._upload if filename == self._upload.original_filename else None

    def bind_graph_ml_dataset(
        self,
        *,
        dataset_id: str,
        source_filename: str,
        fingerprint: str,
        dataset_profile: str,
    ) -> None:
        self.dataset_binding = {
            "dataset_id": dataset_id,
            "source_filename": source_filename,
            "fingerprint": fingerprint,
            "dataset_profile": dataset_profile,
        }

    def get_graph(self) -> None:
        return None


def _device_argument(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DEVICE_PATTERN.fullmatch(normalized):
        raise argparse.ArgumentTypeError(
            "device must be one of auto, cpu, cuda, or cuda:N"
        )
    return normalized


def _sha256_argument(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise argparse.ArgumentTypeError("expected SHA-256 must contain exactly 64 hex digits")
    return normalized


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=("hiv", "pcba"))
    parser.add_argument("path", type=Path, help="Path to the graph-classification .pt dataset")
    parser.add_argument(
        "--models",
        nargs="+",
        choices=("GCN", "GAT", "SAGE"),
        default=None,
    )
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--hidden-dim", type=int, default=8)
    parser.add_argument("--num-layers", type=int, default=2)
    parser.add_argument("--device", type=_device_argument, default="cpu")
    parser.add_argument(
        "--expected-sha256",
        type=_sha256_argument,
        default=None,
        help="Override the canonical dataset SHA-256 used for identity verification",
    )
    parser.add_argument(
        "--allow-fingerprint-mismatch",
        action="store_true",
        help="Allow non-canonical synthetic/custom data (never use for official results)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Persistent result directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args(argv)


def _default_models(dataset: str) -> list[str]:
    return ["GCN", "GAT", "SAGE"] if dataset == "hiv" else ["GCN"]


def _validate_arguments(args: argparse.Namespace) -> Path:
    source_path = args.path.expanduser().resolve()
    if not source_path.is_file():
        raise BenchmarkFailure(EXIT_DATASET, "input", f"dataset file not found: {source_path}")
    if source_path.suffix.lower() != ".pt":
        raise BenchmarkFailure(EXIT_DATASET, "input", "benchmark input must be a .pt file")
    for name in ("epochs", "batch_size", "hidden_dim"):
        if int(getattr(args, name)) < 1:
            option = name.replace("_", "-")
            raise BenchmarkFailure(EXIT_USAGE, "configuration", f"{option} must be positive")
    if not 0 <= int(args.seed) <= 2**32 - 1:
        raise BenchmarkFailure(
            EXIT_USAGE,
            "configuration",
            "seed must be between 0 and 4294967295",
        )
    if int(args.num_layers) < 2:
        raise BenchmarkFailure(
            EXIT_USAGE,
            "configuration",
            "num-layers must be at least 2 for the supported GNN backbones",
        )
    return source_path


def _git_state(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Return reproducibility metadata without making Git a runtime dependency."""
    unavailable = {
        "available": False,
        "commit": None,
        "working_tree_dirty": None,
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if commit.returncode != 0 or not commit.stdout.strip():
            return unavailable
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        return {
            "available": True,
            "commit": commit.stdout.strip(),
            "working_tree_dirty": (
                bool(status.stdout.strip()) if status.returncode == 0 else None
            ),
        }
    except (OSError, subprocess.SubprocessError):
        return unavailable


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id(dataset: str, now: datetime | None = None, token: str | None = None) -> str:
    timestamp = (now or _utc_now()).strftime("%Y%m%dT%H%M%SZ")
    suffix = token or uuid.uuid4().hex[:8]
    return f"{dataset}-{timestamp}-{suffix}"


def _result_path(output_dir: Path, run_id: str) -> Path:
    return output_dir.expanduser().resolve() / f"{run_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
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
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _output_failure_envelope(
    result: dict[str, Any],
    exc: BaseException,
) -> dict[str, Any]:
    """Return a deliberately small payload that is always strict-JSON safe."""

    previous_error = result.get("error")
    error: dict[str, Any] = {
        "stage": "output",
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if previous_error is not None:
        error["previous_error"] = repr(previous_error)

    duration = result.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, Real):
        duration = None
    elif not math.isfinite(float(duration)):
        duration = None

    return {
        "schema_version": str(result.get("schema_version", SCHEMA_VERSION)),
        "status": "failed",
        "run_id": str(result.get("run_id", "unknown")),
        "started_at": str(result.get("started_at", "")),
        "finished_at": str(result.get("finished_at", "")),
        "duration_seconds": duration,
        "result_file": None,
        "error": error,
    }


def _preflight_output_path(path: Path) -> None:
    """Verify the destination before loading a potentially large dataset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".probe",
            delete=False,
        ) as handle:
            probe_path = Path(handle.name)
            handle.write("{}\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


def _peak_memory_bytes() -> int | None:
    if resource is None:
        return None
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def _load_runtime_components() -> dict[str, Any]:
    try:
        import numpy
        import pydantic
        import torch
        import torch_geometric

        from graph_data_service.graph_ml import GraphMLModule
        from graph_data_service.graph_ml.datasets import GraphMLDatasetStore
        from graph_data_service.graph_ml.device_utils import select_best_gpu
    except (ImportError, OSError) as exc:
        raise BenchmarkFailure(
            EXIT_ENVIRONMENT,
            "environment",
            f"benchmark dependencies are unavailable: {exc}",
        ) from exc
    return {
        "numpy": numpy,
        "pydantic": pydantic,
        "torch": torch,
        "torch_geometric": torch_geometric,
        "GraphMLModule": GraphMLModule,
        "GraphMLDatasetStore": GraphMLDatasetStore,
        "select_best_gpu": select_best_gpu,
    }


def _resolve_device(requested: str, runtime: dict[str, Any]) -> str:
    torch = runtime["torch"]
    if requested == "cpu":
        return "cpu"
    if requested == "auto":
        return str(runtime["select_best_gpu"]())
    if not torch.cuda.is_available():
        raise BenchmarkFailure(
            EXIT_ENVIRONMENT,
            "device",
            f"device={requested} was requested, but CUDA is unavailable",
        )
    index = 0 if requested == "cuda" else int(requested.split(":", 1)[1])
    if index >= int(torch.cuda.device_count()):
        raise BenchmarkFailure(
            EXIT_ENVIRONMENT,
            "device",
            f"CUDA device index {index} is out of range",
        )
    return f"cuda:{index}"


def _environment_state(runtime: dict[str, Any], resolved_device: str) -> dict[str, Any]:
    torch = runtime["torch"]
    python_executable = Path(sys.executable).resolve()
    raw_conda_prefix = str(os.getenv("CONDA_PREFIX") or "").strip()
    conda_prefix = Path(raw_conda_prefix).resolve() if raw_conda_prefix else None
    is_conda_python = bool(
        conda_prefix is not None and python_executable.is_relative_to(conda_prefix)
    )
    conda_environment = None
    if is_conda_python and conda_prefix is not None:
        conda_environment = os.getenv("CONDA_DEFAULT_ENV") or conda_prefix.name
    cuda_available = bool(torch.cuda.is_available())
    cuda_devices = []
    if cuda_available:
        cuda_devices = [
            str(torch.cuda.get_device_name(index))
            for index in range(int(torch.cuda.device_count()))
        ]
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": str(python_executable),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "numpy": runtime["numpy"].__version__,
        "pydantic": runtime["pydantic"].__version__,
        "torch": torch.__version__,
        "torch_geometric": runtime["torch_geometric"].__version__,
        "torch_threads": int(torch.get_num_threads()),
        "cuda_available": cuda_available,
        "cuda_runtime": torch.version.cuda,
        "cuda_devices": cuda_devices,
        "resolved_device": resolved_device,
        "conda_environment": conda_environment,
        "conda_prefix": str(conda_prefix) if is_conda_python else None,
        "is_conda_python": is_conda_python,
        "launcher": os.getenv("IGRAPH_BENCHMARK_LAUNCHER") or "direct-python",
    }


def _progress(event: dict[str, Any]) -> None:
    message = str(event.get("message") or event.get("kind") or "working")
    print(f"[benchmark] {message}", file=sys.stderr, flush=True)


def _verify_dataset_fingerprint(args: argparse.Namespace, dataset: dict[str, Any]) -> None:
    actual = str(dataset.get("fingerprint") or "").strip().lower()
    expected = args.expected_sha256 or CANONICAL_DATASET_SHA256[args.dataset]
    verified = bool(actual) and actual == expected
    dataset["expected_sha256"] = expected
    dataset["fingerprint_verified"] = verified
    dataset["fingerprint_mismatch_allowed"] = bool(args.allow_fingerprint_mismatch)
    if not verified and not args.allow_fingerprint_mismatch:
        raise BenchmarkFailure(
            EXIT_DATASET,
            "dataset_fingerprint",
            "dataset SHA-256 does not match the official "
            f"{args.dataset.upper()} benchmark (expected {expected}, got {actual or 'missing'}); "
            "use --allow-fingerprint-mismatch only for synthetic/custom smoke data",
        )


def _verify_metrics(dataset: str, metrics: Any) -> None:
    if not isinstance(metrics, dict):
        raise BenchmarkFailure(EXIT_EVALUATION, "metrics", "training returned no metrics object")
    metric_suffix = "roc_auc" if dataset == "hiv" else "ap"
    for split in ("val", "test"):
        key = f"{split}_{metric_suffix}"
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise BenchmarkFailure(
                EXIT_EVALUATION,
                "metrics",
                f"{key} must be a finite number, got {value!r}",
            )
        if not 0.0 <= float(value) <= 1.0:
            raise BenchmarkFailure(
                EXIT_EVALUATION,
                "metrics",
                f"{key} must be within [0, 1], got {value!r}",
            )
        if dataset == "pcba":
            count_key = f"{split}_valid_tasks"
            count = metrics.get(count_key)
            if isinstance(count, bool) or not isinstance(count, Integral) or int(count) < 1:
                raise BenchmarkFailure(
                    EXIT_EVALUATION,
                    "metrics",
                    f"{count_key} must be a positive integer, got {count!r}",
                )


def _verify_model_outputs(
    dataset: str,
    model: str,
    inference_index: int,
    inferred: Any,
    architecture: Any,
) -> None:
    if not isinstance(inferred, dict) or inferred.get("success") is not True:
        raise BenchmarkFailure(EXIT_EVALUATION, f"verify:{model}", "inference was not successful")
    predictions = inferred.get("predictions")
    if (
        inferred.get("returned_count") != 1
        or not isinstance(predictions, list)
        or len(predictions) != 1
        or not isinstance(predictions[0], dict)
        or predictions[0].get("graph_index") != inference_index
    ):
        raise BenchmarkFailure(
            EXIT_EVALUATION,
            f"verify:{model}",
            "single-graph inference returned an inconsistent result",
        )
    prediction = predictions[0]
    if dataset == "hiv":
        probabilities = [prediction.get("probability")]
    else:
        top_predictions = prediction.get("top_predictions")
        if not isinstance(top_predictions, list) or not top_predictions:
            raise BenchmarkFailure(
                EXIT_EVALUATION,
                f"verify:{model}",
                "PCBA inference returned no top_predictions",
            )
        probabilities = [
            item.get("probability") if isinstance(item, dict) else None
            for item in top_predictions
        ]
    if any(
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
        for value in probabilities
    ):
        raise BenchmarkFailure(
            EXIT_EVALUATION,
            f"verify:{model}",
            "inference probabilities must be finite numbers within [0, 1]",
        )
    if (
        not isinstance(architecture, dict)
        or architecture.get("success") is not True
        or architecture.get("task") != "graph_classification"
    ):
        raise BenchmarkFailure(
            EXIT_EVALUATION,
            f"verify:{model}",
            "model architecture verification was not successful",
        )


def _execute(args: argparse.Namespace, result: dict[str, Any]) -> None:
    source_path = _validate_arguments(args)
    runtime = _load_runtime_components()
    resolved_device = _resolve_device(args.device, runtime)
    models = list(args.models or _default_models(args.dataset))

    result["config"].update(
        {
            "models": models,
            "resolved_device": resolved_device,
            "source_path": str(source_path),
        }
    )
    result["environment"] = _environment_state(runtime, resolved_device)

    session_id = f"benchmark-{result['run_id']}"
    session = _BenchmarkSession(session_id, source_path)
    module = None
    with tempfile.TemporaryDirectory(prefix="igraph-graph-classification-") as temp_root:
        artifact_root = Path(temp_root) / "artifacts"
        try:
            dataset_store = runtime["GraphMLDatasetStore"](max_resident_datasets=1)

            def get_session(requested_id: str) -> _BenchmarkSession:
                if requested_id != session_id:
                    raise KeyError(requested_id)
                return session

            module = runtime["GraphMLModule"](
                get_session=get_session,
                artifact_root=str(artifact_root),
                dataset_store=dataset_store,
            )
        except Exception as exc:
            raise BenchmarkFailure(
                EXIT_ENVIRONMENT,
                "initialization",
                f"unable to initialize GraphML runtime: {exc}",
            ) from exc

        try:
            load_started = time.perf_counter()
            try:
                loaded = module.load_graph_classification_dataset(
                    session_id,
                    {"filename": source_path.name, "dataset_profile": args.dataset},
                    progress_callback=_progress,
                )
            except Exception as exc:
                raise BenchmarkFailure(EXIT_DATASET, "dataset_load", str(exc)) from exc
            dataset = dict(loaded["dataset"])
            dataset["load_seconds"] = time.perf_counter() - load_started
            result["dataset"] = dataset
            result["readiness"] = loaded["readiness"]

            bundle = dataset_store.resolve_for_session(session_id, dataset["dataset_id"])
            dataset.update(
                {
                    "source_filename": bundle.source_filename,
                    "source_size_bytes": int(bundle.metadata.get("source_size_bytes", 0)),
                    "source_mtime_ns": int(bundle.metadata.get("source_mtime_ns", 0)),
                }
            )
            _verify_dataset_fingerprint(args, dataset)
            test_indices = list(bundle.split_indices.get("test") or [])
            if not test_indices:
                raise BenchmarkFailure(
                    EXIT_DATASET,
                    "dataset_load",
                    "validated dataset has an empty test split",
                )
            inference_index = int(test_indices[0])

            for model in models:
                train_config = {
                    "epochs": args.epochs,
                    "lr": 0.001,
                    "weight_decay": 0.0,
                    "seed": args.seed,
                    "early_stop_patience": 0,
                    "batch_size": args.batch_size,
                    "num_workers": 0,
                }
                model_config = {
                    "device": resolved_device,
                    "node_embedding_dim": args.hidden_dim,
                    "hidden_dim": args.hidden_dim,
                    "embedding_dim": args.hidden_dim,
                    "num_layers": args.num_layers,
                    "dropout": 0.0,
                }
                print(f"[benchmark] training {model}", file=sys.stderr, flush=True)
                if resolved_device.startswith("cuda"):
                    runtime["torch"].cuda.reset_peak_memory_stats(
                        runtime["torch"].device(resolved_device)
                    )
                train_started = time.perf_counter()
                try:
                    trained = module.train(
                        session_id,
                        {
                            "task": "graph_classification",
                            "model": model,
                            "data": {"dataset_id": dataset["dataset_id"]},
                            "train_config": train_config,
                            "algo_config": model_config,
                        },
                        progress_callback=_progress,
                    )
                except Exception as exc:
                    raise BenchmarkFailure(
                        EXIT_EVALUATION,
                        f"train:{model}",
                        str(exc),
                    ) from exc
                train_seconds = time.perf_counter() - train_started
                if not isinstance(trained, dict):
                    raise BenchmarkFailure(
                        EXIT_EVALUATION,
                        f"train:{model}",
                        "training returned an invalid result",
                    )
                _verify_metrics(args.dataset, trained.get("metrics"))

                try:
                    inferred = module.infer(
                        session_id,
                        {
                            "model_id": trained["model_id"],
                            "query": {"graph_indices": [inference_index]},
                            "infer_config": {
                                "device": resolved_device,
                                "result_limit": 1,
                                "top_k_tasks": 5,
                            },
                        },
                    )
                    architecture = module.visualize_model_architecture(
                        session_id,
                        {"model_id": trained["model_id"]},
                    )
                except Exception as exc:
                    raise BenchmarkFailure(
                        EXIT_EVALUATION,
                        f"verify:{model}",
                        str(exc),
                    ) from exc
                _verify_model_outputs(
                    args.dataset,
                    model,
                    inference_index,
                    inferred,
                    architecture,
                )

                result["runs"].append(
                    {
                        "model": model,
                        "model_id": trained["model_id"],
                        "model_config": model_config,
                        "train_config": train_config,
                        "metrics": trained["metrics"],
                        "train_and_full_eval_seconds": train_seconds,
                        "cuda_peak_memory_allocated_bytes": (
                            int(
                                runtime["torch"].cuda.max_memory_allocated(
                                    runtime["torch"].device(resolved_device)
                                )
                            )
                            if resolved_device.startswith("cuda")
                            else None
                        ),
                        "inference": {
                            "graph_index": inference_index,
                            "returned_count": inferred["returned_count"],
                            "query": inferred.get("query"),
                            "predictions": inferred["predictions"],
                        },
                        "architecture": architecture,
                    }
                )
        finally:
            if module is not None:
                try:
                    module.release_session(session_id)
                except Exception:
                    pass


def _base_result(args: argparse.Namespace, run_id: str, started_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "result_file": None,
        "git": _git_state(),
        "environment": None,
        "config": {
            "dataset": args.dataset,
            "source_path": str(args.path.expanduser()),
            "models": list(args.models) if args.models else _default_models(args.dataset),
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "seed": args.seed,
            "hidden_dim": args.hidden_dim,
            "num_layers": args.num_layers,
            "requested_device": args.device,
            "resolved_device": None,
            "expected_sha256": (
                args.expected_sha256 or CANONICAL_DATASET_SHA256[args.dataset]
            ),
            "allow_fingerprint_mismatch": bool(args.allow_fingerprint_mismatch),
        },
        "dataset": None,
        "readiness": None,
        "runs": [],
        "resources": {"process_peak_memory_bytes": None},
        "artifacts_retained": False,
        "error": None,
    }


def _signal_handler(signum: int, _frame: Any) -> None:
    raise BenchmarkInterrupted(signum)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    started_at = _utc_now()
    started_clock = time.perf_counter()
    run_id = _new_run_id(args.dataset, now=started_at)
    output_path = _result_path(args.output_dir, run_id)
    result = _base_result(args, run_id, started_at)
    result["result_file"] = str(output_path)
    exit_code = EXIT_OK

    # Fail before loading a large dataset if the requested result destination is
    # not writable, without leaving a stale status=running result behind.
    try:
        _preflight_output_path(output_path)
    except Exception as exc:
        result["status"] = "failed"
        result["finished_at"] = _utc_now().isoformat()
        result["duration_seconds"] = time.perf_counter() - started_clock
        result["result_file"] = None
        result["error"] = {
            "stage": "output",
            "type": type(exc).__name__,
            "message": str(exc),
        }
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
        print(f"benchmark result could not be created: {exc}", file=sys.stderr)
        return EXIT_OUTPUT

    previous_handlers: dict[signal.Signals, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _signal_handler)

    try:
        _execute(args, result)
        result["status"] = "success"
    except BenchmarkFailure as exc:
        exit_code = exc.exit_code
        result["status"] = "failed"
        result["error"] = {
            "stage": exc.stage,
            "type": type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
            "message": str(exc),
        }
    except BenchmarkInterrupted as exc:
        exit_code = exc.exit_code
        result["status"] = "interrupted"
        result["error"] = {
            "stage": "signal",
            "type": type(exc).__name__,
            "message": str(exc),
        }
    except KeyboardInterrupt:
        exit_code = 130
        result["status"] = "interrupted"
        result["error"] = {
            "stage": "signal",
            "type": "KeyboardInterrupt",
            "message": "benchmark interrupted by user",
        }
    except Exception as exc:
        exit_code = EXIT_EVALUATION
        result["status"] = "failed"
        result["error"] = {
            "stage": "internal",
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)

    result["finished_at"] = _utc_now().isoformat()
    result["duration_seconds"] = time.perf_counter() - started_clock
    result["resources"]["process_peak_memory_bytes"] = _peak_memory_bytes()
    try:
        _atomic_write_json(output_path, result)
    except Exception as exc:
        exit_code = EXIT_OUTPUT
        result = _output_failure_envelope(result, exc)
        print(f"benchmark result could not be persisted; no result file was saved: {exc}", file=sys.stderr)

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    )
    if exit_code == EXIT_OK:
        print(f"[benchmark] result saved to {output_path}", file=sys.stderr)
    elif result["result_file"] is None:
        print(f"[benchmark] failed with exit code {exit_code}; no result file was saved", file=sys.stderr)
    else:
        print(
            f"[benchmark] failed with exit code {exit_code}; result: {output_path}",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
