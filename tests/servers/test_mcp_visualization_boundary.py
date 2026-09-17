"""Exercise provider JSON strings through the real FastMCP validation boundary."""

import json
import os
import unittest
from unittest.mock import patch

from fastmcp import Client, FastMCP

from graph_engine.tool_catalog import get_all_public_tools
from mcp_service.tool_forwarding import forwarded_graph_tool_arguments

with patch.dict(os.environ, {
    "MCP_HOST": "127.0.0.1", "MCP_PORT": "19110",
    "GRAPH_DATA_SERVER_URL": "http://127.0.0.1:19111",
}):
    from mcp_service.server import _build_handler_with_signature


class VisualizationBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_and_encoded_arguments_reach_forwarder(self):
        forwarded = []

        async def invoke(tool_name, _ctx, arguments):
            result = forwarded_graph_tool_arguments(tool_name, arguments)
            forwarded.append(result)
            return json.dumps(result)

        spec = next(item for item in get_all_public_tools(include_graph_ml=True)
                    if item["name"] == "visualize_subgraph")
        server = FastMCP("visualization-boundary-test")
        server.tool()(_build_handler_with_signature(spec["name"], spec["inputSchema"], invoke))
        nodes = ["163", "523", "565"]
        highlights = {"paths": [{"node_sequence": nodes}]}
        async with Client(server) as client:
            for encoded in (False, True):
                result = await client.call_tool("visualize_subgraph", {
                    "node_names": json.dumps(nodes) if encoded else nodes,
                    "highlights": json.dumps(highlights) if encoded else highlights,
                })
                self.assertFalse(result.is_error)
                self.assertEqual(forwarded[-1]["node_names"], nodes)
                self.assertEqual(forwarded[-1]["highlights"], highlights)
            for invalid in ('{"id":"163"}', '[163]', 'not JSON'):
                result = await client.call_tool("visualize_subgraph", {
                    "node_names": invalid,
                }, raise_on_error=False)
                self.assertTrue(result.is_error)
            self.assertEqual(len(forwarded), 2)
