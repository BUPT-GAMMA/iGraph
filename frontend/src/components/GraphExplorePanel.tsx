import { useState } from 'react';
import { useI18n } from '../i18n';
import { graphQuestionPrompt, shortestPathPrompt } from '../graphExploration';
import './GraphExplorePanel.css';

type Props = {
  schema: boolean;
  selectedId: string;
  pending: boolean;
  onQuery: (prompt: string) => void;
};

export function GraphExplorePanel({ schema, selectedId, pending, onQuery }: Props) {
  const { language, t } = useI18n();
  const [mode, setMode] = useState<'question' | 'path'>('question');
  const [question, setQuestion] = useState('');
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [weighted, setWeighted] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const disabled = pending || schema;
  return <section className="graph-explore" aria-label={t('Graph exploration', '图探索')}>
    <div className="graph-explore-tabs">
      <button type="button" aria-pressed={mode === 'question'} onClick={() => setMode('question')}>{t('Locate by question', '问题定位')}</button>
      <button type="button" aria-pressed={mode === 'path'} onClick={() => setMode('path')}>{t('Shortest path', '最短路径')}</button>
    </div>
    {schema && <p>{t('This view describes node types and fields. Load an instance graph in a conversation to locate records or calculate paths.', '当前视图展示节点类型与字段。请在会话中加载实例图后定位记录或计算路径。')}</p>}
    <form onSubmit={event => {
      event.preventDefault();
      if (disabled || (mode === 'question' ? !question.trim() : !source.trim() || !target.trim())) return;
      onQuery(mode === 'question' ? graphQuestionPrompt(question, language) : shortestPathPrompt(source, target, weighted, language));
      setSubmitted(true);
    }}>
      {mode === 'question' ? <label className="graph-explore-question">{t('Question about the graph', '图谱问题')}<textarea rows={2} value={question} disabled={disabled} onChange={event => { setQuestion(event.target.value); setSubmitted(false); }} placeholder={t('Find the papers related to graph learning and their authors', '例如：定位与图学习相关的论文及作者')} /></label>
        : <><div className="graph-path-endpoints">
          <label>{t('Source node', '起点节点')}<input value={source} disabled={disabled} onChange={event => { setSource(event.target.value); setSubmitted(false); }} placeholder={t('Node ID, name or description', '节点 ID、名称或描述')} />{selectedId && <button type="button" disabled={disabled} onClick={() => setSource(selectedId)}>{t('Use selected node as source', '将选中节点设为起点')}</button>}</label>
          <label>{t('Target node', '终点节点')}<input value={target} disabled={disabled} onChange={event => { setTarget(event.target.value); setSubmitted(false); }} placeholder={t('Node ID, name or description', '节点 ID、名称或描述')} />{selectedId && <button type="button" disabled={disabled} onClick={() => setTarget(selectedId)}>{t('Use selected node as target', '将选中节点设为终点')}</button>}</label>
        </div><label className="graph-path-weight"><input type="checkbox" checked={weighted} disabled={disabled} onChange={event => setWeighted(event.target.checked)} />{t('Use edge weights (otherwise fewest hops)', '按边权重计算（默认按最少跳数）')}</label></>}
      <button className="graph-explore-submit" type="submit" disabled={disabled || (mode === 'question' ? !question.trim() : !source.trim() || !target.trim())}>{pending ? t('Processing in conversation…', '正在对话中处理…') : mode === 'question' ? t('Locate and visualize', '定位并显示') : t('Find and highlight path', '查找并高亮路径')}</button>
    </form>
    {submitted && <p role="status">{pending ? t('The graph assistant is working. Follow its progress in the conversation.', '图助手正在处理，可在对话中查看进度。') : t('Request sent to the conversation. Check its answer for results, errors or candidate confirmation.', '请求已发送到对话，请查看回答中的结果、错误或候选确认。')}</p>}
  </section>;
}
