"""Validate line and branch percentages from a coverage.py JSON report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--lines", type=float, required=True)
    parser.add_argument("--branches", type=float, required=True)
    args = parser.parse_args()

    totals = json.loads(args.report.read_text(encoding="utf-8"))["totals"]
    line_rate = percentage(int(totals["covered_lines"]), int(totals["num_statements"]))
    branch_rate = percentage(int(totals["covered_branches"]), int(totals["num_branches"]))
    print(
        "Python coverage: "
        f"lines={line_rate:.2f}% (required {args.lines:.2f}%), "
        f"branches={branch_rate:.2f}% (required {args.branches:.2f}%)"
    )

    failures = []
    if line_rate < args.lines:
        failures.append(f"line coverage {line_rate:.2f}% is below {args.lines:.2f}%")
    if branch_rate < args.branches:
        failures.append(f"branch coverage {branch_rate:.2f}% is below {args.branches:.2f}%")
    if failures:
        print("Coverage gate failed: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
