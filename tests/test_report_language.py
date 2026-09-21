import asyncio
import json
import re
from pathlib import Path

from graph_data_service.reporting import build_analysis_report
from graph_data_service.report_locale import LABELS


def visible(value):
    if isinstance(value, dict):
        return {key: visible(item) for key, item in value.items() if not key.endswith('_i18n')}
    if isinstance(value, list):
        return [visible(item) for item in value]
    return value


def test_reports_translate_all_generated_sections_without_touching_source_facts():
    sources = {
        'get_graph_info': {'num_nodes': 5, 'num_edges': 4, 'directed': True, 'has_edge_weight': False},
        'degree_analysis': {'avg_in_degree': 1, 'avg_out_degree': 1, 'top_degree_nodes': [{'node': 'raw_node_id', 'total': 2}], 'truncated': True},
        'node_centrality': {'centrality_type': 'betweenness', 'top_nodes': [{'node': 'raw_node_id', 'value': .5}]},
        'graph_connectivity': {'is_weakly_connected': True, 'weakly_connected_components': {'count': 1, 'largest_size': 5}},
        'global_metrics': {'density': .2},
        'community_detection': {'num_communities': 1, 'modularity': .1, 'size_stats': {'mean': 5}, 'communities': [{'community_id': 'A', 'size': 5, 'nodes_truncated': True}], 'communities_truncated': True},
        'custom': {'score': 0},
    }
    en = build_analysis_report('analysis', '', '', sources, {'language': 'English'})['report']
    zh = build_analysis_report('analysis', '', '', sources, {'language': 'Chinese'})['report']
    assert not re.search('[\u4e00-\u9fff]', json.dumps(visible(en), ensure_ascii=False))
    assert en['language'] == 'en' and zh['language'] == 'zh'
    assert en['id'] != zh['id']
    assert '5 nodes · 4 edges' in en['summary']
    assert zh['sections'][0]['title_i18n']['en'] == 'Graph overview'
    assert en['sections'][1]['charts'][0]['data'][0]['label'] == 'raw_node_id'
    assert en['sections'][0]['metrics'][0]['value'] == zh['sections'][0]['metrics'][0]['value'] == 5
    sources['custom'] = {'名称': '原始中文'}
    custom = build_analysis_report('analysis', '', '用户标题', sources, {'language': 'English'})['report']
    assert custom['title'] == '用户标题' and 'title_i18n' not in custom
    assert custom['sections'][-1]['tables'][0]['rows'][0] == {'field': '名称', 'value': '原始中文'}


def test_minimal_report_and_centrality_only_are_english():
    for sources in ({'custom': {'nested': []}}, {'node_centrality': {'centrality_type': 'degree', 'top_nodes': [{'node': 1, 'value': .2}]}}):
        report = build_analysis_report('analysis', '', '', sources, {'language': 'en', 'max_sections': 1})['report']
        assert not re.search('[\u4e00-\u9fff]', json.dumps(visible(report), ensure_ascii=False))


def test_legacy_ui_catalogue_matches_report_labels():
    catalogue = Path(__file__).resolve().parents[1] / 'frontend/src/reportLabels.json'
    assert json.loads(catalogue.read_text()) == LABELS


def test_native_tool_locks_report_language_but_preserves_other_arguments():
    from agno.tools.function import ToolResult
    from agent_runtime.graph_jobs import GraphMCPTools
    toolkit = GraphMCPTools(url='http://unused/mcp', response_language='English')
    seen = []

    async def entrypoint(**arguments):
        seen.append(arguments)
        return ToolResult(content='{"success": true}')

    options = {'top_k': 4, 'language': 'Chinese'}
    asyncio.run(toolkit._completed_entrypoint(entrypoint, 'create_analysis_report')(options=options))
    assert seen[0]['options'] == {'top_k': 4, 'language': 'English'}
    assert options['language'] == 'Chinese'
    asyncio.run(toolkit._completed_entrypoint(entrypoint, 'get_graph_info')())
    assert 'options' not in seen[1]
