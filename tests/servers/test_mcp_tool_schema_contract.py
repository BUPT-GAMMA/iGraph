from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from graph_data_service.large_graph.compatibility import (
    SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS,
    SMALL_GRAPH_ALGORITHM_MODES,
)
from graph_data_service.tool_catalog import get_all_public_tools


# The production MCP server intentionally requires explicit network settings
# at import time.  CI checks out the repository without a local .env, so keep
# this schema-only test self-contained instead of depending on developer or
# deployment configuration.
with patch.dict(os.environ, {
    "MCP_HOST": "127.0.0.1",
    "MCP_PORT": "19110",
    "GRAPH_DATA_SERVER_URL": "http://127.0.0.1:19111",
}):
    from mcp_service.server import mcp


def test_mcp_preserves_canonical_graph_tool_json_schemas() -> None:
    async def registered_schemas() -> dict[str, dict]:
        return {
            tool.name: tool.parameters
            for tool in await mcp.list_tools(run_middleware=False)
        }

    actual = asyncio.run(registered_schemas())
    for spec in get_all_public_tools():
        assert actual[spec["name"]] == spec["inputSchema"], spec["name"]


def test_large_graph_algorithm_schema_exposes_real_parameter_contract() -> None:
    tools = {spec["name"]: spec for spec in get_all_public_tools()}
    schema = tools["run_large_graph_algorithm"]["inputSchema"]
    parameters = schema["properties"]["parameters"]
    properties = parameters["properties"]

    assert parameters["additionalProperties"] is True
    assert {
        "source", "target", "sink", "algorithm", "centrality_type", "method",
        "metric", "node", "nodes", "focus_nodes", "k", "cutoff", "limit", "top_k",
        "max_cycles", "optimize_by", "treat_as_undirected", "max_iterations",
        "sample_sources", "damping", "alpha", "beta", "tolerance",
        "use_weight", "create_version", "allow_expensive", "strict_semantics",
    } <= set(properties)
    assert set(properties["algorithm"]["enum"]) == {
        "default", "yen_k_shortest", "louvain", "greedy_modularity",
        "label_propagation", "label_propagation_asyn", "leiden",
        "girvan_newman", "asyn_fluidc",
    }
    assert set(properties["centrality_type"]["enum"]) == {
        "degree", "betweenness", "closeness", "pagerank", "eigenvector",
        "harmonic", "katz", "load", "voterank", "percolation",
    }
    expected_methods = {
        mode
        for algorithm, modes in SMALL_GRAPH_ALGORITHM_MODES.items()
        if SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS.get(algorithm) == "method"
        for mode in modes
    }
    expected_metrics = {
        mode
        for algorithm, modes in SMALL_GRAPH_ALGORITHM_MODES.items()
        if SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS.get(algorithm) == "metric"
        for mode in modes
    }
    assert set(properties["method"]["enum"]) == expected_methods
    assert set(properties["metric"]["enum"]) == expected_metrics
    try:
        expected_sample_limit = max(
            1, int(os.getenv("LARGE_GRAPH_MAX_SAMPLE_SOURCES", "10000"))
        )
    except (TypeError, ValueError, OverflowError):
        expected_sample_limit = 10_000
    assert properties["sample_sources"]["maximum"] == expected_sample_limit
    assert (
        "LARGE_GRAPH_MAX_SAMPLE_SOURCES"
        in properties["sample_sources"]["description"]
    )
    assert properties["focus_nodes"]["maxItems"] == 200
    community = tools["community_detection"]["inputSchema"]["properties"]
    assert community["focus_nodes"]["maxItems"] == 200
    assert community["max_iterations"]["default"] == 5


def test_large_subgraph_schema_does_not_claim_version_creation() -> None:
    tools = {spec["name"]: spec for spec in get_all_public_tools()}
    run_spec = tools["run_large_graph_algorithm"]
    create_version = run_spec["inputSchema"]["properties"]["parameters"][
        "properties"
    ]["create_version"]

    assert "const" not in create_version
    assert create_version["default"] is True
    assert "分区快照" in create_version["description"]
    assert "new_graph_version" in create_version["description"]
    assert "可用子模式" in tools["get_large_graph_capabilities"]["description"]


def test_global_metrics_explains_mathematically_undefined_results() -> None:
    tools = {spec["name"]: spec for spec in get_all_public_tools()}
    description = tools["global_metrics"]["description"]

    assert "defined=false" in description
    assert "数学未定义" in description
    assert "不表示引擎不支持" in description
