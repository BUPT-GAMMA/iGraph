from __future__ import annotations

import sys

from backend.runtime.proxy_compat import setup_no_proxy
from .server import logger, mcp


def main() -> None:
    setup_no_proxy()
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    logger.info("mcp_server starting  transport=%s", transport)
    if transport in ("sse", "streamable-http"):
        mcp.run(transport=transport)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
