import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { RemoteGraphQueryResult } from './RemoteGraphQueryResult';
afterEach(cleanup);
const result = { success: true, query_id: 'q1', source: { connection_id: 'c', revision: 2, name: 'Cora', database: 'neo4j', kind: 'neo4j' }, columns: ['count'], rows: [[2708]], returned_rows: 50, truncated: false, preview_truncated: true, elapsed_ms: 4, artifact_path: 'Ephemeral/remote_graph/q1.json' };
it('shows saved source, bounded count and separate preview limitation', () => {
  render(<RemoteGraphQueryResult result={JSON.stringify(result)} />);
  expect(screen.getByText(/Cora/)).toBeTruthy(); expect(screen.getByText('2708')).toBeTruthy();
  expect(screen.getByText(/读取.*50|Read.*50/)).toBeTruthy();
  expect(screen.getByText(/预览.*截断|Preview truncated/)).toBeTruthy();
  expect(screen.queryByText(/结果.*未完整|Result truncated/)).toBeNull();
  expect(screen.getByText(result.artifact_path)).toBeTruthy();
});
it('shows execution truncation and hides internal paths', () => {
  render(<RemoteGraphQueryResult result={{ ...result, truncated: true, artifact_path: '/srv/private/file.json' }} />);
  expect(screen.getByText(/结果.*未完整|Result truncated/)).toBeTruthy();
  expect(screen.queryByText('/srv/private/file.json')).toBeNull();
});
it('renders controlled error codes without raw private errors', () => {
  render(<RemoteGraphQueryResult result={{ ...result, success: false, error_code: 'REMOTE_GRAPH_TIMEOUT', error_message: '/srv/private/secret' }} />);
  expect(screen.getByText(/REMOTE_GRAPH_TIMEOUT/)).toBeTruthy();
  expect(screen.queryByText('/srv/private/secret')).toBeNull();
});
