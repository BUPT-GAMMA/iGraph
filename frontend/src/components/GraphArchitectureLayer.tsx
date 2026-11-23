import { useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import * as d3 from 'd3';
import { buildGraphArchitecture, architectureHighlights, type ArchitectureNode, type ArchitectureRelation } from '../graphArchitecture';
import { useI18n } from '../i18n';
import type { GraphData, Highlight } from '../types';
import { Icon } from './Icon';
import './GraphArchitectureLayer.css';

type Point = { x: number; y: number };
type Selection = { kind: 'node' | 'relation'; id: string };
type Placement = { id: string } & Point & d3.SimulationNodeDatum;
const NODE_WIDTH = 154;
const NODE_HEIGHT = 60;
const NODE_LIMIT = 64;
const RELATION_LIMIT = 120;
const colors = ['#477de1', '#1a9c8a', '#925ed0', '#b77b27', '#208fb2', '#c65472', '#66833a'];

function colorFor(id: string) {
  let hash = 0;
  for (const character of id) hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
  return colors[hash % colors.length];
}

function positionsFor(nodes: ArchitectureNode[], relations: ArchitectureRelation[]) {
  const radius = Math.max(137, nodes.length * 39);
  const count = nodes.length;
  const placed: Placement[] = nodes.map(({ id }, index) => {
    const angle = index * Math.PI * 2 / count - Math.PI / 2;
    const x = count === 1 ? 0 : count === 2 ? (index ? 122 : -122) : Math.cos(angle) * radius;
    const y = count <= 2 ? 0 : Math.sin(angle) * radius * .79;
    return { id, x, y };
  });
  if (count > 2) {
    // Parallel relationship types share a single spring.
    const pairs = new Map<string, { source: string; target: string }>();
    relations.forEach(({ source, target }) => {
      if (source !== target) pairs.set(JSON.stringify([source, target].sort()), { source, target });
    });
    d3.forceSimulation(placed).stop()
      .force('relationships', d3.forceLink<Placement, d3.SimulationLinkDatum<Placement>>([...pairs.values()]).id(node => node.id).distance(245).strength(.25))
      .force('spacing', d3.forceCollide<Placement>(99).strength(.94))
      .force('x', d3.forceX(0).strength(.022)).force('y', d3.forceY(0).strength(.035))
      .tick(110);
  }
  return new Map(placed.map(point => [point.id, point]));
}

function cardPort(center: Point, toward: Point, padding = 0): Point {
  const dx = toward.x - center.x, dy = toward.y - center.y;
  const ratio = Math.max(Math.abs(dx) / (NODE_WIDTH / 2 + padding), Math.abs(dy) / (NODE_HEIGHT / 2 + padding));
  return ratio ? { x: center.x + dx / ratio, y: center.y + dy / ratio } : center;
}

function relationGeometry(source: Point, target: Point, lane: number, reverse: boolean, self: boolean) {
  if (self) {
    const size = 64 + lane * 35;
    const x = source.x, y = source.y - NODE_HEIGHT / 2 - 2;
    return { path: `M${x - 38},${y} C${x - 77 - lane * 8},${y - size} ${x + 77 + lane * 8},${y - size} ${x + 39},${y}`,
      x, y: y - size * .75 };
  }
  const dx = target.x - source.x, dy = target.y - source.y;
  const offset = lane * 45 * (reverse ? -1 : 1) / (Math.hypot(dx, dy) || 1);
  const control = { x: (source.x + target.x) / 2 - dy * offset, y: (source.y + target.y) / 2 + dx * offset };
  const from = cardPort(source, control, 2), to = cardPort(target, control, 3);
  return { path: `M${from.x},${from.y} Q${control.x},${control.y} ${to.x},${to.y}`,
    x: (from.x + 2 * control.x + to.x) / 4, y: (from.y + 2 * control.y + to.y) / 4 };
}

function shortLabel(value: string, max: number) {
  let width = 0;
  let result = '';
  for (const char of value) {
    width += /^[\u0020-\u007e]$/.test(char) ? 1 : 1.8;
    if (width > max) return result + '…';
    result += char;
  }
  return result;
}

export function GraphArchitectureLayer({ data, highlights }: { data: GraphData; highlights: Highlight[] }) {
  const { language, t } = useI18n();
  const formatter = useMemo(() => new Intl.NumberFormat(language), [language]);
  const graph = useMemo(() => buildGraphArchitecture(data, t), [data, t]);
  const result = useMemo(() => architectureHighlights(data, highlights), [data, highlights]);
  const [query, setQuery] = useState('');
  const [selection, setSelection] = useState<Selection | null>(null);
  const [camera, setCamera] = useState(d3.zoomIdentity);
  const [draggedPositions, setDraggedPositions] = useState<{ key: string; points: Map<string, Point> }>();
  const svgRef = useRef<SVGSVGElement>(null);
  const viewportRef = useRef<SVGGElement>(null);
  const viewActions = useRef<{ fit: () => void; zoomBy: (factor: number) => void }>();
  const drag = useRef<{ id: string; pointerId: number; clientX: number; clientY: number; origin: Point; moved: boolean }>();
  const skipClick = useRef('');
  const changedCamera = useRef(false);
  const marker = 'architecture-arrow-' + useId().replace(/:/g, '');
  const nodeFallback = t('Unspecified type', '未声明类型');
  const relationFallback = t('Relationship', '关系');
  const nodeLabel = (node?: ArchitectureNode) => node?.label || nodeFallback;
  const relationLabel = (relation: ArchitectureRelation) => relation.label || relationFallback;
  const allNodes = useMemo(() => new Map(graph.nodes.map(node => [node.id, node])), [graph.nodes]);
  const search = query.trim().toLocaleLowerCase();
  const visible = useMemo(() => {
    const matched = graph.nodes.filter(item => (item.label || nodeFallback).toLocaleLowerCase().includes(search));
    const matchedIds = new Set(matched.map(item => item.id));
    const candidates = graph.relations.filter(edge => !search || matchedIds.has(edge.source) || matchedIds.has(edge.target)
      || (edge.label || relationFallback).toLocaleLowerCase().includes(search));
    const orderNodes = (items: ArchitectureNode[]) => [...items].sort((a, b) => Number(result.nodes.has(b.id)) - Number(result.nodes.has(a.id)));
    const nodes = new Map(orderNodes(matched).slice(0, NODE_LIMIT).map(item => [item.id, item]));
    const sorted = [...candidates].sort((a, b) => Number(result.relations.has(b.id)) - Number(result.relations.has(a.id)));
    const relations: ArchitectureRelation[] = [];
    for (const edge of sorted) {
      if (relations.length === RELATION_LIMIT) break;
      const missing = [...new Set([edge.source, edge.target])].filter(id => !nodes.has(id));
      if (missing.length + nodes.size > NODE_LIMIT) continue;
      missing.forEach(id => { const node = allNodes.get(id); if (node) nodes.set(id, node); });
      relations.push(edge);
    }
    return { nodes: [...nodes.values()], relations };
  }, [graph, search, nodeFallback, relationFallback, allNodes, result]);
  const topology = JSON.stringify([visible.nodes.map(node => node.id).sort(), visible.relations.map(relation => relation.id).sort()]);
  const basePositions = useMemo(() => positionsFor(visible.nodes, visible.relations), [topology]);
  const lanes = useMemo(() => {
    const groups = new Map<string, ArchitectureRelation[]>();
    visible.relations.forEach(relation => {
      const key = JSON.stringify([relation.source, relation.target].sort());
      const siblings = groups.get(key) || [];
      siblings.push(relation);
      groups.set(key, siblings);
    });
    const values = new Map<string, number>();
    groups.forEach(group => group.forEach((relation, index) => values.set(relation.id,
      relation.source === relation.target ? index : index - (group.length - 1) / 2)));
    return values;
  }, [visible.relations]);
  const position = (id: string): Point => (draggedPositions?.key === topology ? draggedPositions.points.get(id) : undefined) || basePositions.get(id)!;

  useLayoutEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current);
    changedCamera.current = false;
    const extent = (): [[number, number], [number, number]] => [[0, 0], [svg.node()?.clientWidth || 360, svg.node()?.clientHeight || 340]];
    const zoom = d3.zoom<SVGSVGElement, unknown>().extent(extent).scaleExtent([.1, 3.5])
      .filter(event => event.type === 'wheel' || (!event.button && !event.ctrlKey && !event.target?.closest?.('[data-architecture-item]')))
      .on('zoom', event => {
        if (event.sourceEvent) changedCamera.current = true;
        setCamera(event.transform);
      });
    svg.call(zoom).on('dblclick.zoom', null);
    function fit() {
      const points = [...basePositions.values()];
      if (!points.length) return;
      let left = Math.min(...points.map(point => point.x)) - NODE_WIDTH / 2;
      let right = Math.max(...points.map(point => point.x)) + NODE_WIDTH / 2;
      let top = Math.min(...points.map(point => point.y)) - NODE_HEIGHT / 2;
      let bottom = Math.max(...points.map(point => point.y)) + NODE_HEIGHT / 2;
      const box = viewportRef.current?.getBBox?.();
      if (box?.width && box.height) {
        left = box.x; right = box.x + box.width;
        top = box.y; bottom = box.y + box.height;
      }
      const [, [width, height]] = extent();
      const scale = Math.max(.1, Math.min(1.4, (width - 35) / (right - left), (height - 40) / (bottom - top)));
      svg.call(zoom.transform, d3.zoomIdentity.translate((width - scale * (left + right)) / 2, (height - scale * (top + bottom)) / 2).scale(scale));
    }
    viewActions.current = {
      fit: () => { changedCamera.current = false; fit(); },
      zoomBy: factor => { changedCamera.current = true; svg.call(zoom.scaleBy, factor); },
    };
    fit();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => { if (!changedCamera.current && !drag.current) fit(); });
    observer?.observe(svgRef.current);
    return () => { observer?.disconnect(); svg.on('.zoom', null); viewActions.current = undefined; };
  }, [basePositions]);

  function beginDrag(event: PointerEvent<SVGGElement>, id: string) {
    if (event.button !== 0 || drag.current) return;
    skipClick.current = '';
    drag.current = { id, pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, origin: position(id), moved: false };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function moveNode(event: PointerEvent<SVGGElement>) {
    const item = drag.current;
    if (!item || item.pointerId !== event.pointerId) return;
    const dx = event.clientX - item.clientX, dy = event.clientY - item.clientY;
    if (!item.moved && Math.hypot(dx, dy) < 4) return;
    item.moved = true;
    changedCamera.current = true;
    const location = { x: item.origin.x + dx / camera.k, y: item.origin.y + dy / camera.k };
    setDraggedPositions(previous => {
      const points = new Map(previous?.key === topology ? previous.points : undefined);
      points.set(item.id, location);
      return { key: topology, points };
    });
  }
  function endDrag(event: PointerEvent<SVGGElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    if (drag.current.moved) skipClick.current = drag.current.id;
    drag.current = undefined;
  }
  function select(item: Selection) {
    setSelection(current => current?.kind === item.kind && current.id === item.id ? null : item);
  }
  function keyboard(event: KeyboardEvent<SVGGElement>, item: Selection) {
    if (event.key === 'Escape' && selection) { event.stopPropagation(); setSelection(null); }
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(item); }
  }

  const pickedNode = selection?.kind === 'node' ? visible.nodes.find(node => node.id === selection.id) : undefined;
  const pickedRelation = selection?.kind === 'relation' ? visible.relations.find(relation => relation.id === selection.id) : undefined;
  const adjacent = pickedNode ? visible.relations.filter(edge => edge.source === pickedNode.id || edge.target === pickedNode.id) : [];
  const activeNodes = new Set(pickedRelation ? [pickedRelation.source, pickedRelation.target]
    : pickedNode ? [pickedNode.id, ...adjacent.flatMap(edge => [edge.source, edge.target])] : []);
  const activeRelations = new Set(pickedRelation ? [pickedRelation.id] : adjacent.map(edge => edge.id));
  const details = pickedNode || pickedRelation;
  const relationContext = (relation: ArchitectureRelation) => nodeLabel(allNodes.get(relation.source))
    + (graph.directed ? ' → ' : ' — ') + nodeLabel(allNodes.get(relation.target));
  const bounded = visible.nodes.length < graph.nodes.length || visible.relations.length < graph.relations.length;
  const countText = bounded
    ? t(`${visible.nodes.length}/${graph.nodes.length} types · ${visible.relations.length}/${graph.relations.length} relations`, `${visible.nodes.length}/${graph.nodes.length} 种节点 · ${visible.relations.length}/${graph.relations.length} 种关系`)
    : t(`${graph.nodes.length} node types · ${graph.relations.length} relationship types`, `${graph.nodes.length} 种节点 · ${graph.relations.length} 种关系`);

  return <section className="graph-architecture" aria-label={t('Graph architecture', '图的架构层')}>
    <div className="architecture-toolbar">
      <div className="architecture-search"><Icon name="search" size={15} />
        <input type="search" aria-label={t('Search node and relationship types', '搜索节点类型或关系')} placeholder={t('Find a node type or relationship', '搜索节点类型或关系')}
          value={query} onChange={event => { setQuery(event.target.value); setSelection(null); }} />
      </div>
      <button type="button" className="architecture-tool" onClick={() => viewActions.current?.fit()} disabled={!visible.nodes.length}>{t('Fit to canvas', '适应画布')}</button>
    </div>

    <div className="architecture-canvas">
      <svg ref={svgRef} aria-label={t('Architecture canvas. Drag types to move them and use the mouse wheel to zoom.', '架构层画布，可拖动节点并使用滚轮缩放')}
        onClick={event => { if (event.target === event.currentTarget) setSelection(null); }}>
        <defs>
          <marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10Z" fill="context-stroke" />
          </marker>
        </defs>
        <g ref={viewportRef} transform={camera.toString()}>
          {visible.relations.map(relation => {
            const geometry = relationGeometry(position(relation.source), position(relation.target), lanes.get(relation.id) || 0,
              relation.source > relation.target, relation.source === relation.target);
            const label = shortLabel(relationLabel(relation), 22);
            const name = relationLabel(relation) + ' · ' + relationContext(relation);
            const highlighted = result.relations.has(relation.id) || activeRelations.has(relation.id);
            const labelWidth = Math.min(155, Math.max(45, [...label].reduce((width, char) => width + (/[ -~]/.test(char) ? 6.2 : 10.5), 0) + 16));
            const selected = pickedRelation?.id === relation.id;
            const item = { kind: 'relation', id: relation.id } as const;
            return <g key={relation.id} data-architecture-item="relation" className="architecture-link" tabIndex={0} role="button"
              aria-label={name} aria-pressed={selected} data-highlighted={highlighted} data-muted={Boolean(details && !highlighted)}
              onClick={() => select(item)} onKeyDown={event => keyboard(event, item)}>
              <title>{name}</title>
              <path className="architecture-link-hit" d={geometry.path} />
              <path className="architecture-link-line" d={geometry.path} markerEnd={graph.directed ? `url(#${marker})` : undefined} />
              {(camera.k >= .55 || visible.relations.length <= 12 || highlighted) && <g className="architecture-link-label" transform={`translate(${geometry.x},${geometry.y})`}>
                <rect x={-labelWidth / 2} y={-10} width={labelWidth} height={20} rx={9} />
                <text textAnchor="middle" dominantBaseline="central">{label}</text>
              </g>}
            </g>;
          })}
          {visible.nodes.map(node => {
            const point = position(node.id);
            const selected = pickedNode?.id === node.id;
            const inResult = result.nodes.has(node.id);
            const active = activeNodes.has(node.id);
            const item = { kind: 'node', id: node.id } as const;
            const name = nodeLabel(node);
            const formatted = formatter.format(node.count);
            return <g key={node.id} data-architecture-item="node" className="architecture-node" role="button" tabIndex={0}
              aria-label={t(`Node type ${name}, ${formatted} instances`, `节点类型 ${name}，${formatted} 个实例`)}
              aria-pressed={selected} data-highlighted={inResult || active} data-muted={Boolean(details && !inResult && !active)}
              transform={`translate(${point.x},${point.y})`}
              onPointerDown={event => beginDrag(event, node.id)} onPointerMove={moveNode} onPointerUp={endDrag}
              onPointerCancel={endDrag} onLostPointerCapture={endDrag}
              onClick={() => { if (skipClick.current === node.id) skipClick.current = ''; else select(item); }}
              onKeyDown={event => keyboard(event, item)}>
              <title>{name}</title>
              <rect className="architecture-node-card" x={-NODE_WIDTH / 2} y={-NODE_HEIGHT / 2} width={NODE_WIDTH} height={NODE_HEIGHT} rx={12} />
              <rect x="-66" y="-14" width="28" height="28" rx="8" fill={colorFor(node.id)} opacity=".15" />
              <circle cx="-52" cy="0" r="5.3" fill={colorFor(node.id)} />
              <circle cx="-52" cy="0" r="9" stroke={colorFor(node.id)} strokeOpacity=".52" fill="none" />
              <text x="-30" y="-4" className="architecture-node-title">{shortLabel(name, 14)}</text>
              <text x="-30" y="14" className="architecture-node-count">{t(`${formatted} instances`, `${formatted} 个实例`)}</text>
            </g>;
          })}
        </g>
      </svg>
      {visible.nodes.length ? <div className="architecture-zoom" role="group" aria-label={t('Canvas zoom', '画布缩放')}>
        <button type="button" aria-label={t('Zoom out', '缩小')} onClick={() => viewActions.current?.zoomBy(.8)}>−</button>
        <output>{Math.round(camera.k * 100)}%</output>
        <button type="button" aria-label={t('Zoom in', '放大')} onClick={() => viewActions.current?.zoomBy(1.25)}>+</button>
      </div> : <div className="architecture-empty">{search ? t('No matching types in this graph.', '没有匹配的节点或关系类型。') : t('Load a graph to see its architecture.', '加载图后即可查看架构。')}</div>}
    </div>

    <div className="architecture-summary" role="status">
      <span>{countText}</span>
      {graph.partial && <span className="architecture-sample">{t('From loaded sample', '基于已加载样本')}</span>}
      {result.nodes.size + result.relations.size > 0 && <span className="architecture-result"><i />{t('Analysis highlight', '分析结果高亮')}</span>}
    </div>
    {details && <div className="architecture-inspector">
      <header>
        <div><small>{pickedNode ? t('Node type', '节点类型') : t('Relationship type', '关系类型')}</small>
          <strong>{pickedNode ? nodeLabel(pickedNode) : pickedRelation && relationLabel(pickedRelation)}</strong></div>
        <button type="button" className="plain-icon" onClick={() => setSelection(null)} aria-label={t('Clear highlight', '清除高亮')}><Icon name="close" size={16} /></button>
      </header>
      <p>{pickedRelation && <>{relationContext(pickedRelation)} · </>}
        {pickedNode ? t(`${formatter.format(pickedNode.count)} instances`, `${formatter.format(pickedNode.count)} 个实例`)
          : t(`${formatter.format(details.count)} relationships`, `${formatter.format(details.count)} 条关系`)}</p>
      {adjacent.length > 0 && <div className="architecture-neighbor-buttons">{adjacent.map(edge => <button key={edge.id} type="button"
        title={relationContext(edge)} onClick={() => setSelection({ kind: 'relation', id: edge.id })}>{relationLabel(edge)}</button>)}</div>}
      {details.fields.length > 0 && <details><summary>{t(`${details.fields.length} fields`, `${details.fields.length} 个属性字段`)}</summary>
        <div className="architecture-field-list">{details.fields.map(field => <code key={field}>{field}</code>)}</div>
      </details>}
    </div>}
  </section>;
}
