from __future__ import annotations

import sys


def main() -> None:
    adapter = sys.argv[1].lower() if len(sys.argv) > 1 else "api"
    if adapter not in {"api", "mcp"}:
        raise SystemExit("Usage: python -m graph_engine [api|mcp] [transport]")
    from graph_engine.env import load_environment
    from execution_protocol import validate_service_configuration

    load_environment()
    validate_service_configuration()
    if adapter == "api":
        from graph_engine.api import run

        run()
        return
    if adapter == "mcp":
        from mcp_service.__main__ import main as run_mcp

        sys.argv = [sys.argv[0], *(sys.argv[2:] or ["streamable-http"])]
        run_mcp()
        return


if __name__ == "__main__":
    main()
