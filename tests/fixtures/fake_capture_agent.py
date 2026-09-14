"""Network-free agent adapter for real-dataset materialization smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    envelope = json.loads(sys.stdin.read())
    if not isinstance(envelope, dict):
        raise ValueError("task envelope must be an object")
    dataset_path = Path(str(envelope.get("dataset_path", "")))
    if not dataset_path.is_file():
        raise FileNotFoundError("materialized dataset is unavailable")
    response = {
        "status": "success",
        "final_answer": {"offline_smoke": True},
        "reasoning": ["verified that the attempt-private dataset exists"],
        "tool_calls": [],
        "transcript": [
            {"role": "assistant", "content": "offline materialization smoke test"}
        ],
    }
    sys.stdout.write(
        json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(2)
