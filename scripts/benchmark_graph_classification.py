#!/usr/bin/env python3
"""Run a reproducible real-data graph-classification benchmark via tool dispatch."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from graph_data_service.app import GraphDataService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=("hiv", "pcba"))
    parser.add_argument("path", type=Path)
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
    return parser.parse_args()


def _git_state() -> dict[str, Any]:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
    )
    return {"commit": commit, "working_tree_dirty": dirty}


def _peak_memory_bytes() -> int:
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


async def _dispatch(
    service: GraphDataService,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    result = await service.dispatch_tool(session_id, tool_name, arguments)
    if not isinstance(result, dict) or result.get("success") is False:
        raise RuntimeError(f"{tool_name} 调用失败: {result}")
    return result


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source_path = args.path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.suffix.lower() != ".pt":
        raise ValueError("benchmark 输入必须是 .pt 文件")
    if args.epochs < 1 or args.batch_size < 1 or args.hidden_dim < 1:
        raise ValueError("epochs、batch-size 和 hidden-dim 必须为正整数")
    if args.num_layers < 1:
        raise ValueError("num-layers 必须为正整数")

    models = args.models or (["GCN", "GAT", "SAGE"] if args.dataset == "hiv" else ["GCN"])
    session_id = f"benchmark-{args.dataset}"
    with tempfile.TemporaryDirectory(prefix="igraph-graph-classification-") as temp_root:
        old_upload_root = os.environ.get("GRAPH_DATA_UPLOAD_DIR")
        old_artifact_root = os.environ.get("GRAPH_ML_ARTIFACT_ROOT")
        os.environ["GRAPH_DATA_UPLOAD_DIR"] = str(Path(temp_root) / "sessions")
        os.environ["GRAPH_ML_ARTIFACT_ROOT"] = str(Path(temp_root) / "artifacts")
        service = None
        try:
            async def unused_text_graph_llm(_prompt: str) -> str:
                raise AssertionError("benchmark 不应调用文本转图模型")

            service = GraphDataService(text_graph_llm_factory=unused_text_graph_llm)
            session = service.get_or_create(session_id)
            session.register_upload(
                f"benchmark-{args.dataset}-upload",
                source_path.name,
                str(source_path),
            )

            load_started = time.perf_counter()
            loaded = await _dispatch(
                service,
                session_id,
                "load_graph_classification_dataset",
                {"filename": source_path.name, "dataset_profile": args.dataset},
            )
            load_seconds = time.perf_counter() - load_started
            dataset = dict(loaded["dataset"])
            dataset["load_seconds"] = load_seconds

            runs = []
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
                algo_config = {
                    "device": "cpu",
                    "node_embedding_dim": args.hidden_dim,
                    "hidden_dim": args.hidden_dim,
                    "embedding_dim": args.hidden_dim,
                    "num_layers": args.num_layers,
                    "dropout": 0.0,
                }
                train_started = time.perf_counter()
                trained = await _dispatch(
                    service,
                    session_id,
                    "train",
                    {
                        "task": "graph_classification",
                        "model": model,
                        "data": {"dataset_id": dataset["dataset_id"]},
                        "train_config": train_config,
                        "algo_config": algo_config,
                    },
                )
                train_seconds = time.perf_counter() - train_started
                inferred = await _dispatch(
                    service,
                    session_id,
                    "infer",
                    {
                        "model_id": trained["model_id"],
                        "query": {"graph_indices": [0]},
                        "infer_config": {
                            "device": "cpu",
                            "result_limit": 1,
                            "top_k_tasks": 5,
                        },
                    },
                )
                architecture = await _dispatch(
                    service,
                    session_id,
                    "visualize_model_architecture",
                    {"model_id": trained["model_id"]},
                )
                runs.append(
                    {
                        "model": model,
                        "model_id": trained["model_id"],
                        "model_config": algo_config,
                        "train_config": train_config,
                        "device": "cpu",
                        "metrics": trained["metrics"],
                        "train_and_full_eval_seconds": train_seconds,
                        "peak_memory_bytes": _peak_memory_bytes(),
                        "infer_returned_count": inferred["returned_count"],
                        "architecture_task": architecture["task"],
                    }
                )

            return {
                "git": _git_state(),
                "dataset": dataset,
                "readiness": loaded["readiness"],
                "runs": runs,
            }
        finally:
            if service is not None:
                service.delete_session(session_id)
                service._process_pool.shutdown(wait=False, cancel_futures=True)
            if old_upload_root is None:
                os.environ.pop("GRAPH_DATA_UPLOAD_DIR", None)
            else:
                os.environ["GRAPH_DATA_UPLOAD_DIR"] = old_upload_root
            if old_artifact_root is None:
                os.environ.pop("GRAPH_ML_ARTIFACT_ROOT", None)
            else:
                os.environ["GRAPH_ML_ARTIFACT_ROOT"] = old_artifact_root


def main() -> int:
    result = asyncio.run(_run(_arguments()))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
