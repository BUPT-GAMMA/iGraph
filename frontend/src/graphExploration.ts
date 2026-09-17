import type { Highlight } from './types';
import type { Language } from './i18n';

/** Tool results may identify a subgraph by nodes, edges, or both. */
export function highlightNodeIds(highlights: Highlight[]) {
  const ids = new Set<string>();
  const add = (value: unknown) => {
    if (value == null) return;
    const id = typeof value === 'object' && 'id' in value ? (value as { id: unknown }).id : value;
    if (id != null) ids.add(String(id));
  };
  highlights.forEach(highlight => {
    (highlight.nodes || []).forEach(add);
    (highlight.edges || []).forEach(edge => {
      add(Array.isArray(edge) ? edge[0] : edge.src ?? edge.source);
      add(Array.isArray(edge) ? edge[1] : edge.dst ?? edge.target);
    });
  });
  return ids;
}

export function graphQuestionPrompt(question: string, language: Language) {
  return language === 'zh'
    ? `请根据当前活动数据集的完整实例图回答以下问题，并将相关节点与子图显示在图画布：\n${question.trim()}\n\n请先使用 search_node/search_edge 等工具定位真实节点，根据问题选择图分析工具。不要仅从前端抽样预览推断答案，也不要修改数据集。节点有歧义时请在对话中列出候选供我确认；无匹配或执行失败时明确说明。定位成功后必须调用 visualize_subgraph，传入相关节点的精确 node_names 以及 highlights.nodes 或 highlights.subgraphs；若是路径问题，使用 highlights.paths 的 node_sequence 标注真实路径，并说明结果范围和限制。`
    : `Answer this question using the complete instance graph of the active dataset, and show the relevant nodes and subgraph on the graph canvas:\n${question.trim()}\n\nUse search_node/search_edge to resolve real nodes, then the appropriate graph analysis tools. Do not infer the answer only from the sampled canvas or modify the dataset. If a name is ambiguous, list candidates in the conversation for confirmation; report no matches or failures explicitly. On success, call visualize_subgraph with exact node_names and highlights.nodes or highlights.subgraphs; for a path use highlights.paths with its real node_sequence. Explain the result scope and any limits.`;
}

export function shortestPathPrompt(source: string, target: string, weighted: boolean, language: Language) {
  const endpoints = JSON.stringify({ source: source.trim(), target: target.trim(), use_weight: weighted });
  return language === 'zh'
    ? `请在当前活动数据集的完整实例图上计算并可视化最短路径。输入：${endpoints}。\n请先确认起终点的精确节点 ID，名称或描述不明确时使用 search_node；若存在歧义，先列出候选让我确认。然后调用 shortest_path，保留图的方向，use_weight=${weighted}（${weighted ? '按边权重' : '按跳数'}）。成功找到路径后必须调用 visualize_subgraph，以路径的完整节点序列为 node_names，并传入 highlights.paths=[{node_sequence: 路径节点序列, label: "最短路径"}]，在画布高亮显示。不要仅在前端抽样子图上计算，也不要修改数据集。若无路径、节点不存在、工具不支持或达到搜索上限，明确说明，不能将有界搜索失败当作全图不连通。`
    : `Compute and visualize a shortest path on the complete instance graph of the active dataset. Input: ${endpoints}.\nResolve the exact endpoint IDs, using search_node for names or descriptions; ask me to choose candidates if ambiguous. Call shortest_path with the graph direction preserved and use_weight=${weighted} (${weighted ? 'edge weights' : 'hop count'}). If found, call visualize_subgraph with the full path as node_names and highlights.paths=[{node_sequence: the path, label: "Shortest path"}]. Do not compute only on the sampled canvas or modify the dataset. Explicitly report absent nodes, no path, unsupported tools, and search limits; a bounded-search failure does not prove full-graph disconnection.`;
}
