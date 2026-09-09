"""One cooperative deadline per construction, including nested helper calls.

Context-local state keeps direct library calls compatible and cannot leak a
budget between users or builds. The service also supervises a process deadline
for calls (including native code and network clients) that cannot checkpoint.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time


_deadline = ContextVar("construction_deadline", default=None)


class ConstructionBudgetExceeded(TimeoutError):
    code = "construction_time_budget_exhausted"


def current_deadline():
    return _deadline.get()


def set_deadline(deadline):
    previous = current_deadline()
    return _deadline.set(deadline if previous is None else min(previous, deadline))


def reset_deadline(token):
    _deadline.reset(token)


@contextmanager
def deadline_scope(deadline):
    token = set_deadline(deadline)
    try:
        yield
    finally:
        reset_deadline(token)


def check_deadline(deadline=None):
    inherited = current_deadline()
    if inherited is not None:
        deadline = inherited if deadline is None else min(inherited, deadline)
    if deadline is not None and time.monotonic() >= deadline:
        raise ConstructionBudgetExceeded("Graph construction exceeded its overall deadline")


def checked(values, *, deadline=None):
    """Checkpoint an ordered scan before entry, every 1024 items, and on exit."""
    if deadline is None and current_deadline() is None:
        yield from values
        return
    check_deadline(deadline)
    for index, value in enumerate(values):
        if index and index % 1024 == 0:
            check_deadline(deadline)
        yield value
    check_deadline(deadline)
