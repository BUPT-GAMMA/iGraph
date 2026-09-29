import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { RemoteGraphQueryResult } from './RemoteGraphQueryResult';
import userEvent from '@testing-library/user-event';
import { fetchArtifact } from './WorkspaceFiles';
vi.mock('./WorkspaceFiles', () => ({ fetchArtifact: vi.fn() }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const result = { success: true, query_id: 'q1', source: { connection_id: 'c', revision: 2, name: 'Cora', database: 'neo4j', kind: 'neo4j' }, columns: ['count'], rows: [[2708]], returned_rows: 50, truncated: false, preview_truncated: true, elapsed_ms: 4, artifact_path: 'Ephemeral/remote_graph/q1.json' };
it('identifies a FalkorDB historical result by its frozen source', () => {
  render(<RemoteGraphQueryResult result={{ ...result, source: { ...result.source, kind: 'falkordb', database: 'cora' } }} />);
  expect(screen.getByText('FalkorDB · Cora · cora')).toBeTruthy();
});
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
it('collapses technical details without hiding limitations', () => {
  render(<RemoteGraphQueryResult result={{ ...result, query: 'MATCH (n) RETURN count(n)', truncated: true }} />);
  const details = screen.getByText(/Technical details|技术详情/).closest('details')!;
  expect(details.open).toBe(false);
  expect(screen.getByText('q1').closest('details')).toBe(details);
  expect(screen.getByText(/Result truncated|结果未完整/).closest('details')).toBeNull();
  expect(screen.getByText('MATCH (n) RETURN count(n)').closest('details')).toBe(details);
});
it('keeps schema sampling and truncation visible outside collapsed properties', () => {
  render(<RemoteGraphQueryResult result={{ success: true, source: result.source, labels: ['Paper'], relationship_types: ['LINKS_TO'], properties: [], coverage: { sampled: true, truncated: true } }} />);
  expect(screen.getByText(/Sampled structure|结构来自采样/).closest('details')).toBeNull();
  expect(screen.getByText(/Structure truncated|结构已截断/).closest('details')).toBeNull();
});
it('shows preflight and schema failures even without a source or row envelope', () => {
  render(<RemoteGraphQueryResult result={{ success: false, error_code: 'REMOTE_GRAPH_NOT_SELECTED' }} />);
  expect(screen.getByRole('alert').textContent).toContain('REMOTE_GRAPH_NOT_SELECTED');
  expect(screen.getByRole('alert').closest('details')).toBeNull();
});
it('keeps graph element IDs in technical details while summarizing graph objects', () => {
  render(<RemoteGraphQueryResult result={{ ...result, rows: [[{ type: 'node', element_id: 'private-element-id', labels: ['Paper'], properties: { title: 'A paper' } }]] }} />);
  expect(screen.getByText(/private-element-id/).closest('details')).not.toBeNull();
  expect(screen.getByText('Paper').closest('details')).toBeNull();
});
it('limits the default table and keeps complete preview rows in technical details', () => {
  const rows = Array.from({ length: 10 }, (_, i) => [`row-${i}`]);
  render(<RemoteGraphQueryResult result={{ ...result, rows, returned_rows: 10, preview_truncated: false }} />);
  expect(screen.getAllByRole('row')).toHaveLength(6);
  expect(screen.getByText(/row-9/).closest('details')).not.toBeNull();
  expect(screen.getByText(/Showing the first|仅展示前/).closest('details')).toBeNull();
});
it('summarizes nested graph objects without exposing their internal IDs', () => {
  const node = { type: 'node', element_id: 'nested-internal-id', labels: ['Paper'], properties: { title: 'Example' } };
  render(<RemoteGraphQueryResult result={{ ...result, columns: ['nodes', 'entry'], rows: [[[node], { node }]] }} />);
  for (const cell of screen.getAllByRole('cell')) expect(cell.textContent).not.toContain('nested-internal-id');
  expect(screen.getByText(/nested-internal-id/).closest('details')).not.toBeNull();
});

it('downloads a published export for its owning conversation without a model link', async () => {
  vi.mocked(fetchArtifact).mockResolvedValue(new Blob(['id\n1\n2\n']));
  vi.stubGlobal('URL', { createObjectURL: vi.fn().mockReturnValue('blob:export'), revokeObjectURL: vi.fn() });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const path = 'Derived/remote_graph/query-1.csv';
  render(<RemoteGraphQueryResult result={{ ...result, export_complete: true, artifact: { path }, consistency: 'paged_no_snapshot' }} sessionId="original-session" />);
  await userEvent.click(screen.getByRole('button', { name: /Download CSV|下载 CSV/ }));
  await waitFor(() => expect(fetchArtifact).toHaveBeenCalledWith('original-session', path));
  expect(click).toHaveBeenCalled();
  expect(screen.getByText(/not a snapshot|不代表同一时刻的快照/)).toBeTruthy();
});
it('does not offer a complete download for partial or unpublished results', () => {
  render(<RemoteGraphQueryResult result={{ ...result, truncated: true, export_complete: false, artifact: { path: 'Derived/remote_graph/query-1.csv' } }} sessionId="s" />);
  expect(screen.queryByRole('button', { name: /Download CSV|下载 CSV/ })).toBeNull();
});
it('shows publication errors independently of successful query execution', () => {
  render(<RemoteGraphQueryResult result={{ ...result, delivery_error: 'REMOTE_GRAPH_EXPORT_FAILED' }} sessionId="s" />);
  expect(screen.getByRole('alert').textContent).toContain('REMOTE_GRAPH_EXPORT_FAILED');
  expect(screen.queryByRole('button', { name: /Download CSV|下载 CSV/ })).toBeNull();
});
