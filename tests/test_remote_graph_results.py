import json

import pytest
from neo4j.graph import Graph, Node, Path
from neo4j.spatial import WGS84Point
from neo4j.time import Date, Duration


def bounded(rows, **kwargs):
    from agent_runtime.remote_graph.results import bounded_rows
    return bounded_rows(rows, max_rows=kwargs.get('max_rows', 100), max_bytes=kwargs.get('max_bytes', 65536))


def test_limit_is_not_full_count_and_exact_limit_is_complete():
    result = bounded([[1], [2], [3]], max_rows=2)
    assert result.rows == [[1], [2]]
    assert result.returned_rows == 2 and result.truncated
    assert bounded([[1], [2]], max_rows=2).truncated is False
    assert bounded([]).returned_rows == 0


def test_consumer_only_looks_one_row_past_limit():
    def source():
        yield [1]
        yield [2]
        raise AssertionError('Unbounded consumption')
    assert bounded(source(), max_rows=1).rows == [[1]]


def test_utf8_byte_budget_counts_json_punctuation_and_huge_values():
    assert bounded([['中文']], max_bytes=12).rows == [['中文']]
    assert bounded([['中文']], max_bytes=11).truncated
    assert bounded([['a' * 1_000_000]], max_bytes=256).rows == []
    assert bounded([[{'large': list(range(100000))}]], max_bytes=256).truncated


def test_official_graph_types_keep_edge_and_path_direction():
    graph = Graph()
    a = Node(graph, 'a', 1, ['Paper'], {'title': '论文'})
    b = Node(graph, 'b', 2, ['Paper'], {})
    edge = graph.relationship_type('LINKS_TO')(graph, 'e', 3, {'weight': 2})
    edge._start_node, edge._end_node = b, a
    path = Path(a, edge)
    node, relationship, encoded_path = bounded([[a, edge, path]]).rows[0]
    assert node['element_id'] == 'a' and node['properties'] == {'title': '论文'}
    assert relationship['start_node'] == 'b' and relationship['end_node'] == 'a'
    assert encoded_path['nodes'][0]['element_id'] == 'a'
    assert encoded_path['relationships'][0]['start_node'] == 'b'


def test_special_values_are_portable_json():
    result = bounded([[Date(2026, 9, 26), Duration(months=1, days=2), WGS84Point((1, 2)), b'\x00\xff', float('inf'), float('nan')]])
    values = result.rows[0]
    assert values[0] == {'type': 'Date', 'value': '2026-09-26'}
    assert values[1]['type'] == 'Duration'
    assert values[2]['srid'] == 4326 and values[2]['coordinates'] == [1.0, 2.0]
    assert values[3] == {'type': 'bytes', 'base64': 'AP8='}
    assert values[4] == {'type': 'float', 'value': 'Infinity'}
    assert values[5] == {'type': 'float', 'value': 'NaN'}
    json.dumps(result.rows, allow_nan=False)


def test_giant_node_property_and_deep_recursion_are_bounded():
    node = Node(Graph(), 'a', 1, ['Paper'], {'text': 'x' * 1_000_000})
    assert bounded([[node]], max_bytes=256).rows == []
    nested = []
    nested.append(nested)
    assert bounded([[nested]], max_bytes=256).truncated
