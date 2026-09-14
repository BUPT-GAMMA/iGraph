"""Offline command-mode fixture that exercises the virtual graph-ML broker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np


LP_TRAIN = "structural_node_embedding_link_prediction_train"
LP_INFER = "structural_node_embedding_link_prediction_infer"


def _strict_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _run_tool(tool_path: Path, arguments: list[str]) -> dict:
    completed = subprocess.run(
        [sys.executable, str(tool_path), *arguments],
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
        timeout=15,
    )
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip()[-1000:]
        raise RuntimeError(
            f"virtual graph-ML tool exited with {completed.returncode}: {diagnostic}"
        )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise TypeError("virtual graph-ML tool result must be an object")
    return result


def main() -> int:
    envelope = json.loads(sys.stdin.read())
    if not isinstance(envelope, dict) or str(envelope.get("task_type", "")).upper() != "LP":
        raise ValueError("fake agent accepts only LP task envelopes")

    tool_value = os.environ.get("IGRAPH_GRAPH_ML_TOOL", "")
    tool_path = Path(tool_value).expanduser().resolve()
    if not tool_value or not tool_path.is_file():
        raise FileNotFoundError("IGRAPH_GRAPH_ML_TOOL does not identify a tool file")

    workspace = Path.cwd()
    features_path = workspace / "x.npy"
    edge_path = workspace / "edge_index.npy"
    query_path = workspace / "query_edge_index.npy"
    model_path = workspace / "virtual_lp_model.json"

    np.save(features_path, np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float))
    np.save(edge_path, np.array([[0], [1]], dtype=int))
    np.save(query_path, np.array([[1], [0]], dtype=int))

    model_state = _run_tool(
        tool_path,
        [
            LP_TRAIN,
            "--node_features",
            str(features_path),
            "--edge_index",
            str(edge_path),
        ],
    )
    model_path.write_text(_strict_json(model_state) + "\n", encoding="utf-8")
    inference = _run_tool(
        tool_path,
        [
            LP_INFER,
            "--model_state",
            str(model_path),
            "--node_features",
            str(features_path),
            "--edge_index",
            str(edge_path),
            "--query_edge_index",
            str(query_path),
        ],
    )
    predictions = inference.get("predictions")
    if not isinstance(predictions, list) or len(predictions) != 1:
        raise ValueError("virtual graph-ML inference returned an invalid prediction")

    response = {
        "answer": {"has_edge": bool(predictions[0])},
        "reasoning": ["virtual_link_prediction_train", "virtual_link_prediction_infer"],
    }
    sys.stdout.write(_strict_json(response) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(
            _strict_json(
                {
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                }
            )
            + "\n"
        )
        raise SystemExit(1)
