"""Graph Engine HTTP adapter."""

from graph_data_service.server import app, run

__all__ = ["app", "run"]


if __name__ == "__main__":
    run()

