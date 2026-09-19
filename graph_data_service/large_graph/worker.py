"""Standalone Local Graph Worker entry point.

Run with ``python -m graph_data_service.large_graph.worker``.  The worker and
Graph Engine API only share the durable data root; no in-process API state is
required to claim, resume, cancel or finish a large-graph job.
"""

from __future__ import annotations

import signal
import threading

from graph_engine.env import load_environment

from .service import LargeGraphService


def run() -> None:
    load_environment()
    stopped = threading.Event()

    def request_stop(_signum=None, _frame=None) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    service = LargeGraphService(embedded_worker=False)
    service.runtime.start()
    try:
        while not stopped.wait(timeout=1.0):
            pass
    finally:
        # A running child is terminated and its durable job is requeued so the
        # replacement container can resume it from a clean staging directory.
        service.close()


if __name__ == "__main__":
    run()
