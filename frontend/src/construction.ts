import type { Dataset } from './types';

type ConstructionMetadata = { revision?: number; status?: string };

function constructionMetadata(dataset: Dataset) {
  const value = dataset.metadata?.construction;
  return value && typeof value === 'object' ? value as ConstructionMetadata : undefined;
}

export function constructionStatus(dataset: Dataset) {
  const status = constructionMetadata(dataset)?.status;
  return typeof status === 'string' && status ? status : dataset.status;
}

export function supportsConstruction(dataset: Dataset) {
  const extension = (dataset.file_type || dataset.original_filename.split('.').pop() || '').toLowerCase().replace(/^\./, '');
  return ['sql', 'csv', 'txt'].includes(extension) || Boolean(dataset.metadata?.construction);
}

export function constructionLabel(dataset: Dataset) {
  const construction = constructionMetadata(dataset);
  const status = constructionStatus(dataset);
  if (status === 'building' || status === 'processing') return '查看进度';
  if (status === 'failed' || status === 'error') return '重新构图';
  if (status === 'algorithm_unavailable') return '算法待接入';
  return status === 'ready' || (!construction?.status && Number(construction?.revision) > 0) ? '查看构图' : '构图';
}
