"""Task-local progress delivery through the native tool DAG event stream."""
from contextvars import ContextVar

progress_sink = ContextVar('igraph_graph_progress_sink', default=None)


async def emit_job_progress(payload):
    sink = progress_sink.get()
    if sink is not None:
        await sink(payload)
