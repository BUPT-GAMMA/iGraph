"""Bounded query exports: collect, verify completion, and publish in one tool call."""
import asyncio
import csv
import json
import re
import time
from pathlib import Path

from agent_runtime.execution_scope import current_scope
from agent_runtime.execution_store import ControlError
from agent_runtime.workspace_tools import create_workspace_tools, workspace_root
from .contracts import QueryRequest
from .policy import _TOKENS
from .results import RowBudget, bounded_rows, dumps

OFFSET = '__igraph_export_offset'
LIMIT = '__igraph_export_limit'


def result_directory(context):
    root = workspace_root(context)
    directory = root / 'Ephemeral' / 'remote_graph'
    if directory.is_symlink() or not directory.resolve().is_relative_to(root):
        raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
    return directory


def _pagination(query, parameters):
    """Append pagination only to an ordered, unpaged outer RETURN query."""
    matches = [m for m in _TOKENS.finditer(query)
               if not m.group().isspace() and not m.group().startswith(('//', '/*'))]
    if matches and matches[-1].group() == ';':
        query = query[:matches[-1].start()] + query[matches[-1].end():]
        matches.pop()
    words, depth = [], 0
    for match in matches:
        token = match.group()
        if token in ('(', '[', '{'):
            if depth == 0: words.append(token)
            depth += 1
        elif token in (')', ']', '}'):
            depth -= 1
            if depth == 0: words.append(token)
        elif depth == 0:
            words.append(token)
    lowered = [word.lower() for word in words]
    returns = [i for i, word in enumerate(lowered) if word == 'return']
    start = returns[-1] + 1 if returns else len(words)
    orders = [i for i in range(start, len(words)) if lowered[i:i+2] == ['order', 'by']]
    ordered = bool(orders)
    if (not ordered or any(word in lowered for word in ('skip', 'limit', 'union', ';'))
            or OFFSET in parameters or LIMIT in parameters):
        raise ControlError('REMOTE_GRAPH_EXPORT_ORDER_REQUIRED', 422)
    groups = [[]]
    for token in words[orders[-1] + 2:]:
        if token == ',': groups.append([])
        else: groups[-1].append(token)
    keys = []
    for group in groups:
        if (not group or len(group) > 2 or (len(group) == 2 and group[1].lower() not in {'asc', 'desc'})
                or not (re.fullmatch(r'[^\W\d]\w*', group[0]) or group[0].startswith('`'))):
            raise ControlError('REMOTE_GRAPH_EXPORT_ORDER_REQUIRED', 422)
        keys.append(group[0].strip('`').replace('``', '`'))
    return query + f'\nSKIP ${OFFSET} LIMIT ${LIMIT}', keys


def paged_query(query, parameters):
    return _pagination(query, parameters)[0]


def _cell(value):
    if isinstance(value, (dict, list)):
        return dumps(value)
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    # CSV is commonly opened in spreadsheets; preserve strings as literal text.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


async def publish_csv(context, query_id, columns, rows):
    scope = current_scope.get()
    if scope:
        scope.check()
    directory = result_directory(context)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{query_id}.csv'
    try:
        with path.open('x', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output)
            writer.writerow([_cell(column) for column in columns])
            writer.writerows([_cell(value) for value in row] for row in rows)
        publish = next(fn for fn in create_workspace_tools() if fn.__name__ == 'workspace_publish')
        result = await publish(f'Ephemeral/remote_graph/{path.name}', context)
        if not result.get('success'):
            raise ControlError('REMOTE_GRAPH_EXPORT_FAILED', 500)
        return result['artifact']
    finally:
        path.unlink(missing_ok=True)


async def deliver_saved_result(context, result):
    """A complete saved query must not depend on a second model publish call."""
    if not result.success or not result.artifact_path or result.truncated:
        return result
    path = result_directory(context) / Path(result.artifact_path).name
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        result.artifact = await publish_csv(context, result.query_id, result.columns, payload['rows'])
        result.export_complete = True
    except (OSError, ControlError):
        result.delivery_error = 'REMOTE_GRAPH_EXPORT_FAILED'
    return result


async def export_result(executor, uid, source, request, context, paginate):
    started = time.monotonic()
    directory = result_directory(context)
    query, order_keys = _pagination(request.query, request.parameters) if paginate else (request.query, [])
    seen_keys = set()
    budget = RowBudget(executor.settings.remote_graph_max_rows, executor.settings.remote_graph_max_bytes)
    columns = None
    offset = 0
    # Below the admitted FalkorDB server cap of 128; lower server caps fail closed.
    page_size = min(64, executor.settings.remote_graph_max_rows)
    if page_size < 1:
        raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
    async with asyncio.timeout(executor.settings.remote_graph_timeout_seconds):
        while True:
            scope = current_scope.get()
            if scope:
                scope.check()
            parameters = {**request.parameters, OFFSET: offset, LIMIT: page_size} if paginate else request.parameters
            result = await executor.query(uid, source, QueryRequest(query=query, parameters=parameters, save_result=True), directory)
            if not result.success:
                return result
            if result.truncated or not result.artifact_path:
                raise ControlError('REMOTE_GRAPH_EXPORT_INCOMPLETE', 422)
            path = directory / Path(result.artifact_path).name
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            finally:
                path.unlink(missing_ok=True)
            if columns is not None and columns != result.columns:
                raise ControlError('REMOTE_GRAPH_EXPORT_INCOMPLETE', 422)
            columns = result.columns
            if any(columns.count(key) != 1 for key in order_keys):
                raise ControlError('REMOTE_GRAPH_EXPORT_ORDER_REQUIRED', 422)
            key_indices = [columns.index(key) for key in order_keys]
            for row in payload['rows']:
                if key_indices:
                    key = dumps([row[index] for index in key_indices])
                    if key in seen_keys:
                        raise ControlError('REMOTE_GRAPH_EXPORT_ORDER_REQUIRED', 422)
                    seen_keys.add(key)
                if not budget.add(row):
                    raise ControlError('REMOTE_GRAPH_EXPORT_INCOMPLETE', 422)
            offset += len(payload['rows'])
            if not paginate or len(payload['rows']) < page_size:
                break
        preview = bounded_rows(budget.rows, max_rows=executor.settings.remote_graph_preview_rows,
                               max_bytes=executor.settings.remote_graph_preview_bytes)
        result.rows = preview.rows
        result.returned_rows = budget.returned_rows
        result.preview_truncated = preview.truncated
        result.artifact_path = None
        result.artifact = await publish_csv(context, result.query_id, columns, budget.rows)
        result.export_complete = True
        result.consistency = 'paged_no_snapshot' if paginate else 'single_query'
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result
