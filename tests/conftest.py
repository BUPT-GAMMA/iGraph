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
