ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY deploy/requirements/mcp.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

RUN useradd --create-home --uid 10001 igraph \
    && mkdir -p /data \
    && chown -R igraph:igraph /app /data
USER igraph

EXPOSE 21110
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=12 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1', 21110), 2); s.close()" || exit 1

CMD ["python", "-m", "graph_engine", "mcp", "http"]
