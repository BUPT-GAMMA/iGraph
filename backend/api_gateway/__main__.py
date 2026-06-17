from __future__ import annotations

from .app import ApiGatewayConfig, ApiGatewayServer
from backend.runtime.dotenv_compat import load_dotenv_compat
from backend.runtime.proxy_compat import setup_no_proxy
from backend.runtime.env_required import (
    require_env,
    require_int_env,
)

load_dotenv_compat()
setup_no_proxy()


def main() -> None:
    cfg = ApiGatewayConfig(
        host=require_env("API_SERVER_HOST"),
        port=require_int_env("API_SERVER_PORT"),
        upload_dir=require_env("API_UPLOAD_DIR"),
        mcp_port=require_int_env("MCP_PORT"),
        graph_data_port=require_int_env("GRAPH_DATA_PORT"),
        mcp_url=require_env("MCP_SERVER_URL"),
        graph_data_base_url=require_env("GRAPH_DATA_SERVER_URL"),
    )
    server = ApiGatewayServer(config=cfg, manage_subprocesses=True)
    server.run()


if __name__ == "__main__":
    main()
