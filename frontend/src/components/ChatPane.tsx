import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { MarkdownContent } from './MarkdownContent';
import type { ArtifactRef, Message, Plan, ToolCall } from '../types';
import { ConversationInputArea, type ConversationInputAreaProps } from './ConversationInputArea';
import { editableLatestTurn } from '../latestTurn';
import { localizeUserCase, nextExampleQuestion, type UserCase } from '../userCases';
import { answerVersions } from '../answerVersions';
import { FileTypeIcon } from './FileTypeIcon';
import { BrandLogo, Icon } from './Icon';
import { ProcessTimeline } from './ProcessTimeline';
import { fetchArtifact } from './WorkspaceFiles';
import { useI18n, type Translate } from '../i18n';
import { executionLabel, executionPending, executionStatus } from '../toolStatus';

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
    const pending = executionPending(tool);
    const statusLabel = pending && progressLabel ? String(progressLabel) : executionLabel(tool, t);
    return <div className={`tool-row ${pending ? 'running' : 'done'}`} key={`${tool.tool}-${index}`} role="status" aria-label={`${tool.tool || 'graph_tool'}${t(': ', '：')}${statusLabel}`} title={`${tool.tool || 'graph_tool'} · ${statusLabel}`}>
      <span className={pending ? 'tool-state' : 'tool-state done'}>{pending ? <span className="loader" /> : <Icon name={executionStatus(tool) === 'completed' ? 'check' : 'close'} size={12} />}</span>
      <span>{tool.tool || 'graph_tool'}</span>
      {progressLabel ? <span className="tool-progress-label">{String(progressLabel)}</span> : null}
    </div>;
  })}</div>;
}


function renderContent(content: string, role: Message['role']) {
  // Runtime IDs live in structured message fields. Never infer metadata from
  // prose: legitimate examples and code can contain the very same identifiers.
  return role === 'assistant'
    ? <MarkdownContent content={content} />
    : <span className="plain-content">{content}</span>;
}

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
  emptyContent?: ReactNode;
  exampleCase?: UserCase;
  onOpenGraph: () => void;
  runTools?: ToolCall[];
  plan?: Plan | null;
  onEditLatest?: (message: Message, runId: string, content: string) => void;
  /** Session that owns published artifacts; absent for unsaved drafts. */
  artifactSessionId?: string;
};

const BOTTOM_THRESHOLD = 48;
const TERMINAL_MESSAGE_STATUSES = new Set(['completed', 'failed', 'cancelled']);

function isNearBottom(element: HTMLDivElement) {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= BOTTOM_THRESHOLD;
}

export function ChatPane({ messages, conversationKey = 'default', emptyProjectName, emptyContent, exampleCase, onOpenGraph, runTools, plan, onEditLatest, artifactSessionId, ...inputProps }: ChatPaneProps) {
  const { t } = useI18n();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const latestTurn = editableLatestTurn(messages);
  const editableKey = onEditLatest && latestTurn && !inputProps.loading && !inputProps.uploading && !inputProps.disabled && !inputProps.sendBlocked
    ? `${conversationKey}:${latestTurn.assistant.run_id}` : null;
  useEffect(() => { setEditingKey(null); }, [editableKey]);
  const [versionSelection, setVersionSelection] = useState<Record<string, string>>({});
  const composerAvailable = inputProps.interaction.kind === 'composer';
  const exampleQuestions = exampleCase ? localizeUserCase(exampleCase, t).questions : undefined;
  const exampleSuggestion = !editingKey ? nextExampleQuestion(exampleCase, messages, t) : undefined;
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

    if (messages.length === 0) {
      if (shouldForceLatest) element.scrollTop = 0;
      setShowJumpToLatest(false);
      return;
    }

    if (shouldForceLatest || sticksToBottomRef.current) {
      scrollToLatest('auto');
      return;
    }
    setShowJumpToLatest(!isNearBottom(element));
  }, [conversationKey, messages, plan, runTools, scrollToLatest]);

  return <main className="chat-pane">
    <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
      {messages.length === 0 ? <><div className="empty-state"><div className="empty-icon"><BrandLogo /></div><h1>{emptyProjectName ? <>{t('Start graph analysis in', '开始在')} <span className="empty-project-name">{emptyProjectName}</span>{t('', ' 中图分析')}</> : t('Start graph analysis', '开始图分析')}</h1>{composerAvailable && !emptyContent ? <div className="suggestions">{exampleQuestions?.length ? exampleQuestions.map(question => <button key={question} onClick={() => inputProps.onChange(question)}>{question}</button>) : <><button onClick={() => inputProps.onChange(t('Inspect the current graph structure and basic properties', '请检查当前图的结构和基本属性'))}>{t('Inspect graph structure', '检查图结构')}</button><button onClick={() => inputProps.onChange(t('Calculate node degrees and find the node with the highest degree', '请计算节点的度数并找出最大度节点'))}>{t('Calculate node degrees', '计算图节点度数')}</button><button onClick={() => inputProps.onChange(t('Run community detection on this graph', '请帮我进行社区检测'))}>{t('Detect communities', '进行社区检测')}</button></>}</div> : null}</div>{composerAvailable && emptyContent}</> : messages.map((message, index) => {
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
        const assistantOutputPending = isAssistant && (isLiveAssistant || displayed.streaming === true
          || Boolean(displayed.run_status && !TERMINAL_MESSAGE_STATUSES.has(displayed.run_status)));
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
            {message.history_compacted && <div className="message-model-provenance">{t('Earlier context compacted for this model', '较早上下文已压缩以适应此模型')}</div>}
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
            {displayed.graphs?.length ? <button className="inline-result" onClick={onOpenGraph}>{t('View graph result', '查看图结果')} <span>→</span></button> : null}
            {versions.length > 1 && !message.streaming && !isLiveAssistant ? <nav className="answer-versions" aria-label={t('Answer versions', '回答版本')}>
              <button type="button" aria-label={t('Previous answer version', '上一个回答版本')} disabled={versionIndex <= 0} onClick={() => setVersionSelection(current => ({ ...current, [versionKey]: versions[versionIndex - 1].run_id! }))}>‹</button>
              <span>{t(`Version ${versionIndex + 1} / ${versions.length}`, `版本 ${versionIndex + 1} / ${versions.length}`)}</span>
              <button type="button" aria-label={t('Next answer version', '下一个回答版本')} disabled={versionIndex >= versions.length - 1} onClick={() => setVersionSelection(current => ({ ...current, [versionKey]: versions[versionIndex + 1].run_id! }))}>›</button>
            </nav> : null}
            {displayed.content && !isEditing && !assistantOutputPending ? <MessageCopyAction content={displayed.content}
              onEdit={editableKey && message === latestTurn?.user ? () => setEditingKey(editableKey) : undefined} /> : null}
          </div>
        </div>;
      })}
      {inputProps.loading && liveAssistantIndex < 0 && (plan?.steps?.length || runTools?.length) ? <div className="live-analysis-card">{toolList(runTools, t)}{planCard(plan, t)}</div> : null}
    </div>
    {showJumpToLatest ? <div className="jump-to-latest-anchor"><button className="jump-to-latest" type="button" onClick={() => scrollToLatest()} aria-label={t('Jump to latest message', '滚动到最新消息')} title={t('Jump to latest message', '滚动到最新消息')}><Icon name="chevron" size={18} /></button></div> : null}
    <ConversationInputArea {...inputProps} exampleSuggestion={exampleSuggestion} onSend={() => {
      // Follow an explicit send, not message IDs replaced by persisted snapshots.
      scrollToLatest('auto');
      inputProps.onSend();
    }} />
  </main>;
}
