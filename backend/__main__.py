from __future__ import annotations

import uvicorn

from backend.config import BackendConfig


def main() -> None:
    config = BackendConfig.from_env()
    uvicorn.run("backend.app:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()

