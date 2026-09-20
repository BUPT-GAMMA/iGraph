import { useEffect, useId, useRef } from 'react';
import * as d3 from 'd3';
import type { Highlight } from '../types';
import type { GraphPreview, PreviewNode } from '../graphPreview';
import { degreeRadius } from '../graphPreview';
import { labelBudget } from '../graphLabels';
import { communityColor, DEFAULT_HIGHLIGHT_COLORS, highlightEdgePairs, highlightedEdgeColor, paddedHullPath, schemaEdgeLabel } from '../graphAppearance';
import { useI18n } from '../i18n';
import { highlightNodeIds } from '../graphExploration';

type Props = {
  active: boolean; schema?: boolean; preview: GraphPreview; directed?: boolean; highlights: Highlight[]; communities: Map<string, string>;
  selected: Record<string, unknown> | null; onSelect: (node: Record<string, unknown> | null) => void;
  layout: 'force' | 'radial' | 'circle'; sizeMode: 'degree' | 'uniform';
  labels: 'auto' | 'all' | 'hidden'; edgeWidth: number; fitVersion: number; focusId: string; state?: GraphCanvasState;
};
type Node = PreviewNode & d3.SimulationNodeDatum;
export type GraphCanvasState = {
  camera: d3.ZoomTransform;
  positions: Map<string, { x: number; y: number }>;
  topologySignature: string;
  layout: Props['layout'] | '';
  appliedFitVersion: number;
  appliedFocusId: string;
};
export function createGraphCanvasState(): GraphCanvasState {
  return { camera: d3.zoomIdentity, positions: new Map(), topologySignature: '', layout: '', appliedFitVersion: 0, appliedFocusId: '' };
}
export function GraphCanvas(props: Props) {
  const { t } = useI18n();
  const ref = useRef<SVGSVGElement>(null);
  const latest = useRef(props); latest.current = props;
  const controller = useRef<{ paint: () => void; fit: () => void; focus: () => void; resizeNodes: () => void }>();
  const localState = useRef<GraphCanvasState>(createGraphCanvasState());
  const state = props.state || localState.current;
  const marker = `graph-arrow-${useId().replace(/:/g, '')}`;
  // Compare only the bounded preview. Parent chat updates never restart physics.
  const signature = JSON.stringify([props.preview.nodes, props.preview.edges, props.directed, props.schema, props.highlights, [...props.communities]]);
  // Labels, translations, highlights and colors may rebuild SVG appearance, but
  // only topology and an explicit layout change are allowed to restart physics.
  const topologySignature = JSON.stringify([
    props.preview.nodes.map(node => node.id).sort(),
    props.preview.edges.map(edge => JSON.stringify([edge.source, edge.target].sort())).sort(),
    Boolean(props.schema),
  ]);
  useEffect(() => {
    if (!ref.current || !props.active) return;
    const svg = d3.select(ref.current); svg.selectAll('*').remove();
    const host = ref.current.parentElement!;
    let width = host.clientWidth || 360, height = host.clientHeight || 420;
    let disposed = false, frame = 0, interacted = false, zooming = false, needsPaint = false;
    let hovered = '', dragging = false, lastLabels = 0;
    const p = latest.current;
    const nodes: Node[] = p.preview.nodes.map(node => ({ ...node, ...state.positions.get(node.id) }));
    const canResume = state.topologySignature === topologySignature
      && state.layout === p.layout
      && nodes.length > 0
      && nodes.every(node => state.positions.has(node.id));
    const byId = new Map(nodes.map(node => [node.id, node]));
    const maxDegree = p.preview.maxDegree;
    const radius = (n: Node) => latest.current.sizeMode === 'uniform' ? 8 : degreeRadius(n.degree, maxDegree);
    const edges = p.preview.edges.map(edge => ({ ...edge, source: edge.source as string | Node, target: edge.target as string | Node, lane: 0, count: 1 }));
    const groups = new Map<string, typeof edges>();
    edges.forEach(edge => { const key = JSON.stringify([String(edge.source), String(edge.target)].sort()); const list = groups.get(key) || []; list.push(edge); groups.set(key, list); });
    groups.forEach(group => group.forEach((edge, i) => { edge.lane = i - (group.length - 1) / 2; edge.count = group.length; }));
    const neighbors = new Map<string, Set<string>>();
    p.preview.edges.forEach(edge => {
      if (!neighbors.has(edge.source)) neighbors.set(edge.source, new Set());
      if (!neighbors.has(edge.target)) neighbors.set(edge.target, new Set());
      neighbors.get(edge.source)!.add(edge.target); neighbors.get(edge.target)!.add(edge.source);
    });
    if (p.directed) svg.append('defs').append('marker').attr('id', marker).attr('viewBox', '0 -5 10 10').attr('refX', 9).attr('refY', 0).attr('markerWidth', 5).attr('markerHeight', 5).attr('orient', 'auto').append('path').attr('d', 'M0,-4L9,0L0,4Z').attr('fill', 'context-stroke');
    const viewport = svg.append('g').attr('class', 'graph-viewport');
    const visibleHighlights = p.highlights;
    const pathPairs = new Map<string, string>(), subPairs = new Map<string, string>(), nodeColors = new Map<string, string>();
    visibleHighlights.forEach(h => {
      highlightNodeIds([h]).forEach(id => nodeColors.set(id, h.color || DEFAULT_HIGHLIGHT_COLORS[h.type || 'node']));
      if (h.type === 'path' || h.type === 'subgraph') highlightEdgePairs(h).forEach(key => (h.type === 'path' ? pathPairs : subPairs).set(key, h.color || DEFAULT_HIGHLIGHT_COLORS[h.type!]));
    });
    const hulls = visibleHighlights.filter(h => h.type === 'subgraph').map(h => ({ h, path: viewport.append('path').attr('class', 'graph-subgraph-hull').attr('fill', h.color || DEFAULT_HIGHLIGHT_COLORS.subgraph).attr('stroke', h.color || DEFAULT_HIGHLIGHT_COLORS.subgraph) }));
    const link = viewport.append('g').selectAll('path').data(edges).join('path').attr('class', 'graph-link').attr('fill', 'none')
      .classed('graph-schema-link', Boolean(p.schema))
      .attr('aria-label', edge => p.schema ? schemaEdgeLabel(edge.raw, false, t) : null)
      .attr('marker-end', p.directed ? `url(#${marker})` : null)
      .style('stroke', edge => highlightedEdgeColor(pathPairs, edge.source, edge.target, p.directed) || highlightedEdgeColor(subPairs, edge.source, edge.target, p.directed) || null)
      .classed('is-path-highlight', e => Boolean(highlightedEdgeColor(pathPairs, e.source, e.target, p.directed)))
      .classed('is-subgraph-highlight', e => !highlightedEdgeColor(pathPairs, e.source, e.target, p.directed) && Boolean(highlightedEdgeColor(subPairs, e.source, e.target, p.directed)));
    const relationshipLabels = p.schema ? viewport.append('g').selectAll('text').data(edges).join('text')
      .attr('class', 'graph-schema-edge-label').text(edge => schemaEdgeLabel(edge.raw, true, t))
      .attr('aria-label', edge => schemaEdgeLabel(edge.raw, false, t)) : null;
    const node = viewport.append('g').selectAll<SVGGElement, Node>('g').data(nodes).join('g').attr('class', 'graph-node').attr('data-node-id', n => n.id)
      .attr('tabindex', 0).attr('role', 'button').attr('aria-label', n => t(`${n.label ?? n.id}, ${n.degreeLabel} ${n.degree}`, `${n.label ?? n.id}，${n.degreeLabel} ${n.degree}`));
    const circles = node.append('circle').attr('class', 'graph-node-dot').attr('fill', n => communityColor(p.communities.get(n.id)))
      .style('stroke', n => nodeColors.get(n.id) || null).classed('is-highlighted', n => nodeColors.has(n.id));
    const halos = node.append('circle').attr('class', 'graph-node-halo').attr('fill', n => nodeColors.get(n.id) || DEFAULT_HIGHLIGHT_COLORS.node).attr('opacity', .18);
    const rings = node.append('circle').attr('class', 'graph-selection-ring').attr('fill', 'none').attr('pointer-events', 'none');
    const labels = node.append('text').attr('class', 'graph-node-label');
    let budget = 0, visibleLabels = new Set<string>();
    const fullName = (n: Node) => String(n.label ?? n.id);
    function paintLabels() {
      lastLabels = performance.now();
      const { labels: mode, selected, preview } = latest.current;
      const focus = hovered || String(selected?.id ?? '');
      const related = neighbors.get(focus);
      const k = state.camera.k;
      budget = labelBudget(k, budget, nodes.length);
      const count = mode === 'all' || p.schema ? nodes.length : budget;
      const priority = [...nodes].sort((a, b) => {
        const score = (n: Node) => n.id === focus ? 1e12 : preview.matches.has(n.id) || nodeColors.has(n.id) ? 1e11 : related?.has(n.id) ? 1e10 + n.degree : (visibleLabels.has(n.id) ? 1e9 : 0) + n.degree;
        return score(b) - score(a);
      });
      const visible = new Set<string>(); const boxes: number[][] = [];
      for (const n of priority) {
        if (mode === 'hidden' && n.id !== focus) continue;
        if (visible.size >= count && n.id !== focus) continue;
        const name = fullName(n), text = n.id === focus ? name : name.length > 18 ? name.slice(0, 17) + '…' : name;
        const x = (n.x || 0) * k + state.camera.x, y = (n.y || 0) * k + state.camera.y + (radius(n) + 12) * Math.sqrt(k);
        const w = Math.min(text.length * 8, 440), padding = visibleLabels.has(n.id) ? 0 : 3;
        const box = [x - w / 2 - padding, y - 11 - padding, x + w / 2 + padding, y + 4 + padding];
        if (mode !== 'all' && n.id !== focus && boxes.some(b => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
        if (x < -w || x > width + w || y < -20 || y > height + 20) continue;
        visible.add(n.id); boxes.push(box);
      }
      labels.each(function(n) {
        const text = n.id === focus ? fullName(n) : fullName(n).length > 18 ? fullName(n).slice(0, 17) + '…' : fullName(n);
        if (this.textContent !== text) this.textContent = text;
        const opacity = visible.has(n.id) ? '1' : '0';
        if (this.style.opacity !== opacity) {
          this.style.opacity = opacity;
          this.style.visibility = visible.has(n.id) ? 'visible' : 'hidden';
        }
      });
      // Hidden labels do not need font/position writes on every zoom frame.
      const fontSize = `${11 / Math.sqrt(k)}px`;
      labels.each(function(n) {
        if (!visible.has(n.id)) return;
        if (this.style.fontSize !== fontSize) this.style.fontSize = fontSize;
        const dy = String((radius(n) + 12) / Math.sqrt(k));
        if (this.getAttribute('dy') !== dy) this.setAttribute('dy', dy);
      });
      visibleLabels = visible;
    }
    function paint() {
      if (disposed) return;
      const focus = hovered || String(latest.current.selected?.id ?? '');
      const related = neighbors.get(focus), active = byId.has(focus);
      node.classed('is-muted', n => active && n.id !== focus && !related?.has(n.id) && !nodeColors.has(n.id))
        .classed('is-focused', n => n.id === focus).classed('is-selected', n => n.id === String(latest.current.selected?.id ?? ''));
      rings.attr('r', n => n.id === String(latest.current.selected?.id ?? '') ? radius(n) + 4 : 0);
      link.classed('is-neighbor', e => (e.source as Node).id === focus || (e.target as Node).id === focus)
        .classed('is-muted', e => active && (e.source as Node).id !== focus && (e.target as Node).id !== focus && !highlightedEdgeColor(pathPairs, e.source, e.target, p.directed) && !highlightedEdgeColor(subPairs, e.source, e.target, p.directed))
        .style('stroke-width', e => `${latest.current.edgeWidth * (((e.source as Node).id === focus || (e.target as Node).id === focus) ? 2 : highlightedEdgeColor(pathPairs, e.source, e.target, p.directed) ? 2.8 : highlightedEdgeColor(subPairs, e.source, e.target, p.directed) ? 2.2 : 1)}px`);
      paintLabels();
    }
    function scheduleFrame() {
      if (disposed) return;
      if (!frame) frame = requestAnimationFrame(() => {
        frame = 0;
        const transform = state.camera.toString();
        if (viewport.attr('transform') !== transform) viewport.attr('transform', transform);
        if (needsPaint) { needsPaint = false; paint(); }
        // Recompute labels while moving, at most 30 times per second. Camera
        // movement stays at the display frame rate; zoom never repaints highlights.
        else if (zooming && performance.now() - lastLabels >= 32) paintLabels();
      });
    }
    function schedulePaint() { needsPaint = true; scheduleFrame(); }
    const zoom = d3.zoom<SVGSVGElement, unknown>().extent((): [[number, number], [number, number]] => [[0, 0], [width, height]]).scaleExtent([.15, 8])
      .on('start', () => {
        zooming = true; svg.classed('is-zooming', true);
        // Moving nodes beneath the pointer must not repeatedly dim the entire graph.
        if (hovered) { hovered = ''; schedulePaint(); }
      })
      .on('zoom', event => {
        if (event.sourceEvent) interacted = true;
        state.camera = event.transform;
        scheduleFrame();
      })
      .on('end', () => {
        zooming = false; svg.classed('is-zooming', false); schedulePaint();
      });
    svg.call(zoom).call(zoom.transform, state.camera).on('dblclick.zoom', null).on('click.clear', event => { if (event.target === ref.current) { hovered = ''; latest.current.onSelect(null); schedulePaint(); } });
    // Restore the viewport in the same render as the nodes, without an identity-camera frame.
    viewport.attr('transform', state.camera.toString());
    const simulation = d3.forceSimulation(nodes).alphaDecay(.045).velocityDecay(.45)
      .force('link', d3.forceLink<Node, any>(edges).id(n => n.id).distance(p.schema ? 115 : 65))
      .force('charge', d3.forceManyBody().strength(p.schema ? -300 : -115).distanceMax(550))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide<Node>(n => (p.schema ? 35 : Math.max(8, degreeRadius(n.degree, maxDegree)) + 5)));
    if (p.layout === 'radial') simulation.force('radial', d3.forceRadial(Math.min(width, height) * .3, width / 2, height / 2).strength(.45));
    function renderFrame() {
      link.attr('d', e => {
        const a = e.source as Node, b = e.target as Node;
        const ax = a.x || 0, ay = a.y || 0, bx = b.x || 0, by = b.y || 0;
        if (a.id === b.id) { const r = radius(a), loop = r + 20 + e.lane * 8; return `M${ax - r * .7},${ay - r * .7}C${ax - loop},${ay - loop * 2} ${ax + loop},${ay - loop * 2} ${ax + r * .7},${ay - r * .7}`; }
        const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1;
        const offset = e.lane * 24 * (a.id < b.id ? 1 : -1);
        const cx = (ax + bx) / 2 - dy / len * offset, cy = (ay + by) / 2 + dx / len * offset;
        const al = Math.hypot(cx - ax, cy - ay) || 1, bl = Math.hypot(cx - bx, cy - by) || 1;
        const ar = Math.min(radius(a) + 1, len / 3), br = Math.min(radius(b) + 2, len / 3);
        return `M${ax + (cx - ax) / al * ar},${ay + (cy - ay) / al * ar}Q${cx},${cy} ${bx + (cx - bx) / bl * br},${by + (cy - by) / bl * br}`;
      });
      relationshipLabels?.attr('x', e => {
        const a = e.source as Node, b = e.target as Node;
        if (a.id === b.id) return a.x || 0;
        const dx = (b.x || 0) - (a.x || 0), dy = (b.y || 0) - (a.y || 0), len = Math.hypot(dx, dy) || 1;
        return ((a.x || 0) + (b.x || 0)) / 2 - dy / len * e.lane * 12 * (a.id < b.id ? 1 : -1);
      }).attr('y', e => {
        const a = e.source as Node, b = e.target as Node;
        if (a.id === b.id) return (a.y || 0) - (radius(a) + 20 + e.lane * 8) * 1.5 - 5;
        const dx = (b.x || 0) - (a.x || 0), dy = (b.y || 0) - (a.y || 0), len = Math.hypot(dx, dy) || 1;
        return ((a.y || 0) + (b.y || 0)) / 2 + dx / len * e.lane * 12 * (a.id < b.id ? 1 : -1) - 5;
      });
      node.attr('transform', n => `translate(${n.x},${n.y})`);
      hulls.forEach(({ h, path }) => path.attr('d', paddedHullPath([...highlightNodeIds([h])].map(id => byId.get(id)).filter((n): n is Node => Boolean(n)).map(n => [n.x || 0, n.y || 0]), 20)));
      if (!zooming && performance.now() - lastLabels > 100) paintLabels();
    }
    function resizeNodes() {
      circles.attr('r', radius); halos.attr('r', n => nodeColors.has(n.id) ? radius(n) + 5 : 0);
      // Reserve enough space for either size mode without restarting physics.
      renderFrame(); paint();
    }
    function fit(manual = true) {
      if (!nodes.length) return;
      if (manual) interacted = true;
      let x0 = Math.min(...nodes.map(n => (n.x || 0) - radius(n))) - 30, x1 = Math.max(...nodes.map(n => (n.x || 0) + radius(n))) + 30;
      let y0 = Math.min(...nodes.map(n => (n.y || 0) - radius(n))) - 30, y1 = Math.max(...nodes.map(n => (n.y || 0) + radius(n))) + 40;
      // Schema loops and relationship labels extend beyond the node circles.
      // Measure the rendered content once on fit, never in the animation loop.
      if (p.schema) {
        const bounds = viewport.node()?.getBBox?.();
        if (bounds && Number.isFinite(bounds.width) && bounds.width > 0 && Number.isFinite(bounds.height) && bounds.height > 0) {
          x0 = Math.min(x0, bounds.x - 20); x1 = Math.max(x1, bounds.x + bounds.width + 20);
          y0 = Math.min(y0, bounds.y - 20); y1 = Math.max(y1, bounds.y + bounds.height + 20);
        }
      }
      const k = Math.max(.15, Math.min(2, width / Math.max(1, x1 - x0), height / Math.max(1, y1 - y0)));
      svg.call(zoom.transform, d3.zoomIdentity.translate(width / 2 - k * (x0 + x1) / 2, height / 2 - k * (y0 + y1) / 2).scale(k));
    }
    function focus() {
      const focusId = latest.current.focusId;
      if (!focusId) { state.appliedFocusId = ''; return; }
      if (state.appliedFocusId === focusId) return;
      const n = byId.get(focusId); if (!n) return;
      const k = Math.max(.8, state.camera.k);
      svg.call(zoom.transform, d3.zoomIdentity.translate(width / 2 - (n.x || 0) * k, height / 2 - (n.y || 0) * k).scale(k));
      interacted = true;
      state.appliedFocusId = focusId;
    }
    node.on('pointerenter', (_, n) => { if (!dragging && !zooming) { hovered = n.id; schedulePaint(); } })
      .on('pointerleave', () => { if (!dragging && !zooming) { hovered = ''; schedulePaint(); } })
      .on('focus', (_, n) => { hovered = n.id; schedulePaint(); }).on('blur', () => { hovered = ''; schedulePaint(); })
      .on('click', (event, n) => { event.stopPropagation(); latest.current.onSelect(n); schedulePaint(); })
      .on('keydown', (event, n) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); latest.current.onSelect(n); } if (event.key === 'Escape') { hovered = ''; latest.current.onSelect(null); } schedulePaint(); })
      .call(d3.drag<SVGGElement, Node>().on('start', (event, n) => { interacted = true; dragging = true; hovered = n.id; n.fx = n.x; n.fy = n.y; })
        .on('drag', (event, n) => { if (p.layout !== 'circle') simulation.alphaTarget(.15).restart(); n.fx = n.x = event.x; n.fy = n.y = event.y; renderFrame(); })
        .on('end', (_, n) => { dragging = false; hovered = ''; simulation.alphaTarget(0); n.fx = null; n.fy = null; schedulePaint(); }));
    if (p.layout === 'circle') {
      if (!canResume) nodes.forEach((n, i) => { const angle = i * 2 * Math.PI / Math.max(nodes.length, 1), r = Math.min(width, height) * .34; n.x = width / 2 + Math.cos(angle) * r; n.y = height / 2 + Math.sin(angle) * r; });
      simulation.stop();
    }
    const fresh = !nodes.some(node => state.positions.has(node.id));
    state.topologySignature = topologySignature;
    state.layout = p.layout;
    if (canResume) {
      simulation.stop();
      renderFrame();
      resizeNodes();
    } else {
      simulation.on('tick', renderFrame).on('end', () => { if (!interacted && fresh) fit(false); paint(); });
      if (p.layout !== 'circle') simulation.tick();
      resizeNodes();
      if (fresh) fit(false);
    }
    focus();
    controller.current = { paint: schedulePaint, fit, focus, resizeNodes };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      width = host.clientWidth || width; height = host.clientHeight || height;
      // Resizing changes only the viewport. It does not reset coordinates or restart physics.
      schedulePaint();
    }); observer?.observe(host);
    return () => { disposed = true; simulation.stop(); observer?.disconnect(); cancelAnimationFrame(frame); svg.on('.zoom', null).on('.clear', null); controller.current = undefined;
      state.positions = new Map(nodes.map(n => [n.id, { x: n.x || 0, y: n.y || 0 }])); };
  }, [signature, topologySignature, props.layout, props.active, props.state, marker, t]);
  useEffect(() => { controller.current?.paint(); }, [props.selected, props.labels, props.edgeWidth, props.preview.matches]);
  useEffect(() => { controller.current?.resizeNodes(); }, [props.sizeMode]);
  useEffect(() => {
    if (!controller.current || !props.fitVersion || state.appliedFitVersion === props.fitVersion) return;
    state.appliedFitVersion = props.fitVersion;
    controller.current.fit();
  }, [props.fitVersion, props.active, signature, props.layout, state]);
  useEffect(() => { controller.current?.focus(); }, [props.focusId]);
  return <svg ref={ref} aria-label={props.schema
    ? t('Graph Schema canvas. Drag to pan and use the mouse wheel to zoom.', 'Graph Schema 画布，可拖动平移并使用滚轮缩放')
    : t('Graph canvas. Drag to pan and use the mouse wheel to zoom.', '图谱画布，可拖动平移并使用滚轮缩放')} />;
}
