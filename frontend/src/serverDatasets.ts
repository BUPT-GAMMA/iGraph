import type { ServerDataset } from './types';

export function serverDatasetReady(dataset: ServerDataset) {
  const status = dataset.preparation?.status;
  return !status || status === 'not_required' || status === 'ready';
}

export function serverDatasetPreparationLabel(dataset: ServerDataset) {
  const preparation = dataset.preparation;
  if (!preparation) return '';
  const status = preparation.status;
  if (status === 'not_required') return '会话内直接加载';
  if (status === 'ready') {
    const nodes = Number(preparation.num_nodes);
    const edges = Number(preparation.num_edges);
    if (Number.isFinite(nodes) && Number.isFinite(edges)) {
      return `已预处理 · ${nodes.toLocaleString()} nodes · ${edges.toLocaleString()} edges`;
    }
    return '已完成服务器预处理';
  }
  if (status === 'running') {
    const progress = Number(preparation.progress);
    const percentage = Number.isFinite(progress)
      ? ` ${Math.round(Math.max(0, Math.min(1, progress)) * 100)}%`
      : '';
    return `服务器预处理中${percentage}`;
  }
  if (status === 'queued' || status === 'not_prepared') return '服务器预处理已排队';
  if (status === 'unavailable') return '预处理服务暂不可用';
  if (status === 'cancelled') return '服务器预处理已取消';
  if (status === 'failed') return '服务器预处理失败';
  return preparation.message || '服务器预处理中';
}

export function serverDatasetActionLabel(dataset: ServerDataset, fallback: string) {
  const status = dataset.preparation?.status;
  if (status === 'queued' || status === 'running' || status === 'not_prepared') return '预处理中';
  if (status === 'failed' || status === 'cancelled') return '预处理失败';
  if (status === 'unavailable') return '暂不可用';
  return fallback;
}
