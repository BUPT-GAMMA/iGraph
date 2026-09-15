"""Community coloring stays enabled across page and agent visualizations."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from graph_data_service.app import GraphDataService
from graph_data_service.compute import GraphCompute
from graph_data_service.graph_data import GraphData
from graph_data_service.tool_catalog import GRAPH_TOOLS


@pytest.fixture
def graph_data():
    # Two triangles joined by a bridge give visibly distinct communities.
    pairs = [(0, 1), (1, 2), (2, 0), (2, 3), (3, 4), (4, 5), (5, 3)]
    names = list("abcdef")
    return GraphData(
        num_nodes=len(names),
        edge_index=np.asarray(pairs, dtype=np.int64).T,
        id2node=dict(enumerate(names)),
        node2id={name: index for index, name in enumerate(names)},
        directed=False,
    )


@pytest.fixture(params=[False, True], ids=["local", "remote"])
def service(request, graph_data):
    # Exercise real dispatch, visualization and computation without opening a DB.
    instance = GraphDataService.__new__(GraphDataService)
    session = SimpleNamespace(
        is_remote=request.param,
        graph_version=1,
        graph_summary=lambda: SimpleNamespace(
            num_nodes=graph_data.num_nodes, num_edges=graph_data.num_edges,
        ),
    )
    instance.get_required = Mock(return_value=session)
    instance._require_graph = Mock(return_value=(session, graph_data, graph_data.to_networkx()))
    instance._compute = GraphCompute()
    instance._nebula = SimpleNamespace(materialize_subgraph=Mock(return_value=graph_data))
    return instance


def test_agent_highlight_keeps_page_community_colors(service):
    page = service._dispatch_graph_tool("session", "visualize_subgraph", {"include_community": True})
    highlighted = service._dispatch_graph_tool("session", "visualize_subgraph", {
        "highlights": {"nodes": [{"ids": ["c"], "color": "#ef4444"}]},
    })
    assert page["success"] and highlighted["success"]
    assert page["community"]["num_communities"] >= 2
    assert highlighted["community"] == page["community"]
    assert highlighted["highlights"]["nodes"][0]["ids"] == ["c"]
    assert highlighted["highlights"]["nodes"][0]["color"] == "#ef4444"


def test_explicit_community_opt_out_still_works(service):
    result = service._dispatch_graph_tool("session", "visualize_subgraph", {"include_community": False})
    assert result["success"]
    assert result["community"] is None


def test_compute_includes_communities_by_default(graph_data):
    result = GraphCompute().visualize_subgraph(graph_data.to_networkx(), graph_data)
    assert result["community"]["num_communities"] >= 2
    assert set(result["community"]["node_community"]) == set(graph_data.node2id)


def test_tool_schema_advertises_community_coloring_by_default():
    tool = next(tool for tool in GRAPH_TOOLS if tool["name"] == "visualize_subgraph")
    assert tool["inputSchema"]["properties"]["include_community"]["default"] is True
