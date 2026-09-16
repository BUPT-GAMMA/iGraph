import { Fragment, type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ArtifactRef, Message, Plan, ToolCall } from '../types';
import { ConversationInputArea, type ConversationInputAreaProps } from './ConversationInputArea';
import { editableLatestTurn } from '../latestTurn';
import { answerVersions } from '../answerVersions';
import { FileTypeIcon } from './FileTypeIcon';
import { BrandLogo, Icon } from './Icon';
import { ProcessTimeline } from './ProcessTimeline';
import { fetchArtifact } from './WorkspaceFiles';
import { useI18n, type Translate } from '../i18n';

function formatBytes(size?: number) {
  if (!Number.isFinite(size) || (size as number) < 0) return '';
  const bytes = size as number;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Deliverables the agent published this turn, downloadable inline. */
function ArtifactList({ artifacts, sessionId }: { artifacts: ArtifactRef[]; sessionId?: string }) {
  const { t } = useI18n();
  const [downloading, setDownloading] = useState('');
  const [error, setError] = useState('');
  async function download(artifact: ArtifactRef) {
    if (!sessionId) return;
    setDownloading(artifact.path); setError('');
    try {
      const blob = await fetchArtifact(sessionId, artifact.path);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = artifact.filename; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('File download failed', '文件下载失败'));
    } finally { setDownloading(''); }
  }
  return <div className="message-artifacts">
    {artifacts.map(artifact => {
      const size = formatBytes(artifact.size);
      return <button type="button" className="artifact-chip" key={artifact.path}
        disabled={!sessionId || Boolean(downloading)}
        title={t(`Download ${artifact.filename}`, `下载 ${artifact.filename}`)} aria-label={t(`Download ${artifact.filename}`, `下载 ${artifact.filename}`)}
        onClick={() => void download(artifact)}>
        <FileTypeIcon filename={artifact.filename} />
        <span className="artifact-name">{artifact.filename}</span>
        {size ? <em>{size}</em> : null}
        <span className="artifact-action">{downloading === artifact.path ? t('Downloading…', '下载中…') : t('Download', '下载')}</span>
      </button>;
    })}
    {error ? <p role="alert" className="artifact-error">{error}</p> : null}
  </div>;
}

function planCard(plan: Plan | null | undefined, t: Translate) {
  if (!plan?.steps?.length) return null;
  const done = plan.steps.filter(step => step.status === 'done' || step.status === 'completed').length;
  return <div className="plan-card"><div className="card-heading"><strong>{t('Analysis plan', '分析计划')}</strong><span>{t(`${done} / ${plan.steps.length} complete`, `${done} / ${plan.steps.length} 已完成`)}</span></div>{plan.steps.map((step, index) => <div className="plan-step" key={`${step.step_id}-${index}`}><span className={step.status === 'running' ? 'step-dot running' : 'step-dot'}>{step.status === 'done' || step.status === 'completed' ? <Icon name="check" size={12} /> : index + 1}</span><span>{step.goal || step.result_summary || t('Analysis step', '分析步骤')}</span></div>)}</div>;
}

function toolList(tools: ToolCall[] | undefined, t: Translate) {
  if (!tools?.length) return null;
  return <div className="tool-list">{tools.map((tool, index) => {
    const progress = tool.progress || {};
    const epoch = Number(progress.epoch);
    const epochs = Number(progress.epochs);
    const hasEpoch = Number.isFinite(epoch) && Number.isFinite(epochs) && epochs > 0;
    const progressLabel = hasEpoch ? t(`Epoch ${epoch}/${epochs}`, `轮次 ${epoch}/${epochs}`) : progress.message;
    const statusLabel = tool.done ? t('Complete', '已完成') : progressLabel ? String(progressLabel) : t('Running', '运行中');
    return <div className={`tool-row ${tool.done ? 'done' : 'running'}`} key={`${tool.tool}-${index}`} role="status" aria-label={`${tool.tool || 'graph_tool'}${t(': ', '：')}${statusLabel}`} title={`${tool.tool || 'graph_tool'} · ${statusLabel}`}>
      <span className={tool.done ? 'tool-state done' : 'tool-state'}>{tool.done ? <Icon name="check" size={12} /> : <span className="loader" />}</span>
      <span>{tool.tool || 'graph_tool'}</span>
      {progressLabel ? <span className="tool-progress-label">{String(progressLabel)}</span> : null}
    </div>;
  })}</div>;
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const token = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|\[([^\]]+)\]\(([^)\s]+)\))/g;
  const parts: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  let index = 0;
  while ((match = token.exec(text))) {
    if (match.index > cursor) parts.push(text.slice(cursor, match.index));
    const value = match[0];
    if (value.startsWith('`')) parts.push(<code key={`${keyPrefix}-code-${index}`}>{value.slice(1, -1)}</code>);
    else if (value.startsWith('**') || value.startsWith('__')) parts.push(<strong key={`${keyPrefix}-strong-${index}`}>{value.slice(2, -2)}</strong>);
    else if (value.startsWith('*') || value.startsWith('_')) parts.push(<em key={`${keyPrefix}-em-${index}`}>{value.slice(1, -1)}</em>);
    else if (match[2] && match[3] && /^https?:\/\//i.test(match[3])) parts.push(<a key={`${keyPrefix}-link-${index}`} href={match[3]} target="_blank" rel="noreferrer">{match[2]}</a>);
    else parts.push(value);
    cursor = match.index + value.length;
    index += 1;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

function splitTableRow(line: string) {
  const normalized = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  return normalized.split('|').map(cell => cell.trim());
}

function isTableSeparator(line: string) {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}

function MarkdownContent({ content }: { content: string }) {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const linesInParagraph = paragraph;
    blocks.push(<p key={`p-${blocks.length}`}>{linesInParagraph.map((line, index) => <Fragment key={index}>{renderInline(line, `p-${blocks.length}-${index}`)}{index < linesInParagraph.length - 1 ? <br /> : null}</Fragment>)}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const current = list;
    const ListTag = current.ordered ? 'ol' : 'ul';
    blocks.push(<ListTag key={`list-${blocks.length}`}>{current.items.map((item, index) => <li key={index}>{renderInline(item, `list-${blocks.length}-${index}`)}</li>)}</ListTag>);
    list = null;
  };
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) { flushParagraph(); flushList(); index += 1; continue; }
    if (trimmed.startsWith('```')) {
      flushParagraph(); flushList();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) { codeLines.push(lines[index]); index += 1; }
      if (index < lines.length) index += 1;
      blocks.push(<pre key={`pre-${blocks.length}`}><code>{codeLines.join('\n')}</code></pre>);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushParagraph(); flushList();
      const Heading = `h${heading[1].length}` as keyof JSX.IntrinsicElements;
      blocks.push(<Heading key={`heading-${blocks.length}`}>{renderInline(heading[2], `heading-${blocks.length}`)}</Heading>);
      index += 1;
      continue;
    }
    if (/^(\*{3,}|-{3,}|_{3,})\s*$/.test(trimmed)) {
      flushParagraph(); flushList(); blocks.push(<hr key={`hr-${blocks.length}`} />); index += 1; continue;
    }
    if (index + 1 < lines.length && line.includes('|') && isTableSeparator(lines[index + 1])) {
      flushParagraph(); flushList();
      const headers = splitTableRow(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) { rows.push(splitTableRow(lines[index])); index += 1; }
      blocks.push(<table key={`table-${blocks.length}`}><thead><tr>{headers.map((cell, cellIndex) => <th key={cellIndex}>{renderInline(cell, `th-${cellIndex}`)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{headers.map((_, cellIndex) => <td key={cellIndex}>{renderInline(row[cellIndex] || '', `td-${rowIndex}-${cellIndex}`)}</td>)}</tr>)}</tbody></table>);
      continue;
    }
    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unordered || ordered) {
      flushParagraph();
      const orderedList = Boolean(ordered);
      if (list && list.ordered !== orderedList) flushList();
      if (!list) list = { ordered: orderedList, items: [] };
      list.items.push((ordered || unordered)![1]);
      index += 1;
      continue;
    }
    flushList(); paragraph.push(line); index += 1;
  }
  flushParagraph(); flushList();
  return <div className="markdown-content">{blocks}</div>;
}

function renderContent(content: string, role: Message['role']) { return role === 'assistant' ? <MarkdownContent content={content} /> : <span className="plain-content">{content}</span>; }

function MessageCopyAction({ content, onEdit }: { content: string; onEdit?: () => void }) {
  const { t } = useI18n();
  const [status, setStatus] = useState<'idle' | 'copied' | 'error'>('idle');
  const requestRef = useRef(0);
  const resetTimerRef = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    setStatus('idle');
    return () => {
      requestRef.current += 1;
      clearTimeout(resetTimerRef.current);
    };
  }, [content]);

  async function copy() {
    const request = ++requestRef.current;
    clearTimeout(resetTimerRef.current);
    setStatus('idle');
    try {
      await navigator.clipboard.writeText(content);
      if (request !== requestRef.current) return;
      setStatus('copied');
      resetTimerRef.current = setTimeout(() => setStatus('idle'), 2000);
    } catch {
      if (request === requestRef.current) setStatus('error');
    }
  }

  return <div className={`message-actions${status === 'error' ? ' has-error' : ''}`}>
    <button type="button" className={`message-action${status === 'copied' ? ' is-copied' : ''}`}
      aria-label={status === 'copied' ? t('Copied', '已复制') : t('Copy', '复制')} title={status === 'copied' ? t('Copied', '已复制') : t('Copy', '复制')}
      onClick={() => void copy()}>
      <Icon name={status === 'copied' ? 'check' : 'copy'} size={16} />
    </button>
    {onEdit ? <button type="button" className="message-action" aria-label={t('Edit prompt', '修改提示词')} title={t('Edit prompt', '修改提示词')} onClick={onEdit}><Icon name="edit" size={16} /></button> : null}
    {status === 'error' ? <span className="message-copy-error" role="alert">{t('Copy failed. Try again or select the text manually.', '复制失败，请重试或手动选择文本')}</span> : null}
  </div>;
}

function PromptEditor({ message, onCancel, onSubmit }: { message: Message; onCancel: () => void; onSubmit: (content: string) => void }) {
  const { t } = useI18n();
  const [value, setValue] = useState(message.content);
  const textarea = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const input = textarea.current;
    if (!input) return;
    const resize = () => {
      input.style.height = 'auto';
      input.style.height = `${Math.min(360, Math.max(100, input.scrollHeight))}px`;
    };
    resize();
    let width = input.getBoundingClientRect().width;
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(entries => {
      const nextWidth = entries[0]?.contentRect.width;
      if (nextWidth !== undefined && nextWidth !== width) { width = nextWidth; resize(); }
    });
    observer.observe(input);
    return () => observer.disconnect();
  }, [value]);
  useEffect(() => {
    const input = textarea.current;
    if (!input) return;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    input.scrollTop = input.scrollHeight;
  }, []);
  return <form className="prompt-editor" onSubmit={event => { event.preventDefault(); if (value.trim()) onSubmit(value.trim()); }}>
    {message.attachments?.length ? <div className="message-attachments">{message.attachments.map(file => <span key={file.file_token}><FileTypeIcon filename={file.filename} /> {file.filename}</span>)}</div> : null}
    <textarea ref={textarea} aria-label={t('Edit this prompt', '修改本轮提示词')} value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === 'Escape') { event.preventDefault(); onCancel(); }
      if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && value.trim()) { event.preventDefault(); onSubmit(value.trim()); }
    }} />
    <div className="prompt-editor-actions"><button type="button" onClick={onCancel}>{t('Cancel', '取消')}</button><button type="submit" disabled={!value.trim()} aria-label={t('Send edited prompt', '发送修改后的提示词')}>{t('Send', '发送')}</button></div>
  </form>;
}

export type ChatPaneProps = ConversationInputAreaProps & {
  messages: Message[];
  conversationKey?: string;
  emptyProjectName?: string;
  hasGraph: boolean;
  onOpenGraph: () => void;
  runTools?: ToolCall[];
  plan?: Plan | null;
  onEditLatest?: (message: Message, runId: string, content: string) => void;
  /** Session that owns published artifacts; absent for unsaved drafts. */
  artifactSessionId?: string;
};

const BOTTOM_THRESHOLD = 48;

function isNearBottom(element: HTMLDivElement) {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= BOTTOM_THRESHOLD;
}

export function ChatPane({ messages, conversationKey = 'default', emptyProjectName, hasGraph, onOpenGraph, runTools, plan, onEditLatest, artifactSessionId, ...inputProps }: ChatPaneProps) {
  const { t } = useI18n();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const latestTurn = editableLatestTurn(messages);
  const editableKey = onEditLatest && latestTurn && !inputProps.loading && !inputProps.uploading && !inputProps.disabled && !inputProps.sendBlocked
    ? `${conversationKey}:${latestTurn.assistant.run_id}` : null;
  useEffect(() => { setEditingKey(null); }, [editableKey]);
  const [versionSelection, setVersionSelection] = useState<Record<string, string>>({});
  const composerAvailable = inputProps.interaction.kind === 'composer';
  const scrollRef = useRef<HTMLDivElement>(null);
  const sticksToBottomRef = useRef(true);
  const initializedRef = useRef(false);
  const previousConversationRef = useRef(conversationKey);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  let liveAssistantIndex = -1;
  if (inputProps.loading) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'assistant') { liveAssistantIndex = index; break; }
    }
  }

  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const element = scrollRef.current;
    if (!element) return;
    sticksToBottomRef.current = true;
    setShowJumpToLatest(false);
    if (behavior === 'smooth' && typeof element.scrollTo === 'function') {
      element.scrollTo({ top: element.scrollHeight, behavior });
    } else {
      element.scrollTop = element.scrollHeight;
    }
  }, []);

  const handleScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    const atBottom = isNearBottom(element);
    sticksToBottomRef.current = atBottom;
    setShowJumpToLatest(!atBottom);
  }, []);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const conversationChanged = previousConversationRef.current !== conversationKey;
    const shouldForceLatest = !initializedRef.current || conversationChanged;
    initializedRef.current = true;
    previousConversationRef.current = conversationKey;

    if (shouldForceLatest || sticksToBottomRef.current) {
      scrollToLatest('auto');
      return;
    }
    setShowJumpToLatest(!isNearBottom(element));
  }, [conversationKey, messages, plan, runTools, scrollToLatest]);

  return <main className="chat-pane">
    <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
      {messages.length === 0 ? <div className="empty-state"><div className="empty-icon"><BrandLogo /></div><h1>{emptyProjectName ? <>{t('Start graph analysis in', '开始在')} <span className="empty-project-name">{emptyProjectName}</span>{t('', ' 中图分析')}</> : t('Start graph analysis', '开始图分析')}</h1><p>{t('Paste graph edges, upload a file, or ask in natural language', '粘贴图的边数据、上传文件，或直接用自然语言提问')}</p>{composerAvailable ? <div className="suggestions"><button onClick={() => inputProps.onChange(t('Inspect the current graph structure and basic properties', '请检查当前图的结构和基本属性'))}>{t('Inspect graph structure', '检查图结构')}</button><button onClick={() => inputProps.onChange(t('Calculate node degrees and find the node with the highest degree', '请计算节点的度数并找出最大度节点'))}>{t('Calculate node degrees', '计算图节点度数')}</button><button onClick={() => inputProps.onChange(t('Run community detection on this graph', '请帮我进行社区检测'))}>{t('Detect communities', '进行社区检测')}</button></div> : null}</div> : messages.map((message, index) => {
        const isAssistant = message.role === 'assistant';
        const isLiveAssistant = isAssistant && index === liveAssistantIndex;
        const versions = isAssistant ? answerVersions(message, messages) : [message];
        const versionKey = conversationKey + ':' + (message.id || index);
        const selectedVersion = !isLiveAssistant ? versions.find(item => item.run_id === versionSelection[versionKey]) : undefined;
        const displayed = selectedVersion || message;
        const versionIndex = versions.indexOf(displayed);
        const visibleTools = isLiveAssistant && runTools?.length ? runTools : displayed.tools;
        const visiblePlan = isLiveAssistant && plan ? plan : displayed.plan;
        const isEditing = Boolean(editableKey && editingKey === editableKey && message === latestTurn?.user);
        // Attachment chips sit above the user bubble so they read as separate
        // files rather than as part of the sent text.
        const attachmentChips = displayed.attachments?.length
          ? <div className="message-attachments">{displayed.attachments.map(file => <span key={file.file_token || file.filename}><FileTypeIcon filename={file.filename} /> {file.filename || t('Attachment', '附件')}</span>)}</div>
          : null;
        const contents = <div className="message-content">{renderContent(displayed.content || (message.streaming ? t('Analyzing…', '正在分析…') : ''), message.role)}</div>;
        return <div className={`message-row ${message.role}${isEditing ? ' is-editing' : ''}`} key={versionKey}>
          {isAssistant ? <div className="message-avatar assistant"><Icon name="spark" size={15} /></div> : null}
          <div className="message-body">
            <div className="message-label">{isAssistant ? (message.regenerate_of ? t('iGraph Assistant · Regenerated', 'iGraph 助手 · 重新生成') : t('iGraph Assistant', 'iGraph 助手')) : t('You', '你')}</div>
            {isAssistant ? (displayed.processItems?.length ? <ProcessTimeline items={displayed.processItems} tools={visibleTools} compact={isLiveAssistant} streaming={isLiveAssistant} /> : toolList(visibleTools, t)) : null}
            {isAssistant ? planCard(visiblePlan, t) : null}
            {!isAssistant && !isEditing ? attachmentChips : null}
            {isEditing ? <PromptEditor message={message} onCancel={() => setEditingKey(null)} onSubmit={content => {
              if (latestTurn && onEditLatest) {
                scrollToLatest('auto');
                onEditLatest(message, latestTurn.assistant.run_id!, content);
              }
              setEditingKey(null);
            }} /> : isAssistant ? contents : <div className="message-bubble">{contents}</div>}
            {isAssistant && displayed.artifacts?.length ? <ArtifactList artifacts={displayed.artifacts} sessionId={artifactSessionId} /> : null}
            {displayed.graphs?.length ? <button className="inline-result" onClick={onOpenGraph}><Icon name="graph" size={16} /> {t('View graph result', '查看图结果')} <span>→</span></button> : null}
            {versions.length > 1 && !message.streaming && !isLiveAssistant ? <nav className="answer-versions" aria-label={t('Answer versions', '回答版本')}>
              <button type="button" aria-label={t('Previous answer version', '上一个回答版本')} disabled={versionIndex <= 0} onClick={() => setVersionSelection(current => ({ ...current, [versionKey]: versions[versionIndex - 1].run_id! }))}>‹</button>
              <span>{t(`Version ${versionIndex + 1} / ${versions.length}`, `版本 ${versionIndex + 1} / ${versions.length}`)}</span>
              <button type="button" aria-label={t('Next answer version', '下一个回答版本')} disabled={versionIndex >= versions.length - 1} onClick={() => setVersionSelection(current => ({ ...current, [versionKey]: versions[versionIndex + 1].run_id! }))}>›</button>
            </nav> : null}
            {displayed.content && !isEditing ? <MessageCopyAction content={displayed.content}
              onEdit={editableKey && message === latestTurn?.user ? () => setEditingKey(editableKey) : undefined} /> : null}
          </div>
        </div>;
      })}
      {inputProps.loading && liveAssistantIndex < 0 && (plan?.steps?.length || runTools?.length) ? <div className="live-analysis-card">{toolList(runTools, t)}{planCard(plan, t)}</div> : null}
      {hasGraph && messages.length > 0 ? <button className="graph-context-link" onClick={onOpenGraph}><Icon name="graph" size={15} /> {t('Current graph loaded · View it in the workspace on the right', '当前图已加载 · 在右侧工作台查看')}</button> : null}
    </div>
    {showJumpToLatest ? <div className="jump-to-latest-anchor"><button className="jump-to-latest" type="button" onClick={() => scrollToLatest()} aria-label={t('Jump to latest message', '滚动到最新消息')} title={t('Jump to latest message', '滚动到最新消息')}><Icon name="chevron" size={18} /></button></div> : null}
    <ConversationInputArea {...inputProps} onSend={() => {
      // Follow an explicit send, not message IDs replaced by persisted snapshots.
      scrollToLatest('auto');
      inputProps.onSend();
    }} />
  </main>;
}
