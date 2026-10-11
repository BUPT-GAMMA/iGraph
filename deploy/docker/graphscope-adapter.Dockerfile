ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential cmake libgomp1 procps tini \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/requirements/graphscope-adapter.txt /tmp/requirements.txt
RUN --mount=type=cache,id=igraph-pip,target=/root/.cache/pip,sharing=locked \
    PIP_DEFAULT_TIMEOUT=180 python -m pip install -r /tmp/requirements.txt

ENV GRAPHSCOPE_HOME=/usr/local/lib/python3.11/site-packages/graphscope.runtime

COPY graphscope_runtime /app/graphscope_runtime

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data /tmp/graphscope \
    && chown -R igraph:igraph /app /data /tmp/graphscope
USER igraph

EXPOSE 21113
HEALTHCHECK --interval=10s --timeout=5s --start-period=90s --retries=18 \
  CMD python -c "import json,urllib.request; p=json.load(urllib.request.urlopen('http://127.0.0.1:21113/health', timeout=3)); assert p['status']=='ok' and p['graphscope_available'] and p['engine_available'] and p['toolchain_available']" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "graphscope_runtime.server"]
