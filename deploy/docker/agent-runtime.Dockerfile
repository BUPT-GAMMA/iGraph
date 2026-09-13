ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap bash && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/requirements/agent-runtime.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY agent_runtime /app/agent_runtime
# Shared execution policies are standard-library-only; no graph compute stack.
COPY graph_engine/__init__.py graph_engine/tool_execution.py /app/graph_engine/
COPY execution_protocol.py construction_access.py /app/
COPY execution_lifecycle.py workspace_storage.py runtime_observability.py /app/

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data/agent_runtime/skills/users /data/agent_runtime/skills/candidates \
    && chown -R igraph:igraph /app /data
USER igraph

EXPOSE 21112
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=18 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:21112/internal/health', timeout=2)" || exit 1

CMD ["python", "-m", "agent_runtime"]
