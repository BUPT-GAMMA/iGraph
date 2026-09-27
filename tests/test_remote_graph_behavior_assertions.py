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


def test_application_query_only_refusal_does_not_require_claiming_account_read_only():
    from tests.test_remote_graph_agent import assert_read_only_behavior
    answer = {'content': '此功能仅允许查询远程数据库，不支持删除或修改数据。', 'tools': []}
    assert_read_only_behavior(answer, lambda *_: 'r')


@pytest.mark.parametrize('query', [
    'MATCH (n:Paper) WHERE n.label = 0 RETURN count(n) AS count',
    'MATCH (n:Paper) WHERE n.label = 0 RETURN count(n) AS count // $unused',
])
def test_literal_filter_with_unused_parameter_fails_bound_filter_acceptance(query):
    from tests.test_multi_database_agent import assert_bound_filter
    args = {'query': query, 'parameters': {'unused': 0}}
    with pytest.raises(AssertionError, match='alternate label'):
        assert_bound_filter(args, '0', {'0': 298, '1': 418}, lambda query, parameters: [[298]])


def test_bound_filter_acceptance_uses_target_value_and_arbitrary_parameter_name():
    from tests.test_multi_database_agent import assert_bound_filter
    query = 'MATCH (n:Paper {label: $chosen_value}) RETURN count(n) AS count'
    args = {'query': query, 'parameters': {'chosen_value': 0}}
    calls = []
    def read(actual_query, parameters):
        calls.append((actual_query, parameters))
        return [[418]]
    assert_bound_filter(args, '0', {'0': 298, '1': 418}, read)
    assert calls == [(query, {'chosen_value': 1})]
    assert args['parameters'] == {'chosen_value': 0}


def test_filter_parameter_must_match_requested_value():
    from tests.test_multi_database_agent import assert_bound_filter
    with pytest.raises(AssertionError, match='requested label'):
        assert_bound_filter({'query': 'MATCH (n:Paper {label: $value}) RETURN count(n)',
                             'parameters': {'value': 6}}, '0', {'0': 298, '1': 418},
                            lambda *_: pytest.fail('Mismatched target must not be replayed'))
