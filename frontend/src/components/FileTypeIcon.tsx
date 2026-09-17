import { Icon } from './Icon';

export function FileTypeIcon({ filename }: { filename: string }) {
  const extension = filename.split('.').pop()?.toLowerCase() || '';
  const kind = ['csv', 'tsv', 'xls', 'xlsx', 'sql'].includes(extension) ? 'table'
    : ['py', 'js', 'json', 'yaml', 'yml'].includes(extension) ? 'code'
    : ['pt', 'npz', 'edgelist'].includes(extension) ? 'graph' : 'file';
  return <span className={`attachment-file-icon file-kind-${kind}`} aria-hidden="true">
    <Icon name={kind === 'table' ? 'table' : kind === 'graph' ? 'graph' : 'file'} size={17} />
    <small>{extension.slice(0, 4).toUpperCase()}</small>
  </span>;
}
