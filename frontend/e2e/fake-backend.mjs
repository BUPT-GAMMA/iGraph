import http from 'node:http';

const sessions = [
  { session_id: 'session-a', project_id: 'project-1', title: 'Session A', updated_at: '2026-08-31T02:00:00Z' },
  { session_id: 'session-b', project_id: 'project-1', title: 'Session B', updated_at: '2026-08-31T01:00:00Z' },
  { session_id: 'session-c', project_id: 'project-1', title: 'Session C Paused', updated_at: '2026-08-31T00:00:00Z' },
];

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
  response.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Access-Control-Allow-Origin': '*',
  });
  response.write(envelope(sessionId, runId, 0, [{ type: 'model_turn_start', run_id: runId }]));
  setTimeout(() => response.write(envelope(sessionId, runId, 1, [{ type: 'token', message_id: `assistant:${runId}`, content: `${prefix}-response-` }])), 50);
  setTimeout(() => response.write(envelope(sessionId, runId, 2, [{ type: 'token', message_id: `assistant:${runId}`, content: 'complete' }])), sessionId === 'session-a' ? 650 : 180);
  setTimeout(() => {
    response.end(envelope(sessionId, runId, 3, [{ type: 'done', message_id: `assistant:${runId}` }]));
  }, sessionId === 'session-a' ? 800 : 260);
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url || '/', 'http://127.0.0.1:11109');
  if (request.method === 'OPTIONS') return json(response, 204, {});
  if (url.pathname === '/api/auth/validate') return json(response, 200, { valid: true, default_model: 'qwen3.6-plus', available_models: ['qwen3.6-plus'] });
  if (url.pathname === '/api/sessions' && request.method === 'GET') return json(response, 200, { success: true, sessions });
  if (url.pathname === '/api/projects' && request.method === 'GET') return json(response, 200, { success: true, projects: [{ project_id: 'project-1', user_id: 'e2e-user', name: 'E2E Project', conversation_count: 3 }] });
  if (url.pathname === '/api/projects/project-1/conversations') return json(response, 200, { success: true, conversations: sessions });
  if (url.pathname === '/api/projects/project-1/datasets') return json(response, 200, { success: true, project_id: 'project-1', datasets: [] });
  const activate = /^\/api\/sessions\/([^/]+)\/activate$/.exec(url.pathname);
  if (activate) return json(response, 200, {
    success: true,
    session_id: activate[1],
    messages: [],
    active_run: activate[1] === 'session-c' ? { run_id: 'run-paused', status: 'paused', last_event_index: 8 } : null,
    pending_feedback: activate[1] === 'session-c' ? { run_id: 'run-paused', request_id: 'run-paused', call_id: 'call-train', tool_name: 'train', proposed_args: { epochs: 2 }, description: '确认训练任务' } : null,
    project_id: 'project-1',
    active_dataset_id: null,
    has_graph: false,
    model: 'qwen3.6-plus',
    available_models: ['qwen3.6-plus'],
  });
  const feedback = /^\/api\/sessions\/([^/]+)\/feedback\/stream$/.exec(url.pathname);
  if (feedback) {
    response.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });
    response.write(eventEnvelope(feedback[1], 'run-paused', [{ type: 'feedback_accepted', run_id: 'run-paused' }]));
    setTimeout(() => response.end(envelope(feedback[1], 'run-paused', 9, [{ type: 'done', message_id: 'assistant:run-paused', content: '确认处理完成' }])), 120);
    return;
  }
  const chat = /^\/api\/sessions\/([^/]+)\/chat\/stream$/.exec(url.pathname);
  if (chat) return streamRun(response, chat[1]);
  const graph = /^\/api\/sessions\/([^/]+)\/graph\/visualize$/.exec(url.pathname);
  if (graph) return json(response, 200, { success: true, nodes: [], edges: [], num_nodes: 0, num_edges: 0 });
  return json(response, 404, { detail: `Unhandled fake route: ${request.method} ${url.pathname}` });
});

server.listen(11109, '127.0.0.1');

for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
