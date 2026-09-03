"""Application container entry point.

The regular ``backend.api_gateway`` entry continues to supervise local MCP and
GraphData processes.  Compose starts those same services separately, so this
thin entry only disables subprocess supervision.
"""

from __future__ import annotations

from backend.runtime.dotenv_compat import load_dotenv_compat
from backend.runtime.env_required import require_env, require_int_env
from backend.runtime.proxy_compat import setup_no_proxy

from .app import ApiGatewayConfig, ApiGatewayServer


def config_from_env() -> ApiGatewayConfig:
    return ApiGatewayConfig(
        host=require_env("API_SERVER_HOST"),
        port=require_int_env("API_SERVER_PORT"),
        upload_dir=require_env("API_UPLOAD_DIR"),
        mcp_port=require_int_env("MCP_PORT"),
        graph_data_port=require_int_env("GRAPH_DATA_PORT"),
        mcp_url=require_env("MCP_SERVER_URL"),
        graph_data_base_url=require_env("GRAPH_DATA_SERVER_URL"),
    )


def main() -> None:
    load_dotenv_compat()
    setup_no_proxy()
    server = ApiGatewayServer(
        config=config_from_env(),
        manage_subprocesses=False,
    )
    server.run()


if __name__ == "__main__":
    main()
