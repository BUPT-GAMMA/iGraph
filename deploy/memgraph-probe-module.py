"""Dedicated QA fixture: graph-read procedure with an observable file side effect."""

from pathlib import Path

import mgp


@mgp.read_proc
def touch() -> mgp.Record(count=int):
    path = Path("/tmp/igraph-task7-read-procedure-counter")
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return mgp.Record(count=count)


@mgp.read_proc
def counter() -> mgp.Record(count=int):
    path = Path("/tmp/igraph-task7-read-procedure-counter")
    return mgp.Record(count=int(path.read_text()) if path.exists() else 0)
