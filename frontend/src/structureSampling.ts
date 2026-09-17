/** Budgeted structural sampling: components get proportional space and new
 * representatives bring their real connecting paths, never synthetic edges. */
export type StructuralNode = { id: string; degree: number; type: string; community?: string; sourceDegree?: number };
type Neighbor = { node: string };

class Heap<T> {
  values: T[] = [];
  constructor(private before: (a: T, b: T) => boolean) {}
  push(value: T) {
    const a = this.values; a.push(value); let i = a.length - 1;
    while (i > 0) { const p = (i - 1) >> 1; if (!this.before(a[i], a[p])) break; [a[i], a[p]] = [a[p], a[i]]; i = p; }
  }
  pop(): T | undefined {
    const a = this.values, first = a[0], last = a.pop();
    if (a.length && last !== undefined) {
      a[0] = last; let i = 0;
      while (true) { let child = i * 2 + 1; if (child >= a.length) break;
        if (child + 1 < a.length && this.before(a[child + 1], a[child])) child++;
        if (!this.before(a[child], a[i])) break; [a[child], a[i]] = [a[i], a[child]]; i = child;
      }
    }
    return first;
  }
  peek() { return this.values[0]; }
}

export function sampleStructure<T extends StructuralNode>(entries: T[], adjacency: Map<string, Neighbor[]>, limit: number, required: Set<string>, compare: (a: T, b: T) => number): Set<string> {
  const ranked = [...entries].sort(compare), byId = new Map(entries.map(n => [n.id, n]));
  const rank = new Map(ranked.map((n, i) => [n.id, i]));
  const seen = new Set<string>();
  const components: T[][] = [];
  for (const node of ranked) {
    if (seen.has(node.id)) continue;
    const queue = [node.id]; seen.add(node.id);
    for (let i = 0; i < queue.length; i++) for (const next of adjacency.get(queue[i]) || []) {
      if (byId.has(next.node) && !seen.has(next.node)) { seen.add(next.node); queue.push(next.node); }
    }
    components.push(queue.map(id => byId.get(id)!).sort(compare));
  }
  // Isolates share a budget bucket; allocating one slot per component in ID
  // order otherwise strongly biases types when thousands of isolates exist.
  const isolates = components.filter(group => group.length === 1 && group[0].degree === 0 && !(group[0].sourceDegree || 0)).flat();
  if (isolates.length) {
    const isolateIds = new Set(isolates.map(n => n.id));
    const connected = components.filter(group => !isolateIds.has(group[0].id));
    components.splice(0, components.length, ...connected, isolates.sort(compare));
  }
  const quotas = components.map(group => group.filter(n => required.has(n.id)).length);
  let remaining = limit - quotas.reduce((a, b) => a + b, 0);
  const weights = components.map(group => group.length === 1 && group[0].degree === 0 && (group[0].sourceDegree || 0) > 0 ? 0 : group.length);
  const totalWeight = weights.reduce((a, b) => a + b, 0);
  const exact = components.map((group, i) => (totalWeight ? weights[i] * limit / totalWeight : group.length * limit / entries.length));
  const cost = (i: number) => quotas[i] === 0 && components[i].length > 1 && components[i][0].degree > 0 ? 2 : 1;
  const score = (i: number) => (exact[i] - quotas[i]) / cost(i);
  const heap = new Heap<number>((a, b) => score(a) > score(b) || (score(a) === score(b) && (components[a].length > components[b].length || (components[a].length === components[b].length && a < b))));
  components.forEach((group, i) => { if (quotas[i] < group.length && (weights[i] > 0 || !totalWeight)) heap.push(i); });
  while (remaining > 0 && heap.peek() !== undefined) {
    const i = heap.pop()!; const increment = cost(i);
    if (increment > remaining) continue;
    quotas[i] += increment; remaining -= increment;
    if (quotas[i] < components[i].length) heap.push(i);
  }
  // A single remaining slot cannot represent an unseen connected component.
  // Only use it when the whole display budget is one, or no pair can fit.
  if (remaining > 0) for (let i = 0; i < components.length && remaining; i++) {
    if (quotas[i] < components[i].length && (weights[i] > 0 || !totalWeight)) { quotas[i]++; remaining--; }
  }
  const selected = new Set<string>();
  components.forEach((group, componentIndex) => {
    const quota = quotas[componentIndex]; if (!quota) return;
    if (quota >= group.length) { group.forEach(n => selected.add(n.id)); return; }
    const local = new Set(group.filter(n => required.has(n.id)).map(n => n.id));
    if (group.every(n => n.degree === 0)) {
      const types = new Map<string, T[]>(), counts = new Map<string, number>();
      group.forEach(n => { const items = types.get(n.type) || []; items.push(n); types.set(n.type, items); if (local.has(n.id)) counts.set(n.type, (counts.get(n.type) || 0) + 1); });
      const cursors = new Map<string, number>();
      while (local.size < quota) {
        let best: T | undefined, bestDeficit = -Infinity;
        for (const [type, items] of types) {
          let cursor = cursors.get(type) || 0;
          while (cursor < items.length && local.has(items[cursor].id)) cursor++;
          cursors.set(type, cursor); if (cursor >= items.length) continue;
          const deficit = items.length * quota / group.length - (counts.get(type) || 0);
          if (!best || deficit > bestDeficit || (deficit === bestDeficit && compare(items[cursor], best) < 0)) { best = items[cursor]; bestDeficit = deficit; }
        }
        if (!best) break;
        local.add(best.id); counts.set(best.type, (counts.get(best.type) || 0) + 1);
      }
      local.forEach(id => selected.add(id)); return;
    }
    const anchor = group.find(n => local.has(n.id)) || group[0]; local.add(anchor.id);
    const parents = new Map<string, string | null>([[anchor.id, null]]), queue = [anchor.id];
    for (let i = 0; i < queue.length; i++) for (const next of adjacency.get(queue[i]) || []) {
      if (!byId.has(next.node) || parents.has(next.node)) continue;
      parents.set(next.node, queue[i]); queue.push(next.node);
    }
    const groups = new Map<string, T[]>();
    const keys = (n: T) => [`t:${n.type}`, ...(n.community === undefined ? [] : [`c:${n.community}`])];
    group.forEach(n => keys(n).forEach(key => { const items = groups.get(key) || []; items.push(n); groups.set(key, items); }));
    const representatives = [...groups.values()].sort((a, b) => b.length - a.length || compare(a[0], b[0])).map(items => items[0]);
    const targets = [...group.filter(n => required.has(n.id)), ...representatives.slice(0, 64)];
    for (const target of targets) {
      if (target.id === anchor.id) continue;
      const path: string[] = []; let cursor: string | null = target.id;
      while (cursor !== null) { path.push(cursor); cursor = parents.get(cursor) ?? null; }
      const missing = path.filter(id => !local.has(id));
      const pathBudget = required.has(target.id) || quota <= 8 ? quota : Math.max(local.size, Math.floor(quota * .65));
      if (local.size + missing.length <= pathBudget) missing.forEach(id => local.add(id));
    }
    const counts = new Map<string, number>(), frontier = new Map<string, Heap<T>>(), queued = new Set<string>();
    const enqueue = (id: string) => {
      for (const nb of adjacency.get(id) || []) {
        const node = byId.get(nb.node); if (!node || local.has(node.id) || queued.has(node.id)) continue;
        queued.add(node.id);
        const key = JSON.stringify(keys(node)); let bucket = frontier.get(key);
        if (!bucket) { bucket = new Heap<T>((a, b) => rank.get(a.id)! < rank.get(b.id)!); frontier.set(key, bucket); }
        bucket.push(node);
      }
    };
    local.forEach(id => { keys(byId.get(id)!).forEach(key => counts.set(key, (counts.get(key) || 0) + 1)); enqueue(id); });
    const deficit = (n: T) => keys(n).reduce((sum, key) => {
      const target = groups.get(key)!.length * quota / group.length;
      return sum + (target - (counts.get(key) || 0)) / Math.max(1, target);
    }, 0);
    while (local.size < quota) {
      let best: T | undefined, bestScore = -Infinity, bestBucket: Heap<T> | undefined;
      for (const bucket of frontier.values()) {
        while (bucket.peek() && local.has(bucket.peek()!.id)) bucket.pop();
        const n = bucket.peek(); if (!n) continue;
        const value = deficit(n);
        if (!best || value > bestScore || (value === bestScore && compare(n, best) < 0)) { best = n; bestScore = value; bestBucket = bucket; }
      }
      if (!best) break;
      bestBucket!.pop(); local.add(best.id);
      keys(best).forEach(key => counts.set(key, (counts.get(key) || 0) + 1)); enqueue(best.id);
    }
    local.forEach(id => selected.add(id));
  });
  return selected;
}

/** Keep a spanning forest before optional dense/parallel edges. Input order
 * determines priority; explicit highlighted edges still have first claim. */
export function topologyFirstEdges<T>(nodes: Iterable<string>, edges: T[], endpoints: (edge: T) => [string, string], highlighted: (edge: T) => boolean, limit: number): T[] {
  if (edges.length <= limit) return edges;
  const parents = new Map(Array.from(nodes, id => [id, id]));
  const root = (id: string): string => { let p = id; while (parents.get(p) !== p) p = parents.get(p)!; while (id !== p) { const next = parents.get(id)!; parents.set(id, p); id = next; } return p; };
  const priority = edges.filter(highlighted), protectedEdges = new Set(priority);
  for (const edge of priority) { const [a, b] = endpoints(edge); parents.set(root(a), root(b)); }
  for (const edge of edges) { const [a, b] = endpoints(edge), ra = root(a), rb = root(b); if (ra !== rb) { parents.set(ra, rb); priority.push(edge); protectedEdges.add(edge); } }
  return [...priority, ...edges.filter(edge => !protectedEdges.has(edge))].slice(0, limit);
}
