from __future__ import annotations

import uvicorn

from agent_runtime.config import AgentRuntimeConfig


def main() -> None:
    config = AgentRuntimeConfig.from_env()
    uvicorn.run("agent_runtime.os_app:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()

