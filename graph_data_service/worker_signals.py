"""Cooperative worker checkpoints (inactive in the shared service process)."""
import threading

stopping = threading.Event()


def check_cancelled():
    if stopping.is_set():
        raise InterruptedError('WORKER_CANCELLED')
