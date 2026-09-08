from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

from backend.config import BackendConfig
from execution_protocol import validate_service_configuration


def main() -> None:
    load_dotenv()
    validate_service_configuration()
    config = BackendConfig.from_env()
    uvicorn.run("backend.app:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
