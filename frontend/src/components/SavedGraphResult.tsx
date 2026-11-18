import { useEffect, useState } from 'react';
import { ApiError, getSavedGraphResult } from '../api';
import { useI18n } from '../i18n';
import type { Dataset } from '../types';
import { GraphArchitectureLayer } from './GraphArchitectureLayer';
import { GraphWorkspace } from './GraphWorkspace';

type Result = Awaited<ReturnType<typeof getSavedGraphResult>>;

export function SavedGraphResult({ dataset, layer }: { dataset: Dataset; layer: 'schema' | 'instances' }) {
  const { language, t } = useI18n();
  const [result, setResult] = useState<Result>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [tab, setTab] = useState<'Graph' | 'Overview' | 'Data' | 'ML' | 'Artifacts'>('Graph');
  const { dataset_id, project_id } = dataset;

  useEffect(() => {
    const controller = new AbortController();
    setResult(undefined);
    setError('');
    getSavedGraphResult({ dataset_id, project_id }, controller.signal).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof ApiError && cause.code || 'unknown');
    });
    return () => controller.abort();
  }, [dataset_id, project_id, attempt]);

  if (error) return <div className="construction-empty" role="alert">
    <strong>{error === 'saved_graph_too_large' ? t('This graph exceeds the interactive preview limit.', '这张图超过了交互预览的大小限制。')
      : error === 'saved_graph_missing' ? t('The saved graph file is missing.', '找不到已保存的图文件。')
      : error === 'saved_graph_unreadable' ? t('This file cannot be previewed as a single graph.', '此文件的存储格式暂不支持单图预览。')
      : t('Could not load the saved graph.', '暂时无法读取已保存的图。')}</strong>
    <button className="secondary-command" onClick={() => setAttempt(value => value + 1)}>{t('Retry', '重试')}</button>
  </div>;
  if (!result) return <div className="construction-empty" role="status"><span className="loader" />{t('Reading saved graph…', '正在读取已保存的图…')}</div>;
  const { schema, graph } = result;
  const number = (n: number) => n.toLocaleString(language);
  const nodes = number(schema.node_count ?? graph.nodes.length), edges = number(schema.edge_count ?? graph.edges.length);
  return <>
    <div className="saved-graph-counts">{t(`Full graph · ${nodes} nodes · ${edges} edges`, `全图 · ${nodes} 个节点 · ${edges} 条边`)}
      {layer === 'instances' && graph.truncated && <span>{t(`Sample · ${number(graph.nodes.length)} nodes · ${number(graph.edges.length)} edges`,
        `实例样本 · ${number(graph.nodes.length)} 个节点 · ${number(graph.edges.length)} 条边`)}</span>}
    </div>
    {layer === 'schema' ? <GraphArchitectureLayer data={schema.graph} highlights={[]} />
      : <div className="construction-graph saved-graph-instances"><GraphWorkspace embedded data={graph} tab={tab} onTab={setTab}
          selected={selected} onSelect={setSelected} collapsed={false} onToggle={() => undefined} /></div>}
  </>;
}
