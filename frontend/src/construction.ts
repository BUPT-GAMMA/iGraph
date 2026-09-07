import type { Dataset } from './types';

export function supportsConstruction(dataset: Dataset) {
  const extension = (dataset.file_type || dataset.original_filename.split('.').pop() || '').toLowerCase().replace(/^\./, '');
  return ['sql', 'csv', 'txt'].includes(extension) || Boolean(dataset.metadata?.construction);
}

export function constructionLabel(dataset: Dataset) {
  const construction = dataset.metadata?.construction as { revision?: number; status?: string } | undefined;
  return construction?.status === 'ready' || Number(construction?.revision) > 0 ? '查看构图' : '构图';
}
