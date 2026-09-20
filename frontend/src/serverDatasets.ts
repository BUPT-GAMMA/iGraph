import type { ServerDataset } from './types';
import { tr, type Translate } from './i18n';

export function serverDatasetReady(dataset: ServerDataset) {
  const status = dataset.preparation?.status;
  return !status || status === 'not_required' || status === 'ready';
}

export function serverDatasetPreparationLabel(dataset: ServerDataset, t: Translate = tr) {
  const preparation = dataset.preparation;
  if (!preparation) return '';
  const status = preparation.status;
  if (status === 'not_required') return t('Loaded directly in the session', '会话内直接加载');
  if (status === 'ready') {
    const nodes = Number(preparation.num_nodes);
    const edges = Number(preparation.num_edges);
    if (Number.isFinite(nodes) && Number.isFinite(edges)) {
      return t(`Prepared · ${nodes.toLocaleString('en-US')} nodes · ${edges.toLocaleString('en-US')} edges`, `已预处理 · ${nodes.toLocaleString('zh-CN')} 个节点 · ${edges.toLocaleString('zh-CN')} 条边`);
    }
    return t('Server preparation complete', '已完成服务器预处理');
  }
  if (status === 'running') {
    const progress = Number(preparation.progress);
    const percentage = Number.isFinite(progress)
      ? ` ${Math.round(Math.max(0, Math.min(1, progress)) * 100)}%`
      : '';
    return t(`Preparing on server${percentage}`, `服务器预处理中${percentage}`);
  }
  if (status === 'queued' || status === 'not_prepared') return t('Queued for server preparation', '服务器预处理已排队');
  if (status === 'unavailable') return t('Preparation service unavailable', '预处理服务暂不可用');
  if (status === 'cancelled') return t('Server preparation cancelled', '服务器预处理已取消');
  if (status === 'failed') return t('Server preparation failed', '服务器预处理失败');
  return t('Preparation status unknown', '预处理状态未知');
}

export function serverDatasetActionLabel(dataset: ServerDataset, fallback: string, t: Translate = tr) {
  const status = dataset.preparation?.status;
  if (status === 'queued' || status === 'running' || status === 'not_prepared') return t('Preparing', '预处理中');
  if (status === 'cancelled') return t('Cancelled', '已取消');
  if (status === 'failed') return t('Preparation failed', '预处理失败');
  if (status === 'unavailable') return t('Unavailable', '暂不可用');
  return fallback;
}
