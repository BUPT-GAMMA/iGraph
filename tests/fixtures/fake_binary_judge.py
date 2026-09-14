"""Offline binary Judge adapter used only for protocol/smoke tests.

It deliberately makes no network or model calls. A structurally valid runner
envelope with a non-null final answer receives score 1; malformed input exits
non-zero so the benchmark's evaluation-error path can be tested separately.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    envelope = json.loads(sys.stdin.read())
    required = {
        "schema",
        "question_id",
        "original_question",
        "ground_truth",
        "final_answer",
        "rubric",
    }
    if not isinstance(envelope, dict) or not required.issubset(envelope):
        raise ValueError("invalid benchmark Judge envelope")
    score = 0 if envelope["final_answer"] is None else 1
    sys.stdout.write(json.dumps({"score": score}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        raise SystemExit(2)
