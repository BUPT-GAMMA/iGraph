"""Test collection must not configure unrelated tests from deployment files."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(
    ("filename", "opt_in", "collect_only"),
    [
        ("servers/test_nebula_integration.py", "", True),
        ("servers/test_nebula_integration.py", "1", True),
        ("servers/test_nebula_integration.py", "1", False),
        ("servers/test_api_gateway_app.py", "", True),
        ("servers/test_container_entrypoint.py", "", True),
        ("servers/test_graph_classification_agent.py", "", True),
        ("servers/test_large_graph_agent.py", "", True),
        ("test_report_artifacts.py", "", True),
    ],
)
def test_collection_and_skipped_setup_preserve_environment(tmp_path, filename, opt_in, collect_only):
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(root),
        "RUN_NEBULA_INTEGRATION": opt_in,
        "IGRAPH_RUN_CANCEL_ENABLED": "false",
        "IGRAPH_CONTROL_SERVICE_KEY": "collection-test-only",
    }
    script = textwrap.dedent("""\
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        import pytest
        from backend.runtime import dotenv_compat
        from graph_data_service.storage.nebula_backend import NebulaConfig

        def load_deployment_environment():
            os.environ["IGRAPH_RUN_CANCEL_ENABLED"] = "true"
            os.environ["IGRAPH_CONTROL_SERVICE_KEY"] = "deployment-test-only"
            return True

        args = [sys.argv[1], "-q"]
        if sys.argv[2] == "collect":
            args.append("--collect-only")
        # Exercise setup's skip path without opening a real database connection.
        with patch.object(dotenv_compat, "load_dotenv_compat", load_deployment_environment), \
             patch.object(NebulaConfig, "from_env", return_value=SimpleNamespace(enabled=False)):
            result = pytest.main(args)
        assert result == 0
        assert os.environ["IGRAPH_RUN_CANCEL_ENABLED"] == "false"
        assert os.environ["IGRAPH_CONTROL_SERVICE_KEY"] == "collection-test-only"
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(root / "tests" / filename),
         "collect" if collect_only else "run"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
