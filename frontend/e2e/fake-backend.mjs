import http from 'node:http';

const sessions = [
  { session_id: 'session-a', project_id: 'project-1', title: 'Session A', updated_at: '2026-08-31T02:00:00Z' },
  { session_id: 'session-b', project_id: 'project-1', title: 'Session B', updated_at: '2026-08-31T01:00:00Z' },
  { session_id: 'session-c', project_id: 'project-1', title: 'Session C Paused', updated_at: '2026-08-31T00:00:00Z' },
];
const deletedSessions = new Set();
const sessionsByClientId = new Map();
const runs = new Map();
const feedbackRequests = new Map();
const pendingFeedback = { run_id: 'run-paused', request_id: 'run-paused', call_id: 'call-train', tool_name: 'train', proposed_args: { epochs: 2 }, description: '确认训练任务' };
let projectDeleted = false;
const activeDatasetBySession = new Map([
  ['session-a', 'dataset-cora'],
]);

function datasetView(datasetId) {
  if (datasetId === 'dataset-hiv') return {
    kind: 'graph_collection',
    visualization: { supported: false, code: 'NON_SINGLE_GRAPH' },
    summary: {
      num_graphs: 41127,
      num_tasks: 1,
      metric: 'roc_auc',
      split_sizes: { train: 32901, val: 4113, test: 4113 },
      node_vocab_size: 382,
      edge_vocab_size: 8,
      missing_label_rate: 0,
    },
  };
  if (datasetId === 'dataset-cora') return {
    kind: 'single_graph',
    visualization: { supported: true, code: null },
    summary: { num_nodes: 2708, num_edges: 10556 },
  };
  return null;
}

async function requestJson(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); } catch { return {}; }
}

function json(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': '*',
    'Access-Control-Allow-Methods': '*',
  });
  response.end(JSON.stringify(body));
}

function envelope(sessionId, runId, eventIndex, events) {
  return `data: ${JSON.stringify({ protocol_version: 2, session_id: sessionId, run_id: runId, event_index: eventIndex, events })}\n\n`;
}

function eventEnvelope(sessionId, runId, events) {
  return `data: ${JSON.stringify({ protocol_version: 2, session_id: sessionId, run_id: runId, events })}\n\n`;
}

function streamRun(response, sessionId) {
  const runId = `run-${sessionId}`;
  const prefix = sessionId === 'session-a' ? 'A' : 'B';
  const run = { run_id: runId, status: 'running', content: '', events: [] };
  runs.set(sessionId, run);
  const send = (index, events) => { const frame = envelope(sessionId, runId, index, events); run.events.push(frame); if (!response.destroyed) response.write(frame); };
  response.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  });
  send(0, [{ type: 'model_turn_start', run_id: runId }]);
  setTimeout(() => { run.content = `${prefix}-response-`; send(1, [{ type: 'token', message_id: `assistant:${runId}`, content: run.content }]); }, 50);
  setTimeout(() => { run.content += 'complete'; send(2, [{ type: 'token', message_id: `assistant:${runId}`, content: 'complete' }]); }, sessionId === 'session-a' ? 650 : 180);
  setTimeout(() => {
    run.status = 'completed';
    send(3, [{ type: 'done', message_id: `assistant:${runId}` }]);
    response.end();
  }, sessionId === 'session-a' ? 800 : 260);
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url || '/', 'http://127.0.0.1:11109');
  if (request.method === 'OPTIONS') return json(response, 204, {});
  if (url.pathname === '/api/auth/validate') return json(response, 200, { valid: true, default_model: 'qwen3.6-plus', available_models: ['qwen3.6-plus'] });
  if (url.pathname === '/api/sessions' && request.method === 'GET') return json(response, 200, { success: true, sessions: sessions.filter(session => !deletedSessions.has(session.session_id)) });
  if (url.pathname === '/api/sessions' && request.method === 'POST') {
    const body = await requestJson(request);
    const clientSessionId = String(body.client_session_id || `server-${Date.now()}`);
    const existing = sessionsByClientId.get(clientSessionId);
    if (existing) return json(response, 200, { success: true, ...existing, reused: true });
    const title = String(body.initial_message_preview || '新会话').trim().slice(0, 36) || '新会话';
    const created = {
      session_id: clientSessionId,
      project_id: body.project_id || 'general',
      active_dataset_id: body.active_dataset_id || null,
      dataset_view: datasetView(body.active_dataset_id || null),
      model: body.model || 'qwen3.6-plus',
      title,
    };
    sessionsByClientId.set(clientSessionId, created);
    sessions.unshift({ session_id: created.session_id, project_id: created.project_id, title, preview: body.initial_message_preview, model: created.model, updated_at: new Date().toISOString() });
    return json(response, 200, { success: true, ...created, reused: false });
  }
  if (url.pathname === '/api/projects' && request.method === 'GET') return json(response, 200, { success: true, projects: [
    { project_id: 'general', user_id: 'e2e-user', name: 'General', is_default: true, conversation_count: 0 },
    ...(!projectDeleted ? [{ project_id: 'project-1', user_id: 'e2e-user', name: 'E2E Project', conversation_count: 3 }] : []),
  ] });
  if (url.pathname === '/api/projects/project-1/conversations') return json(response, 200, { success: true, conversations: sessions.filter(session => !deletedSessions.has(session.session_id)) });
  if (url.pathname === '/api/projects/project-1/datasets') return json(response, 200, { success: true, project_id: 'project-1', datasets: [
    { dataset_id: 'dataset-cora', project_id: 'project-1', name: 'Cora', original_filename: 'cora.pt', file_type: 'pt', status: 'ready', is_default: true, metadata: { num_nodes: 2708, num_edges: 10556 } },
    { dataset_id: 'dataset-hiv', project_id: 'project-1', name: 'hiv', original_filename: 'hiv.pt', file_type: 'pt', status: 'ready', is_default: false },
  ] });
  if (url.pathname === '/api/projects/project-1' && request.method === 'DELETE') {
    projectDeleted = true;
    sessions.forEach(session => deletedSessions.add(session.session_id));
    return json(response, 200, { success: true, project_id: 'project-1', affected_conversations: sessions.length, general_project_id: 'general' });
  }
  const activate = /^\/api\/sessions\/([^/]+)\/activate$/.exec(url.pathname);
  // Each explicit activation of this fixture starts its approval scenario.
  if (activate?.[1] === 'session-c') runs.set('session-c', { run_id: 'run-paused', status: 'paused', content: '', events: [] });
  if (activate && (!sessions.some(session => session.session_id === activate[1]) || deletedSessions.has(activate[1]))) return json(response, 404, { detail: '会话不存在' });
  if (activate) return json(response, 200, {
    success: true,
    session_id: activate[1],
    messages: runs.has(activate[1]) ? [{ id: `assistant:${runs.get(activate[1]).run_id}`, role: 'assistant', content: runs.get(activate[1]).content }] : [],
    active_run: runs.has(activate[1]) && runs.get(activate[1]).status !== 'completed' ? { run_id: runs.get(activate[1]).run_id, status: runs.get(activate[1]).status, last_event_index: 8 } : null,
    pending_feedback: activate[1] === 'session-c' ? { run_id: 'run-paused', request_id: 'run-paused', call_id: 'call-train', tool_name: 'train', proposed_args: { epochs: 2 }, description: '确认训练任务' } : null,
    project_id: 'project-1',
    active_dataset_id: activeDatasetBySession.get(activate[1]) || null,
    has_graph: activeDatasetBySession.get(activate[1]) === 'dataset-cora',
    dataset_view: datasetView(activeDatasetBySession.get(activate[1]) || null),
    model: 'qwen3.6-plus',
    available_models: ['qwen3.6-plus'],
  });
  const removeSession = /^\/api\/sessions\/([^/]+)$/.exec(url.pathname);
  if (removeSession && request.method === 'DELETE') {
    deletedSessions.add(removeSession[1]);
    return json(response, 200, { success: true, session_id: removeSession[1] });
  }
  const switchDataset = /^\/api\/sessions\/([^/]+)\/dataset$/.exec(url.pathname);
  if (switchDataset && request.method === 'PATCH') {
    const body = await requestJson(request);
    activeDatasetBySession.set(switchDataset[1], body.active_dataset_id || null);
    return json(response, 200, {
      success: true,
      session_id: switchDataset[1],
      project_id: 'project-1',
      active_dataset_id: body.active_dataset_id || null,
      dataset_view: datasetView(body.active_dataset_id || null),
    });
  }
  const feedback = /^\/api\/sessions\/([^/]+)\/feedback\/stream$/.exec(url.pathname);
  if (feedback) {
    feedbackRequests.set(feedback[1], (feedbackRequests.get(feedback[1]) || 0) + 1);
    const run = { run_id: 'run-paused', status: 'running', content: '', events: [] };
    runs.set(feedback[1], run);
    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });
    response.write(eventEnvelope(feedback[1], 'run-paused', [{ type: 'feedback_accepted', run_id: 'run-paused' }]));
    setTimeout(() => {
      run.content = '确认处理完成'; run.status = 'completed';
      response.end(envelope(feedback[1], 'run-paused', 9, [{ type: 'done', message_id: 'assistant:run-paused', content: run.content }]));
    }, 120);
    return;
  }
  const chat = /^\/api\/sessions\/([^/]+)\/chat\/stream$/.exec(url.pathname);
  if (chat) return streamRun(response, chat[1]);
  const snapshot = /^\/api\/sessions\/([^/]+)\/runs\/([^/]+)\/snapshot$/.exec(url.pathname);
  if (snapshot) {
    const run = runs.get(snapshot[1]);
    if (!run || run.run_id !== snapshot[2]) return json(response, 404, { detail: 'Run not found' });
    return json(response, 200, { session_id: snapshot[1], run_id: run.run_id, status: run.status,
      messages: [{ id: `assistant:${run.run_id}`, role: 'assistant', content: run.content }],
      active_run: run.status === 'completed' ? null : { run_id: run.run_id, status: run.status, last_event_index: run.events.length - 1 },
      pending_feedback: run.status === 'paused' ? pendingFeedback : null,
      snapshot_complete: ['completed', 'paused'].includes(run.status), resume_cursor: null });
  }
  const resume = /^\/api\/sessions\/([^/]+)\/runs\/([^/]+)\/resume$/.exec(url.pathname);
  if (resume) {
    const run = runs.get(resume[1]);
    if (!run || !run.events.length) return json(response, 409, { detail: { code: 'REPLAY_UNAVAILABLE', reason: 'buffer_missing' } });
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*' });
    const cursor = Number(url.searchParams.get('last_event_index'));
    for (const frame of run.events.slice(cursor + 1)) response.write(frame);
    response.end();
    return;
  }
  const graph = /^\/api\/sessions\/([^/]+)\/graph\/visualize$/.exec(url.pathname);
  if (graph && activeDatasetBySession.get(graph[1]) === 'dataset-hiv') return json(response, 409, { detail: { code: 'GRAPH_NOT_VISUALIZABLE', message: '非独立图，无法直接可视化', dataset_view: datasetView('dataset-hiv') } });
  if (graph) return json(response, 200, { success: true, nodes: [{ id: 'A', label: 'A' }, { id: 'B', label: 'B' }], edges: [{ src: 'A', dst: 'B' }], num_nodes: 2708, num_edges: 10556, total_nodes: 2708, total_edges: 10556, truncated: true, truncated_to: 2 });
  return json(response, 404, { detail: `Unhandled fake route: ${request.method} ${url.pathname}` });
});

server.listen(11109, '127.0.0.1');

for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
