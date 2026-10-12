import { useEffect, useMemo, useState } from 'react';
import { getBorylationCase, type BorylationCaseResponse } from '../api';
import type { Highlight } from '../types';
import { endpointId } from '../graphAppearance';
import { useI18n } from '../i18n';
import './MoleculeCanvas.css';

const MOLECULE_ID = 'BO-01';
const MOLECULE_SMILES = 'O=C(Nc1ccc(-c2csnn2)cc1)c1c(F)cccc1Cl';
const DEPICTION_URL = '/examples/borylation/BO-01-depiction.json';
const SVG_URL = '/examples/borylation/BO-01-molecule.svg';

export type DepictionAtom = {
  atom_index: number; display_number: number; node_id: string; x: number; y: number;
  element: string; aromatic: boolean; total_hydrogens: number; candidate_ch: boolean;
  formal_charge?: number; symmetry_class?: number; observed_site?: boolean; ring_sizes?: number[];
};
export type MoleculeDepiction = {
  version: string; case_id: string; molecule_id: string; input_smiles: string;
  width: number; height: number; viewBox: number[]; svg_url: string; svg_sha256: string;
  atoms: DepictionAtom[]; candidate_node_ids: string[];
  bonds: Array<{ begin: number; end: number; bond_type: string }>;
};
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value);

/** Match the static drawing to the owned session before exposing any atom map. */
export function validateMoleculeDepiction(value: unknown, response: BorylationCaseResponse): MoleculeDepiction {
  const data = object(value), result = object(response.result);
  if (!response.success || response.step !== 'structure' || result.molecule_id !== MOLECULE_ID || result.smiles !== MOLECULE_SMILES
    || data.version !== 'borylation.depiction.v1' || data.case_id !== 'borylation-bo01-v1'
    || data.molecule_id !== result.molecule_id || data.input_smiles !== result.smiles) throw new Error('molecule-identity-mismatch');
  if (data.svg_url !== SVG_URL || typeof data.svg_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(data.svg_sha256)
    || typeof data.width !== 'number' || !Number.isFinite(data.width) || data.width <= 0
    || typeof data.height !== 'number' || !Number.isFinite(data.height) || data.height <= 0
    || !Array.isArray(data.viewBox) || data.viewBox.length !== 4 || data.viewBox[0] !== 0 || data.viewBox[1] !== 0
    || data.viewBox[2] !== data.width || data.viewBox[3] !== data.height) throw new Error('invalid-drawing-frame');
  if (!Array.isArray(data.atoms) || data.atoms.length !== result.heavy_atom_count || data.atoms.length !== 22
    || !Array.isArray(result.visualization_node_names)) throw new Error('invalid-atom-map');
  const ownedIds = new Set(result.visualization_node_names);
  const indices = new Set<number>(), byId = new Map<string, DepictionAtom>();
  for (const raw of data.atoms) {
    const atom = object(raw);
    if (!integer(atom.atom_index) || atom.atom_index < 0 || atom.atom_index >= 22 || indices.has(atom.atom_index)
      || atom.display_number !== atom.atom_index + 1 || atom.node_id !== `${MOLECULE_ID}:atom:${atom.atom_index}` || !ownedIds.has(atom.node_id)
      || typeof atom.element !== 'string' || !/^[A-Z][a-z]?$/.test(atom.element) || typeof atom.aromatic !== 'boolean'
      || !integer(atom.total_hydrogens) || atom.total_hydrogens < 0 || typeof atom.candidate_ch !== 'boolean'
      || (atom.ring_sizes !== undefined && (!Array.isArray(atom.ring_sizes) || !atom.ring_sizes.every(size => integer(size) && size >= 3)))
      || typeof atom.x !== 'number' || !Number.isFinite(atom.x) || atom.x < 0 || atom.x > data.width
      || typeof atom.y !== 'number' || !Number.isFinite(atom.y) || atom.y < 0 || atom.y > data.height) throw new Error('invalid-atom-map');
    indices.add(atom.atom_index);
    byId.set(atom.node_id, atom as DepictionAtom);
  }
  if (!Array.isArray(result.candidate_atoms) || !Array.isArray(data.candidate_node_ids)
    || result.candidate_atoms.length !== result.aromatic_ch_count || result.candidate_atoms.length !== 8) throw new Error('invalid-candidate-map');
  const candidateIds = new Set<string>();
  for (const raw of result.candidate_atoms) {
    const candidate = object(raw), id = `${MOLECULE_ID}:atom:${candidate.atom_index}`, atom = byId.get(id);
    if (!atom || candidateIds.has(id) || candidate.display_number !== atom.display_number || candidate.element !== atom.element
      || candidate.aromatic !== atom.aromatic || candidate.total_hydrogens !== atom.total_hydrogens
      || atom.element !== 'C' || !atom.aromatic || atom.total_hydrogens < 1 || !atom.candidate_ch) throw new Error('invalid-candidate-map');
    candidateIds.add(id);
  }
  if (data.candidate_node_ids.length !== candidateIds.size || !data.candidate_node_ids.every(id => typeof id === 'string' && candidateIds.has(id))
    || new Set(data.candidate_node_ids).size !== candidateIds.size
    || [...byId.values()].some(atom => atom.element === 'C' && atom.aromatic && atom.total_hydrogens > 0 && !candidateIds.has(atom.node_id))) throw new Error('invalid-candidate-map');
  if (!Array.isArray(data.bonds) || data.bonds.length !== result.chemical_bond_count || data.bonds.length !== 24) throw new Error('invalid-bonds');
  const bonds = new Set<string>();
  for (const raw of data.bonds) {
    const bond = object(raw);
    if (!integer(bond.begin) || !integer(bond.end) || !indices.has(bond.begin) || !indices.has(bond.end) || bond.begin === bond.end
      || !['SINGLE', 'DOUBLE', 'TRIPLE', 'AROMATIC'].includes(String(bond.bond_type))) throw new Error('invalid-bonds');
    const key = [bond.begin, bond.end].sort((a, b) => a - b).join(':');
    if (bonds.has(key)) throw new Error('invalid-bonds');
    bonds.add(key);
  }
  return data as MoleculeDepiction;
}

export function moleculeHighlightLayers(highlights: Highlight[], depiction: MoleculeDepiction) {
  const atoms = new Map(depiction.atoms.map(atom => [atom.node_id, atom]));
  return highlights.filter(item => item.id !== 'highlighted-node-edges').map((item, index) => {
    const ids = new Set((item.nodes || []).map(endpointId));
    for (const edge of item.edges || []) {
      const raw = object(edge);
      ids.add(endpointId(Array.isArray(edge) ? edge[0] : raw.src ?? raw.source));
      ids.add(endpointId(Array.isArray(edge) ? edge[1] : raw.dst ?? raw.target));
    }
    return { id: item.id || `molecular-layer-${index}`, label: item.label || '', color: item.color || '#ef4444',
      atoms: [...ids].flatMap(id => atoms.has(id) ? [atoms.get(id)!] : []).sort((a, b) => a.display_number - b.display_number) };
  }).filter(layer => layer.atoms.length);
}

type Props = { sessionId: string; datasetKey?: string; highlights: Highlight[]; selected: Record<string, unknown> | null; onSelect: (node: Record<string, unknown> | null) => void };

export function MoleculeCanvas({ sessionId, datasetKey = '', highlights, selected, onSelect }: Props) {
  const { t } = useI18n();
  const identity = `${sessionId}\u0000${datasetKey}`;
  const [loaded, setLoaded] = useState<{ identity: string; depiction: MoleculeDepiction } | null>(null);
  const [failure, setFailure] = useState<{ identity: string; message: string } | null>(null);
  const [reload, setReload] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [layerIndex, setLayerIndex] = useState(-1);
  const depiction = loaded?.identity === identity ? loaded.depiction : null;
  const error = failure?.identity === identity ? failure.message : '';

  useEffect(() => {
    const controller = new AbortController();
    setLoaded(null); setFailure(null); setZoom(1); setLayerIndex(-1);
    if (!sessionId) return () => controller.abort();
    void (async () => {
      const owned = await getBorylationCase(sessionId, 'structure', controller.signal);
      if (controller.signal.aborted) return;
      if (!owned.success || owned.step !== 'structure' || owned.result?.molecule_id !== MOLECULE_ID || owned.result?.smiles !== MOLECULE_SMILES) throw new Error('molecule-identity-mismatch');
      const asset = await fetch(DEPICTION_URL, { signal: controller.signal, cache: 'no-cache' });
      if (!asset.ok) throw new Error('depiction-unavailable');
      const valid = validateMoleculeDepiction(await asset.json(), owned);
      if (!controller.signal.aborted) setLoaded({ identity, depiction: valid });
    })().catch(cause => {
      if (!controller.signal.aborted) setFailure({ identity, message: cause instanceof Error ? cause.message : 'depiction-unavailable' });
    });
    return () => controller.abort();
  }, [sessionId, identity, reload]);

  const layers = useMemo(() => depiction ? moleculeHighlightLayers(highlights, depiction) : [], [highlights, depiction]);
  useEffect(() => { setLayerIndex(-1); }, [highlights]);
  const shownLayers = layerIndex >= 0 && layers[layerIndex] ? [layers[layerIndex]] : layers;
  if (!depiction) return <section className="molecule-canvas molecule-empty" aria-label={t('Molecular structure', '分子结构')}>
    {error ? <div role="alert"><strong>{t('Unable to display the molecular structure', '无法显示分子结构')}</strong><p>{t('Retry or switch to record relationships.', '请重试，或切换到记录关系查看。')}</p><button onClick={() => setReload(value => value + 1)}>{t('Retry', '重试')}</button></div>
      : <p role="status">{sessionId ? t('Loading molecular structure…', '正在加载分子结构…') : t('Start a conversation to view the molecule.', '进入会话后可查看分子结构。')}</p>}
  </section>;

  const candidateIds = new Set(depiction.candidate_node_ids);
  const candidates = depiction.atoms.filter(atom => candidateIds.has(atom.node_id)).sort((a, b) => a.display_number - b.display_number);
  const selectedAtom = depiction.atoms.find(atom => atom.node_id === String(selected?.id ?? ''));
  const selectAtom = (atom: DepictionAtom) => onSelect({ id: atom.node_id, label: `${atom.element}${atom.display_number}`, attrs: { type: 'atom', properties: { ...atom, label: `${atom.element}${atom.display_number}` } } });
  const atomLabel = (atom: DepictionAtom) => t(`Atom ${atom.element}${atom.display_number}, site number ${atom.display_number}`, `原子 ${atom.element}${atom.display_number}，位点编号 ${atom.display_number}`);
  return <section className="molecule-canvas" aria-label={t('Molecular structure', '分子结构')} data-molecule-id={depiction.molecule_id} data-chemical-atom-count={depiction.atoms.length} data-chemical-bond-count={depiction.bonds.length}>
    <div className="molecule-toolbar"><strong>{depiction.molecule_id}</strong><span>{t('22 heavy atoms · 24 chemical bonds', '22 个重原子 · 24 条化学键')}</span>
      <button aria-label={t('Zoom out molecule', '缩小分子结构')} disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - .5))}>−</button>
      <button aria-label={t('Zoom in molecule', '放大分子结构')} disabled={zoom >= 3} onClick={() => setZoom(value => Math.min(3, value + .5))}>+</button>
      <button onClick={() => setZoom(1)}>{t('Fit', '适应')}</button>
    </div>
    <div className="molecule-candidate-sites"><span>{t('Candidate aromatic C–H sites', '候选芳香 C–H 位点')}</span><div>{candidates.map(atom => <button key={atom.node_id} aria-label={t(`Select candidate site ${atom.display_number}`, `选择候选位点 ${atom.display_number}`)} aria-pressed={selectedAtom?.node_id === atom.node_id} onClick={() => selectAtom(atom)}>{atom.display_number}</button>)}</div></div>
    {layers.length > 1 && <label className="molecule-layer-control">{t('Highlight layer', '高亮图层')}<select aria-label={t('Molecular highlight layer', '分子高亮图层')} value={layerIndex} onChange={event => setLayerIndex(Number(event.target.value))}><option value={-1}>{t('All highlights', '全部高亮')}</option>{layers.map((layer, index) => <option value={index} key={layer.id}>{layer.label || t(`Highlight ${index + 1}`, `高亮 ${index + 1}`)}</option>)}</select></label>}
    <div className="molecule-scroll"><svg className="molecule-drawing" viewBox={depiction.viewBox.join(' ')} style={{ width: `${zoom * 100}%` }} aria-label={t('Numbered BO-01 chemical structure', '带原子编号的 BO-01 化学结构')}>
      <image href={`${depiction.svg_url}?v=${depiction.svg_sha256}`} x="0" y="0" width={depiction.width} height={depiction.height} onError={() => { setLoaded(null); setFailure({ identity, message: 'depiction-image-unavailable' }); }} />
      <g className="molecule-candidates" aria-hidden="true">{candidates.map(atom => <circle key={atom.node_id} cx={atom.x} cy={atom.y} r="17" className="molecule-candidate" data-candidate-id={atom.node_id} />)}</g>
      <g className="molecule-highlight-layers" aria-hidden="true">{shownLayers.flatMap((layer, index) => layer.atoms.map(atom => <circle key={`${layer.id}:${atom.node_id}`} cx={atom.x} cy={atom.y} r={21 + Math.min(index, 5) * 4} className="molecule-current-highlight" fill={layer.color} stroke={layer.color} data-highlight-node-id={atom.node_id} data-display-number={atom.display_number} data-highlight-color={layer.color} data-highlight-label={layer.label} />))}</g>
      {selectedAtom && <circle className="molecule-selection" cx={selectedAtom.x} cy={selectedAtom.y} r="30" aria-hidden="true" />}
      <g className="molecule-atoms">{depiction.atoms.map(atom => <g key={atom.node_id} role="button" tabIndex={0} aria-label={atomLabel(atom)} aria-pressed={selectedAtom?.node_id === atom.node_id} data-atom-id={atom.node_id} data-atom-index={atom.atom_index} data-display-number={atom.display_number} className="molecule-atom-target" onClick={() => selectAtom(atom)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectAtom(atom); } }}><title>{atom.element}{atom.display_number} · {atom.node_id}</title><circle cx={atom.x} cy={atom.y} r="18" /></g>)}</g>
    </svg></div>
    <div className="molecule-legend"><span><i className="molecule-candidate-key" />{t('Candidate sites', '候选位点')}</span>{shownLayers.map(layer => <span key={layer.id}><i style={{ background: layer.color }} />{layer.label || t('Current highlight', '当前高亮')}：{layer.atoms.map(atom => atom.display_number).join(', ')}</span>)}</div>
    <div className="molecule-atom-inspector" aria-live="polite">{selectedAtom ? <><strong>{t(`Selected atom ${selectedAtom.element}${selectedAtom.display_number}`, `已选原子 ${selectedAtom.element}${selectedAtom.display_number}`)}</strong><dl>
      <div><dt>{t('Element', '元素')}</dt><dd>{selectedAtom.element}</dd></div><div><dt>{t('Site number', '位点编号')}</dt><dd>{selectedAtom.display_number}</dd></div>
      <div><dt>{t('Aromatic', '芳香性')}</dt><dd>{selectedAtom.aromatic ? t('Yes', '是') : t('No', '否')}</dd></div><div><dt>{t('Attached hydrogens', '所连氢数')}</dt><dd>{selectedAtom.total_hydrogens}</dd></div><div><dt>{t('Candidate site', '候选位点')}</dt><dd>{candidateIds.has(selectedAtom.node_id) ? t('Yes', '是') : t('No', '否')}</dd></div>
      {selectedAtom.ring_sizes && <div><dt>{t('Ring membership', '所属环')}</dt><dd>{selectedAtom.ring_sizes.length ? selectedAtom.ring_sizes.map(size => t(`${size}-membered ring`, `${size} 元环`)).join('、') : t('Not in a ring', '不在环中')}</dd></div>}
    </dl></> : <span>{t('Select an atom or a site number to view its properties.', '点击原子或编号，查看原子信息。')}</span>}</div>
  </section>;
}
