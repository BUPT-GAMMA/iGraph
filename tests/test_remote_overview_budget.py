from types import SimpleNamespace
from unittest.mock import Mock

from graph_data_service.app import GraphDataService
from graph_data_service.storage.nebula_backend import NebulaRepositoryError


def test_remote_overview_never_allocates_the_full_graph_edge_count():
    service = object.__new__(GraphDataService)
    session = SimpleNamespace(is_remote=True, graph_version=1,
        graph_summary=lambda: SimpleNamespace(num_nodes=10_000_000, num_edges=100_000_000))
    service.get_required = Mock(return_value=session)
    service._nebula = Mock()
    service._nebula.visualization_seed_name_primary.return_value = 'a'
    service._nebula.materialize_subgraph.side_effect = NebulaRepositoryError('SUBGRAPH_TOO_LARGE', 'budget exceeded')
    result = service.do_visualize_subgraph('s', None, False, user_id='u')
    request = service._nebula.materialize_subgraph.call_args.kwargs
    assert request['max_nodes'] <= 3200
    assert request['max_edges'] <= 200_000
    assert result['success'] is False
