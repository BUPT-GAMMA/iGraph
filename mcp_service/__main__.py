from __future__ import annotations

import sys

from graph_engine.env import setup_no_proxy
from .server import config, logger, mcp


def main() -> None:
    setup_no_proxy()
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    logger.info("mcp_server starting  transport=%s", transport)
    if transport in ("streamable-http", "http"):
        mcp.run(transport="http", host=config.host, port=config.port)
    elif transport == "sse":
        mcp.run(transport="sse", host=config.host, port=config.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
