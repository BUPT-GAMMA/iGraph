import asyncio
import csv
import io
from types import SimpleNamespace

import pytest
from agno.run import RunContext
from agent_runtime.remote_graph.adapters import AdapterRows
from agent_runtime.remote_graph.export import OFFSET, LIMIT, paged_query
from agent_runtime.execution_store import ControlError
from tests.test_projects import client as project_client, headers
from tests.test_remote_graph_tools import tools
from tests.test_remote_graph_executor import service, anyio_backend


@pytest.fixture
def export_service(service, tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    service.executor.settings.remote_graph_max_rows = 3000
    service.executor.settings.remote_graph_max_bytes = 1024 * 1024
    service.executor.settings.remote_graph_preview_rows = 5
    rows = [[i, '中文'] for i in range(2708)]
    calls = []
    class Adapter:
        async def execute(self, ctx, request):
            calls.append(request)
            start = request.parameters.get(OFFSET, 0)
            size = request.parameters.get(LIMIT, len(rows))
            page = rows[start:start+size]
            return AdapterRows(['id', 'name'], page, len(page), False)
    monkeypatch.setattr('agent_runtime.remote_graph.registry.get_adapter', lambda _: Adapter())
    context = RunContext(run_id='r', session_id='s', user_id='alice')
    return SimpleNamespace(service=service, rows=rows, calls=calls, context=context,
        root=tmp_path / 'workspaces' / 'alice' / 's')


def read_csv(root, result):
    assert result['artifact']['visibility'] == 'user'
    assert result['artifact']['zone'] == 'Derived'
    return list(csv.reader(io.StringIO((root / result['artifact']['path']).read_text(encoding='utf-8-sig'))))


@pytest.mark.anyio
@pytest.mark.parametrize('paginate', [False, True])
async def test_complete_2708_row_export_is_published_not_just_preview(export_service, paginate):
    e = export_service
    result = await tools(e.service, e.service.binding)['export_remote_graph'](
        'MATCH (n) RETURN n.id AS id, n.name AS name ORDER BY id', paginate=paginate, run_context=e.context)
    assert result['success'] and result['export_complete']
    assert result['returned_rows'] == 2708 and len(result['rows']) == 5
    assert result['preview_truncated'] and not result['truncated']
    assert read_csv(e.root, result) == [['id', 'name']] + [[str(i), '中文'] for i in range(2708)]
    assert len(e.calls) == (43 if paginate else 1)
    assert result['consistency'] == ('paged_no_snapshot' if paginate else 'single_query')
    assert not list((e.root / 'Ephemeral/remote_graph').iterdir())


@pytest.mark.anyio
async def test_long_query_is_published_even_if_model_omits_save_and_publish(export_service):
    e = export_service
    result = await tools(e.service, e.service.binding)['query_remote_graph']('MATCH (n) RETURN n.id, n.name', run_context=e.context)
    assert result['export_complete']
    assert len(read_csv(e.root, result)) == 2709
    assert (e.root / result['artifact_path']).exists()


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['rows', 'bytes', 'page', 'publish'])
async def test_incomplete_or_failed_exports_never_report_delivery(export_service, monkeypatch, failure):
    e = export_service
    if failure == 'rows': e.service.executor.settings.remote_graph_max_rows = 128
    if failure == 'bytes': e.service.executor.settings.remote_graph_max_bytes = 800
    if failure == 'page':
        original = e.service.executor.query
        async def query(*args, **kwargs):
            result = await original(*args, **kwargs)
            if len(e.calls) == 2: result.truncated = True
            return result
        monkeypatch.setattr(e.service.executor, 'query', query)
    if failure == 'publish':
        (e.root / 'Derived').mkdir(parents=True)
        outside = e.root.parent / 'outside'; outside.mkdir()
        (e.root / 'Derived/remote_graph').symlink_to(outside, target_is_directory=True)
    result = await tools(e.service, e.service.binding)['export_remote_graph'](
        'MATCH (n) RETURN n.id AS id ORDER BY id', paginate=True, run_context=e.context)
    assert not result['success'] and not result.get('export_complete') and not result.get('artifact')
    assert not list((e.root / 'Derived').rglob('*.csv'))


@pytest.mark.parametrize('query', ['MATCH (n) RETURN n', 'MATCH (n) RETURN n ORDER BY n.id LIMIT 10',
    'MATCH (n) RETURN n ORDER BY n.id SKIP 2', 'RETURN 1 AS x UNION RETURN 2 AS x ORDER BY x',
    "RETURN 'ORDER BY' AS x", 'RETURN 1 AS rand ORDER BY rand()', 'RETURN 1 AS x; RETURN 2 AS y ORDER BY y'])
def test_pagination_does_not_rewrite_unordered_or_already_paged_queries(query):
    with pytest.raises(ControlError, match='REMOTE_GRAPH_EXPORT_ORDER_REQUIRED'):
        paged_query(query, {})


def test_pagination_handles_comments_without_allowing_parameter_overrides():
    assert '\nSKIP $' in paged_query('MATCH (n) RETURN n.id AS id ORDER BY id; // comment', {})
    with pytest.raises(ControlError): paged_query('RETURN 1 AS id ORDER BY id', {OFFSET: 500})


@pytest.mark.anyio
async def test_csv_handles_quotes_newlines_arrays_and_formula_strings(export_service):
    e = export_service
    e.rows[:] = [[1, '=SUM(A1:A2)'], [2, 'comma,quote"\n中文'], [3, [1, 'x']], [4, None]]
    result = await tools(e.service, e.service.binding)['export_remote_graph']('RETURN 1', run_context=e.context)
    assert read_csv(e.root, result)[1:] == [['1', "'=SUM(A1:A2)"], ['2', 'comma,quote"\n中文'], ['3', '[1,"x"]'], ['4', '']]


@pytest.mark.anyio
async def test_cancellation_stops_pagination_before_publication(export_service, monkeypatch):
    from agent_runtime.execution_scope import RunScope, current_scope
    from agno.exceptions import RunCancelledException
    e = export_service
    scope = RunScope('r', 'alice', 's', 1)
    original = e.service.executor.query
    async def query(*args, **kwargs):
        result = await original(*args, **kwargs)
        scope.cancel()
        return result
    monkeypatch.setattr(e.service.executor, 'query', query)
    token = current_scope.set(scope)
    try:
        with pytest.raises(RunCancelledException):
            await tools(e.service, e.service.binding)['export_remote_graph']('RETURN 1 AS id ORDER BY id', paginate=True, run_context=e.context)
    finally: current_scope.reset(token)
    assert len(e.calls) == 1 and not list((e.root / 'Derived').rglob('*.csv'))

@pytest.mark.anyio
async def test_export_is_listed_and_downloadable_by_workspace_owner(export_service, project_client):
    e = export_service
    client = project_client
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    context = RunContext(run_id='r', session_id=sid, user_id='admin')
    # The workspace owner needs its own connection; the shared fixture belongs to alice.
    from agent_runtime.remote_graph.contracts import RemoteBinding
    binding = RemoteBinding(**e.service.catalog.save('admin', e.service.form).model_dump())
    result = await tools(e.service, binding)['export_remote_graph'](
        'MATCH (n) RETURN n.id AS id, n.name AS name ORDER BY id', paginate=True, run_context=context)
    assert result['success'] and result['export_complete']
    path = result['artifact']['path']
    listed = client.get(f'/api/sessions/{sid}/workspace/files', headers=headers())
    assert path in [item['path'] for item in listed.json()['files']]
    endpoint = f'/api/sessions/{sid}/workspace/derived/' + path.removeprefix('Derived/')
    downloaded = client.get(endpoint, headers=headers())
    assert downloaded.status_code == 200
    rows = list(csv.reader(io.StringIO(downloaded.content.decode('utf-8-sig'))))
    assert len(rows) == 2709 and rows[-1] == ['2707', '中文']


@pytest.mark.anyio
async def test_exact_page_boundary_requires_empty_final_page(export_service):
    e = export_service
    e.rows[:] = e.rows[:128]
    result = await tools(e.service, e.service.binding)['export_remote_graph'](
        'MATCH (n) RETURN n.id AS id ORDER BY id', paginate=True, run_context=e.context)
    assert result['export_complete'] and result['returned_rows'] == 128
    assert [call.parameters[OFFSET] for call in e.calls] == [0, 64, 128]


@pytest.mark.anyio
async def test_query_publication_error_is_explicit_and_preserves_read_result(export_service, monkeypatch):
    e = export_service
    async def failed(*args): raise OSError('private-path-must-not-leak')
    monkeypatch.setattr('agent_runtime.remote_graph.export.publish_csv', failed)
    result = await tools(e.service, e.service.binding)['query_remote_graph']('RETURN 1', run_context=e.context)
    assert result['success'] and not result['export_complete']
    assert result['delivery_error'] == 'REMOTE_GRAPH_EXPORT_FAILED'
    assert 'private-path' not in str(result)

@pytest.mark.anyio
async def test_duplicate_sort_keys_are_not_silently_exported(export_service):
    e = export_service
    e.rows[64][0] = e.rows[0][0]
    result = await tools(e.service, e.service.binding)['export_remote_graph'](
        'MATCH (n) RETURN n.id AS id ORDER BY id', paginate=True, run_context=e.context)
    assert not result['success'] and result['error_code'] == 'REMOTE_GRAPH_EXPORT_ORDER_REQUIRED'
    assert not list((e.root / 'Derived').rglob('*.csv'))
