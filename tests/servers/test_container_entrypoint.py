from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# The entry point imports the legacy gateway, which loads deployment settings.
with patch.dict(os.environ):
    from backend.api_gateway.container_main import config_from_env, main


class ContainerEntrypointTest(unittest.TestCase):
    def test_application_uses_external_service_urls_without_changing_config_shape(self) -> None:
        env = {
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_PORT": "21109",
            "API_UPLOAD_DIR": "/data/uploads",
            "MCP_PORT": "21110",
            "GRAPH_DATA_PORT": "21111",
            "MCP_SERVER_URL": "http://mcp:21110/mcp",
            "GRAPH_DATA_SERVER_URL": "http://graph-data:21111",
        }
        with patch.dict(os.environ, env, clear=False):
            config = config_from_env()

        self.assertEqual(config.port, 21109)
        self.assertEqual(config.mcp_url, "http://mcp:21110/mcp")
        self.assertEqual(config.graph_data_base_url, "http://graph-data:21111")
        self.assertEqual(config.upload_dir, "/data/uploads")

    @patch("backend.api_gateway.container_main.setup_no_proxy")
    @patch("backend.api_gateway.container_main.load_dotenv_compat")
    @patch("backend.api_gateway.container_main.ApiGatewayServer")
    def test_application_does_not_spawn_compose_managed_services(
        self,
        server_cls,
        _load_dotenv,
        _setup_no_proxy,
    ) -> None:
        env = {
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_PORT": "21109",
            "API_UPLOAD_DIR": "/data/uploads",
            "MCP_PORT": "21110",
            "GRAPH_DATA_PORT": "21111",
            "MCP_SERVER_URL": "http://mcp:21110/mcp",
            "GRAPH_DATA_SERVER_URL": "http://graph-data:21111",
        }
        with patch.dict(os.environ, env, clear=False):
            main()

        server_cls.assert_called_once()
        self.assertFalse(server_cls.call_args.kwargs["manage_subprocesses"])
        server_cls.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
