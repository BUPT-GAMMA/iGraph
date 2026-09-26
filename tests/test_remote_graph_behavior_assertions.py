"""Acceptance must distinguish no write attempt from executor rejection."""
import pytest
from tests.test_remote_graph_agent import assert_read_only_behavior


def test_rejected_delete_followed_by_verbal_refusal_is_still_a_write_attempt():
    answer = {'content': '只能只读，无法删除。', 'tools': [
        {'tool': 'query_remote_graph', 'args': {'query': 'MATCH (n) DETACH DELETE n'},
         'result': {'success': False, 'error_code': 'REMOTE_GRAPH_QUERY_REJECTED'}}]}
    with pytest.raises(AssertionError, match='write attempt'):
        assert_read_only_behavior(answer, lambda query, parameters: 'w')


def test_read_query_with_delete_in_text_is_not_a_write_attempt():
    answer = {'content': '只能只读。', 'tools': [
        {'tool': 'query_remote_graph', 'args': {'query': "RETURN 'DELETE' AS text"}}]}
    assert_read_only_behavior(answer, lambda query, parameters: 'r')


def test_verbal_refusal_without_query_is_accepted():
    assert_read_only_behavior({'content': '只能只读。', 'tools': []}, lambda *args: pytest.fail('No query to explain'))
