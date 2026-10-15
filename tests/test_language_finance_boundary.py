import copy
from pathlib import Path

import networkx as nx
import pytest
from agno.models.message import Message
from agent_runtime.model_history import ModelHistoryMixin
from graph_data_service.compute import GraphCompute
from graph_data_service.graph_data import GraphData
from graph_data_service.highlight_connections import resolve_ownership_selectors
from tests.fixtures.finance_ownership import ownership_document


@pytest.mark.parametrize('language,expected,excluded', [
    ('Chinese', '本轮界面语言：简体中文', 'Current interface language: English'),
    ('English', 'Current interface language: English', '本轮界面语言：简体中文'),
])
@pytest.mark.parametrize('budgeted', [False, True])
def test_captured_language_reapplies_after_foreign_tool_output_without_history_mutation(language, expected, excluded, budgeted):
    model = ModelHistoryMixin()
    model.id = 'test-model'
    model.igraph_response_language = language
    if budgeted:
        model.igraph_capabilities = dict(context_window=10000, max_output_tokens=1000, vision=False)
    messages = [Message(role='system', content='base'), Message(role='user', content='Analyse graph'),
        Message(role='assistant', tool_calls=[{'id': 't1', 'function': {'name': 'get_graph_info', 'arguments': '{}'}}]),
        Message(role='tool', tool_call_id='t1', content='English tool text / 中文工具结果')]
    before = copy.deepcopy(messages)
    for _ in range(2):
        prepared = model._prepare_history(messages, [], None)
        assert prepared[-1].role == 'system'
        assert expected in prepared[-1].content
        assert excluded not in prepared[-1].content
        assert len(prepared) == len(messages) + 1
        assert messages == before


def graph_fixture():
    doc = ownership_document()
    graph = nx.MultiDiGraph()
    for row in doc['graph']['nodes']:
        graph.add_node(row['id'], **{key: value for key, value in row.items() if key != 'id'})
    for record, source, target in [('row-0', 'row-R', 'row-A'), ('row-1', 'row-A', 'row-T'), ('row-2', 'row-P', 'row-T')]:
        graph.add_edge(record, source, id=record + '-holder')
        graph.add_edge(record, target, id=record + '-target')
    return graph


def test_business_ids_resolve_and_preserve_both_fk_links_and_input():
    graph = graph_fixture()
    requested = {'nodes': [{'ids': ['I:A'], 'color': '#ef4444'}],
                 'subgraphs': [{'nodes': ['T', 'A', 'P'], 'label': 'direct holders'}]}
    before = copy.deepcopy(requested)
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ['T', 'A', 'P'], False, requested)
    assert result['success']
    assert {node['id'] for node in result['nodes']} == {'row-T', 'row-A', 'row-P', 'row-1', 'row-2'}
    assert len(result['edges']) == 4
    assert {tuple(edge) for edge in result['highlights']['paths'][0]['edges']} == {
        ('row-1', 'row-A'), ('row-1', 'row-T'), ('row-2', 'row-P'), ('row-2', 'row-T')}
    assert requested == before
    assert result['resolved_node_ids']['I:A'] == ['row-A']


def test_type_collision_fails_instead_of_highlighting_wrong_person():
    graph = graph_fixture()
    graph.nodes['row-P']['properties']['data']['IDV_ID'] = 'A'
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ['A'], False)
    assert result['success'] is False and 'AMBIGUOUS' in result['error']
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ['I:A', 'P:A'], False)
    assert {node['id'] for node in result['nodes']} == {'row-A', 'row-P'}


def test_raw_ids_win_and_duplicate_rows_cannot_silently_choose_path_endpoint():
    graph = graph_fixture()
    graph.add_node('duplicate', **copy.deepcopy(graph.nodes['row-A']))
    selected, _, _ = resolve_ownership_selectors(graph, ['I:A'], None)
    assert set(selected) == {'row-A', 'duplicate'}
    with pytest.raises(ValueError, match='AMBIGUOUS_OWNERSHIP_ENDPOINT'):
        resolve_ownership_selectors(graph, None, {'paths': [{'node_sequence': ['I:A', 'T']}]})
    assert resolve_ownership_selectors(graph, ['row-A'], None)[0] == ['row-A']
    assert resolve_ownership_selectors(graph, ['unknown'], None)[0] == ['unknown']


def test_preset_question_two_maps_verified_holders_and_all_record_edges():
    path = Path('graph_engine/resources/finance_risk/finance-risk.npz')
    gd = GraphData.load(str(path))
    compute = GraphCompute()
    result = compute.finance_analysis(gd, 'finance_first_holder', {'target_id': 'SYN-ENT-000044'})
    holders = result['result']['direct_holders']
    assert len(holders) == result['result']['direct_holder_count'] == 5
    assert not result['result']['direct_holders_truncated']
    assert all(row['source_node_ids'] and row['ownership_source_node_ids'] for row in holders)
    ids = [result['result']['target']['business_id']] + [row['business_id'] for row in holders]
    graph = compute.resolve_graph('highlight-regression', 1, gd)
    visual = compute.visualize_subgraph(graph, gd, ids, False,
        {'subgraphs': [{'nodes': ids}], 'nodes': [{'ids': result['result']['first_holder_ids']}]})
    assert visual['success'], visual
    assert visual['num_nodes'] == 11 and visual['num_edges'] == 10
    assert len(visual['highlights']['paths'][0]['edges']) == 10
    actual = {node['id'] for node in visual['nodes']}
    assert all(set(row['source_node_ids']) <= actual for row in holders)
    assert all(graph.has_edge(edge['src'], edge['dst']) for edge in visual['edges'])
