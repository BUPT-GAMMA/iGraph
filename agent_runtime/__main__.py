from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.workspace_tools import workspace_sandbox_startup_check
from execution_protocol import validate_service_configuration


def main() -> None:
    load_dotenv()
    validate_service_configuration()
    workspace_sandbox_startup_check()
    config = AgentRuntimeConfig.from_env()
    uvicorn.run("agent_runtime.os_app:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
