ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY deploy/requirements/compute-gpu.txt /tmp/compute-gpu.txt
RUN --mount=type=cache,id=igraph-pip,target=/root/.cache/pip,sharing=locked \
    PIP_DEFAULT_TIMEOUT=120 python -m pip install -r /tmp/compute-gpu.txt
COPY deploy/requirements/graph-data.txt /tmp/requirements.txt
RUN --mount=type=cache,id=igraph-pip,target=/root/.cache/pip,sharing=locked \
    PIP_DEFAULT_TIMEOUT=120 python -m pip install -r /tmp/requirements.txt -c /tmp/compute-gpu.txt

COPY . /app

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data \
    && chown -R igraph:igraph /app /data
USER igraph

EXPOSE 21111
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=18 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:21111/health', timeout=2)" || exit 1

CMD ["python", "-m", "graph_engine", "api"]
