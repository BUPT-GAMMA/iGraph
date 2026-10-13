"""Local test services use a disposable credential, independent of production."""
import os

import pytest


def pytest_addoption(parser):
    parser.addoption('--require-live', action='store_true', default=False,
                     help='Fail remote graph acceptance when real database/model prerequisites are missing')


@pytest.fixture(autouse=True)
def test_service_credential(monkeypatch):
    if not os.environ.get("IGRAPH_CONTROL_SERVICE_KEY"):
        monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "test-services-only")


@pytest.fixture(autouse=True)
def skip_bundled_example_inputs(monkeypatch):
    # Each Graph Engine startup would otherwise decompress ~540 MB of bundled
    # inputs into its throwaway data root and exhaust CI runner disk space.
    # tests/test_example_inputs.py covers installation with small fixtures.
    if not os.environ.get("GRAPH_ENGINE_INSTALL_EXAMPLE_INPUTS"):
        monkeypatch.setenv("GRAPH_ENGINE_INSTALL_EXAMPLE_INPUTS", "false")
