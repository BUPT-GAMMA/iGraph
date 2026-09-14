#!/usr/bin/env python3
"""Command-line dispatcher for the standalone iGraph benchmark runners."""

from __future__ import annotations

import sys
from typing import Sequence


_TOP_LEVEL_HELP = """usage: main.py COMMAND [options]

Headless iGraph benchmark platform.

commands:
  questions             parallel question/tool-trajectory benchmark
  graph-classification  legacy HIV/PCBA training benchmark

Backward compatibility: `main.py hiv ...` and `main.py pcba ...` are routed
to the graph-classification benchmark.
"""


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(_TOP_LEVEL_HELP)
        return 0

    command, rest = arguments[0], arguments[1:]
    if command == "questions":
        from scripts.benchmark_questions import main as questions_main

        return questions_main(rest)
    if command == "graph-classification":
        from scripts.benchmark_graph_classification import main as graph_main

        return graph_main(rest)
    if command in {"hiv", "pcba"}:
        from scripts.benchmark_graph_classification import main as graph_main

        return graph_main(arguments)

    print(f"benchmark error: unknown command: {command}", file=sys.stderr)
    print(_TOP_LEVEL_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
