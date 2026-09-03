"""
GraphData Server 入口。

推荐入口：
- `python -m graph_data_service`
"""

from __future__ import annotations

import uvicorn

from graph_engine.env import load_environment, require_env, require_int_env
from .app import GraphDataService, create_app

load_environment()

_service = GraphDataService()
app = create_app(_service)


def run() -> None:
    host = require_env("GRAPH_DATA_HOST")
    port = require_int_env("GRAPH_DATA_PORT")
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    run()
