ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY deploy/requirements/agent-runtime.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY agent_runtime /app/agent_runtime
COPY execution_protocol.py /app/execution_protocol.py
COPY execution_lifecycle.py /app/execution_lifecycle.py

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data/agent_runtime/skills/users /data/agent_runtime/skills/candidates \
    && chown -R igraph:igraph /app /data
USER igraph

EXPOSE 21112
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=18 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:21112/internal/health', timeout=2)" || exit 1

CMD ["python", "-m", "agent_runtime"]
