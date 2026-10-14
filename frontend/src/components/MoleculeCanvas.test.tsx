import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import depictionAsset from '../../public/examples/borylation/BO-01-depiction.json';
import { getBorylationCase, getDatasetMolecularStructure, getConstructionState, type BorylationCaseResponse } from '../api';
import type { Highlight } from '../types';
import { MoleculeCanvas, moleculeHighlightLayers, validateMoleculeDepiction, type MoleculeDepiction } from './MoleculeCanvas';
import { GraphWorkspace } from './GraphWorkspace';
import { GraphConstructionPanel } from './GraphConstructionPanel';
import type { Dataset } from '../types';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'), getBorylationCase: vi.fn(), getDatasetMolecularStructure: vi.fn(), getConstructionState: vi.fn(), visualizeGraph: vi.fn(async () => ({ nodes: [], edges: [] })) }));
const depiction = depictionAsset as MoleculeDepiction;
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const owned = (): BorylationCaseResponse => ({ success: true, step: 'structure', prediction_status: 'not_run', result: {
  molecule_id: depiction.molecule_id, smiles: depiction.input_smiles, heavy_atom_count: 22, chemical_bond_count: 24,
  aromatic_ch_count: 8, candidate_atoms: depiction.atoms.filter(atom => depiction.candidate_node_ids.includes(atom.node_id)),
  visualization_node_names: depiction.atoms.map(atom => atom.node_id),
} });
const fetchMock = vi.fn();
const noSelect = vi.fn();

beforeEach(() => {
  localStorage.setItem('igraph-language', 'zh');
  vi.mocked(getConstructionState).mockReset();
  vi.mocked(getDatasetMolecularStructure).mockReset().mockResolvedValue(owned());
  vi.mocked(getBorylationCase).mockReset();
  vi.mocked(getBorylationCase).mockResolvedValue(owned());
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, json: async () => clone(depiction) });
  vi.stubGlobal('fetch', fetchMock);
  noSelect.mockReset();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('uses stable atom IDs and 1-based site numbers, including heteroatoms, without assuming array order', async () => {
  const shuffled = clone(depiction); shuffled.atoms.reverse();
  expect(validateMoleculeDepiction(shuffled, owned()).atoms[0].atom_index).toBe(21);
  const onSelect = vi.fn();
  const { container } = render(<MoleculeCanvas sessionId="owned-session" highlights={[]} selected={null} onSelect={onSelect} />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  expect(container.querySelectorAll('[data-atom-id]')).toHaveLength(22);
  expect(container.querySelectorAll('.molecule-candidate')).toHaveLength(8);
  expect(container.querySelector('.molecule-canvas')?.getAttribute('data-chemical-bond-count')).toBe('24');
  fireEvent.click(screen.getByRole('button', { name: '选择候选位点 5' }));
  expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'BO-01:atom:4', label: 'C5', attrs: expect.objectContaining({ properties: expect.objectContaining({ atom_index: 4, display_number: 5, element: 'C', total_hydrogens: 1 }) }) }));
  fireEvent.keyDown(screen.getByRole('button', { name: '原子 O1，位点编号 1' }), { key: 'Enter' });
  expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'BO-01:atom:0', label: 'O1' }));
  expect(container.querySelector('image')?.getAttribute('href')).toBe(`${depiction.svg_url}?v=${depiction.svg_sha256}`);
});

it('rejects mismatched ownership, shifted numbering, and a foreign atom ID before rendering', () => {
  const other = owned(); other.result.smiles = 'c1ccccc1';
  expect(() => validateMoleculeDepiction(depiction, other)).toThrow('identity');
  const shifted = clone(depiction); shifted.atoms[8].display_number = 8;
  expect(() => validateMoleculeDepiction(shifted, owned())).toThrow('atom-map');
  const foreign = clone(depiction); foreign.atoms[8].node_id = 'other:atom:8';
  expect(() => validateMoleculeDepiction(foreign, owned())).toThrow('atom-map');
  const candidateWrong = clone(depiction); candidateWrong.candidate_node_ids[0] = 'BO-01:experiment:a5';
  expect(() => validateMoleculeDepiction(candidateWrong, owned())).toThrow('candidate-map');
});

it('maps only actual atom highlights and ignores experiment nodes and synthetic connection layers', async () => {
  const highlights: Highlight[] = [
    { id: 'actual', type: 'node', nodes: [...depiction.candidate_node_ids, 'BO-01:experiment:a5'], color: '#ef4444', label: '当前候选' },
    { id: 'records', type: 'node', nodes: ['BO-01', 'BO-01:P1', 'BO-01:experiment:a5'], color: '#7c3aed', label: '实验记录' },
    { id: 'highlighted-node-edges', type: 'path', edges: [['BO-01:atom:4', 'BO-01:atom:5']], color: '#f97316', label: '派生连接' },
  ];
  expect(moleculeHighlightLayers(highlights, depiction)).toHaveLength(1);
  const { container, rerender } = render(<MoleculeCanvas sessionId="s" highlights={highlights} selected={null} onSelect={noSelect} />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  const circles = container.querySelectorAll('.molecule-current-highlight');
  expect(circles).toHaveLength(8);
  expect([...circles].map(circle => Number(circle.getAttribute('data-display-number')))).toEqual([5, 6, 9, 13, 14, 18, 19, 20]);
  expect([...circles].every(circle => circle.getAttribute('stroke') === '#ef4444')).toBe(true);
  expect(screen.queryByText(/实验记录：/)).toBeNull();
  rerender(<MoleculeCanvas sessionId="s" highlights={[{ type: 'node', nodes: ['BO-01:atom:8'], color: '#16a34a', label: '位点9' }]} selected={null} onSelect={noSelect} />);
  expect(container.querySelectorAll('.molecule-current-highlight')).toHaveLength(1);
  expect(container.querySelector('.molecule-current-highlight')?.getAttribute('data-highlight-node-id')).toBe('BO-01:atom:8');
  expect(container.querySelector('.molecule-current-highlight')?.getAttribute('stroke')).toBe('#16a34a');
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('keeps candidate circles separate from current colored result layers', async () => {
  const highlights: Highlight[] = [{ id: 'all', type: 'node', nodes: depiction.candidate_node_ids, color: '#ef4444', label: '八个候选' },
    { id: 'observed', type: 'node', nodes: ['BO-01:atom:8', 'BO-01:atom:17', 'BO-01:atom:18'], color: '#7c3aed', label: '已报道三个位置' }];
  const { container } = render(<MoleculeCanvas sessionId="s" highlights={highlights} selected={null} onSelect={noSelect} />);
  const selector = await screen.findByRole('combobox', { name: '分子高亮图层' });
  expect(container.querySelectorAll('.molecule-current-highlight')).toHaveLength(11);
  expect(container.querySelectorAll('.molecule-candidate')).toHaveLength(8);
  fireEvent.change(selector, { target: { value: '1' } });
  expect(container.querySelectorAll('.molecule-current-highlight')).toHaveLength(3);
  expect([...container.querySelectorAll('.molecule-current-highlight')].every(circle => circle.getAttribute('stroke') === '#7c3aed')).toBe(true);
  expect(container.querySelectorAll('.molecule-candidate')).toHaveLength(8);
});

it('aborts previous-session verification and ignores its late successful response', async () => {
  let resolveOld!: (value: BorylationCaseResponse) => void;
  vi.mocked(getBorylationCase).mockImplementation(sid => sid === 'old' ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve(owned()));
  const { container, rerender } = render(<MoleculeCanvas sessionId="old" highlights={[]} selected={null} onSelect={noSelect} />);
  const oldSignal = vi.mocked(getBorylationCase).mock.calls[0][2]!;
  rerender(<MoleculeCanvas sessionId="new" highlights={[]} selected={null} onSelect={noSelect} />);
  expect(oldSignal.aborted).toBe(true);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  await act(async () => { resolveOld(owned()); });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(container.querySelectorAll('[data-atom-id]')).toHaveLength(22);
  expect(getBorylationCase).toHaveBeenLastCalledWith('new', 'structure', expect.any(AbortSignal));
});

it('clears the former drawing when the new session is unrelated or access fails', async () => {
  vi.mocked(getBorylationCase).mockImplementation(sid => sid === 'valid' ? Promise.resolve(owned()) : Promise.reject(new Error('not authorized')));
  const { container, rerender } = render(<MoleculeCanvas sessionId="valid" highlights={[]} selected={null} onSelect={noSelect} />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  rerender(<MoleculeCanvas sessionId="unrelated" highlights={[]} selected={null} onSelect={noSelect} />);
  expect(container.querySelector('image')).toBeNull();
  await screen.findByRole('alert');
  expect(container.querySelectorAll('[data-atom-id]')).toHaveLength(0);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByText('无法显示分子结构')).toBeTruthy();
});

it('shows a retryable failure when the depiction asset is unavailable', async () => {
  fetchMock.mockResolvedValueOnce({ ok: false });
  const { container } = render(<MoleculeCanvas sessionId="s" highlights={[]} selected={null} onSelect={noSelect} />);
  await screen.findByRole('alert');
  expect(container.querySelector('image')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '重试' }));
  await screen.findByRole('button', { name: '选择候选位点 5' });
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it('shows only the molecular structure and preserves atom selection in the molecular case', async () => {
  const graph = { nodes: depiction.atoms.map(atom => ({ id: atom.node_id, label: atom.node_id, attrs: { type: 'atom', properties: atom } })), edges: [] };
  function Workspace() {
    const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
    return <GraphWorkspace data={graph} molecularCase sessionId="s" datasetName="科学智能" tab="Graph" onTab={() => {}} selected={selected} onSelect={setSelected} collapsed={false} onToggle={() => {}} />;
  }
  const { container } = render(<Workspace />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  expect(screen.queryByRole('button', { name: '搜索节点' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '选择候选位点 5' }));
  expect(screen.getByText('已选原子 C5')).toBeTruthy();
  expect(container.querySelector('[data-atom-id="BO-01:atom:4"]')?.getAttribute('aria-pressed')).toBe('true');
  expect(container.querySelector('.molecule-atom-inspector')?.textContent).not.toContain('度数');
  fireEvent.click(screen.getByRole('button', { name: '选择候选位点 9' }));
  expect(container.querySelector('.molecule-atom-inspector')?.textContent).toContain('5 元环');
  expect(container.querySelector('.molecule-atom-inspector')?.textContent).not.toContain('6 元环');
  fireEvent.click(screen.getByRole('button', { name: '选择候选位点 5' }));
  expect(screen.queryByRole('button', { name: '记录关系' })).toBeNull();
  expect(screen.queryByRole('group', { name: '分子工作台视图' })).toBeNull();
  expect(screen.queryByRole('button', { name: '搜索节点' })).toBeNull();
  await screen.findByText('已选原子 C5');
  expect(container.querySelector('.molecule-selection')).toBeTruthy();
});

it('rechecks ownership when the dataset ID changes under the same session and display name', async () => {
  const props = { data: { nodes: [], edges: [] }, molecularCase: true, sessionId: 's', datasetName: '同名案例', tab: 'Graph' as const, onTab: vi.fn(), selected: null, onSelect: noSelect, collapsed: false, onToggle: vi.fn() };
  const { container, rerender } = render(<GraphWorkspace {...props} datasetId="dataset-a" />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  const previousSignal = vi.mocked(getBorylationCase).mock.calls[0][2]!;
  vi.mocked(getBorylationCase).mockRejectedValueOnce(new Error('dataset is not the molecular case'));
  rerender(<GraphWorkspace {...props} datasetId="dataset-b" />);
  expect(previousSignal.aborted).toBe(true);
  expect(container.querySelector('image')).toBeNull();
  await screen.findByRole('alert');
  expect(getBorylationCase).toHaveBeenCalledTimes(2);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('never enables molecular drawing for ordinary or schema graphs, even with atom-looking node names', async () => {
  const props = { data: { nodes: [{ id: 'BO-01:atom:4' }], edges: [] }, tab: 'Graph' as const, sessionId: 's', onTab: vi.fn(), selected: null, onSelect: noSelect, collapsed: false, onToggle: vi.fn() };
  const { rerender } = render(<GraphWorkspace {...props} />);
  expect(screen.queryByRole('group', { name: '分子工作台视图' })).toBeNull();
  rerender(<GraphWorkspace {...props} molecularCase data={{ ...props.data, visualization_kind: 'schema' }} />);
  expect(screen.queryByRole('group', { name: '分子工作台视图' })).toBeNull();
  await waitFor(() => expect(getBorylationCase).not.toHaveBeenCalled());
  expect(fetchMock).not.toHaveBeenCalled();
});

it('opens the saved molecular construction without a chat session and preserves atom inspection and zoom', async () => {
  const dataset: Dataset = { dataset_id: 'mol', project_id: null, name: '我的分子', original_filename: 'borylation-story.npz', file_type: 'npz', status: 'ready', source_kind: 'server', metadata: { source_relative_path: 'borylation-story.npz' } };
  const onClose = vi.fn();
  const { container } = render(<GraphConstructionPanel dataset={dataset} embedded onClose={onClose} onSaved={vi.fn()} />);
  await screen.findByRole('button', { name: '原子 O1，位点编号 1' });
  expect(screen.getByRole('region', { name: '分子图构图' })).toBeTruthy();
  expect(container.querySelectorAll('[data-atom-id]')).toHaveLength(22);
  expect(getDatasetMolecularStructure).toHaveBeenCalledExactlyOnceWith({ dataset_id: 'mol', project_id: null }, expect.any(AbortSignal));
  expect(getBorylationCase).not.toHaveBeenCalled();
  expect(getConstructionState).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '记录关系' })).toBeNull();
  expect(screen.queryByRole('button', { name: '保存构图结果' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '放大分子结构' }));
  expect((container.querySelector('.molecule-drawing') as SVGElement).style.width).toBe('150%');
  fireEvent.click(screen.getByRole('button', { name: '适应' }));
  expect((container.querySelector('.molecule-drawing') as SVGElement).style.width).toBe('100%');
  fireEvent.click(screen.getByRole('button', { name: '选择候选位点 9' }));
  expect(screen.getByText('已选原子 C9')).toBeTruthy();
  expect(screen.getByText('5 元环')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '返回构图' }));
  expect(onClose).toHaveBeenCalledOnce();
});

it('clears the construction drawing when switching to a dataset that fails verification', async () => {
  const { container, rerender } = render(<MoleculeCanvas dataset={{ dataset_id: 'first', project_id: null }} highlights={[]} selected={null} onSelect={noSelect} />);
  await screen.findByRole('button', { name: '选择候选位点 5' });
  const signal = vi.mocked(getDatasetMolecularStructure).mock.calls[0][1]!;
  vi.mocked(getDatasetMolecularStructure).mockRejectedValueOnce(new Error('wrong graph'));
  rerender(<MoleculeCanvas dataset={{ dataset_id: 'second', project_id: 'p' }} highlights={[]} selected={null} onSelect={noSelect} />);
  expect(signal.aborted).toBe(true);
  expect(container.querySelector('image')).toBeNull();
  await screen.findByRole('alert');
  expect(getDatasetMolecularStructure).toHaveBeenLastCalledWith({ dataset_id: 'second', project_id: 'p' }, expect.any(AbortSignal));
  expect(getBorylationCase).not.toHaveBeenCalled();
});
