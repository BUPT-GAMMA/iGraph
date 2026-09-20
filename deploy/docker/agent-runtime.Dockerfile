ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG COMPUTE_REQUIREMENTS=deploy/requirements/compute-cpu.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap bash && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ${COMPUTE_REQUIREMENTS} /tmp/compute.txt
RUN --mount=type=cache,id=igraph-pip,target=/root/.cache/pip,sharing=locked \
    PIP_DEFAULT_TIMEOUT=120 python -m pip install -r /tmp/compute.txt
COPY deploy/requirements/agent-runtime.txt /tmp/requirements.txt
# uv provisions per-session virtual environments for uploaded code. It runs
# outside the bubblewrap sandbox, where network access is available.
RUN --mount=type=cache,id=igraph-pip,target=/root/.cache/pip,sharing=locked \
    PIP_DEFAULT_TIMEOUT=120 python -m pip install uv -r /tmp/requirements.txt -c /tmp/compute.txt
COPY agent_runtime /app/agent_runtime
COPY response_language.py /app/
# Skill and memory paths share Backend's canonical data-root resolver.
COPY backend/__init__.py backend/data_paths.py /app/backend/
# Only the graph engine's shared execution policies are needed here.
COPY graph_engine/__init__.py graph_engine/tool_execution.py /app/graph_engine/
COPY execution_protocol.py construction_access.py /app/
COPY execution_lifecycle.py workspace_storage.py runtime_observability.py /app/

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data/agent_runtime/skills/users /data/agent_runtime/skills/candidates \
       /data/agent_runtime/uv-cache \
    && chown -R igraph:igraph /app /data
USER igraph

EXPOSE 21112
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=18 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:21112/internal/health', timeout=2)" || exit 1

CMD ["python", "-m", "agent_runtime"]
