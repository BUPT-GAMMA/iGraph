"""Lightweight spawn targets for tests of real large-graph process cancellation.

Keep this module independent of test modules and their application imports:
the parent verifies the child's process group before opening the start gate.
"""
import os
import time


def holding_worker(request, roots, messages, start_gate):
    """Wait for cancellation only after registering a real child identity."""
    os.setsid()
    from graph_data_service.large_graph.runtime import _await_worker_registration

    _await_worker_registration(start_gate)
    messages.put({'type': 'progress', 'stage': 'test_worker_ready', 'progress': 0.0,
                  'message': 'Test worker passed the start gate', 'detail': {}})
    while True:
        time.sleep(.05)
