import { Fragment, type ReactNode, useCallback, useLayoutEffect, useRef, useState } from 'react';
import type { Message, Plan, ToolCall } from '../types';
import { ConversationInputArea, type ConversationInputAreaProps } from './ConversationInputArea';
import { BrandLogo, Icon } from './Icon';
import { ProcessTimeline } from './ProcessTimeline';

function planCard(plan?: Plan | null) {
  if (!plan?.steps?.length) return null;
  const done = plan.steps.filter(step => step.status === 'done' || step.status === 'completed').length;
  return <div className="plan-card"><div className="card-heading"><strong>分析计划</strong><span>{done} / {plan.steps.length} 已完成</span></div>{plan.steps.map((step, index) => <div className="plan-step" key={`${step.step_id}-${index}`}><span className={step.status === 'running' ? 'step-dot running' : 'step-dot'}>{step.status === 'done' || step.status === 'completed' ? <Icon name="check" size={12} /> : index + 1}</span><span>{step.goal || step.result_summary || '分析步骤'}</span></div>)}</div>;
}

function toolList(tools?: ToolCall[]) {
  if (!tools?.length) return null;
  return <div className="tool-list">{tools.map((tool, index) => {
    const progress = tool.progress || {};
    const epoch = Number(progress.epoch);
    const epochs = Number(progress.epochs);
    const hasEpoch = Number.isFinite(epoch) && Number.isFinite(epochs) && epochs > 0;
    const progressLabel = hasEpoch ? `轮次 ${epoch}/${epochs}` : progress.message;
    const statusLabel = tool.done ? '已完成' : progressLabel ? String(progressLabel) : '运行中';
    return <div className={`tool-row ${tool.done ? 'done' : 'running'}`} key={`${tool.tool}-${index}`} role="status" aria-label={`${tool.tool || 'graph_tool'}：${statusLabel}`} title={`${tool.tool || 'graph_tool'} · ${statusLabel}`}>
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

export type ChatPaneProps = ConversationInputAreaProps & {
  messages: Message[];
  conversationKey?: string;
  emptyProjectName?: string;
  hasGraph: boolean;
  onOpenGraph: () => void;
  runTools?: ToolCall[];
  plan?: Plan | null;
  onRegenerate?: (message: Message, runId: string) => void;
};

const BOTTOM_THRESHOLD = 48;

function isNearBottom(element: HTMLDivElement) {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= BOTTOM_THRESHOLD;
}

export function ChatPane({ messages, conversationKey = 'default', emptyProjectName, hasGraph, onOpenGraph, runTools, plan, onRegenerate, ...inputProps }: ChatPaneProps) {
  const [copyStatus, setCopyStatus] = useState('');
  async function copyMessage(content: string) {
    try { await navigator.clipboard.writeText(content); setCopyStatus('已复制'); }
    catch { setCopyStatus('复制失败，请手动选择内容'); }
  }
  const composerAvailable = inputProps.interaction.kind === 'composer';
  const scrollRef = useRef<HTMLDivElement>(null);
  const sticksToBottomRef = useRef(true);
  const initializedRef = useRef(false);
  const previousConversationRef = useRef(conversationKey);
  const previousLatestUserRef = useRef<string | undefined>(undefined);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const latestUserId = [...messages].reverse().find(message => message.role === 'user')?.id;
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
    const userSentMessage = Boolean(latestUserId && previousLatestUserRef.current !== latestUserId);
    const shouldForceLatest = !initializedRef.current || conversationChanged || userSentMessage;
    initializedRef.current = true;
    previousConversationRef.current = conversationKey;
    previousLatestUserRef.current = latestUserId;

    if (shouldForceLatest || sticksToBottomRef.current) {
      scrollToLatest('auto');
      return;
    }
    setShowJumpToLatest(!isNearBottom(element));
  }, [conversationKey, latestUserId, messages, plan, runTools, scrollToLatest]);

  return <main className="chat-pane">
    <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
      {messages.length === 0 ? <div className="empty-state"><div className="empty-icon"><BrandLogo /></div><h1>{emptyProjectName ? <>开始在 <span className="empty-project-name">{emptyProjectName}</span> 中图分析</> : '开始图分析'}</h1><p>粘贴图的边数据、上传文件，或直接用自然语言提问</p>{composerAvailable ? <div className="suggestions"><button onClick={() => inputProps.onChange('请检查当前图的结构和基本属性')}>检查图结构</button><button onClick={() => inputProps.onChange('请计算节点的度数并找出最大度节点')}>计算图节点度数</button><button onClick={() => inputProps.onChange('请帮我进行社区检测')}>进行社区检测</button></div> : null}</div> : messages.map((message, index) => {
        const isAssistant = message.role === 'assistant';
        const isLiveAssistant = isAssistant && index === liveAssistantIndex;
        const visibleTools = isLiveAssistant && runTools?.length ? runTools : message.tools;
        const previousUser = messages.slice(0, index).reverse().find(item => item.role === 'user');
        const visiblePlan = isLiveAssistant && plan ? plan : message.plan;
        return <div className={`message-row ${message.role}`} key={message.id || `${index}-${message.content.slice(0, 8)}`}>{isAssistant ? <div className="message-avatar assistant"><Icon name="spark" size={15} /></div> : null}<div className="message-body"><div className="message-label">{isAssistant ? (message.regenerate_of ? 'iGraph 助手 · 重新生成' : 'iGraph 助手') : '你'}</div>{isAssistant ? (message.processItems?.length ? <ProcessTimeline items={message.processItems} tools={visibleTools} compact={isLiveAssistant} streaming={isLiveAssistant} /> : toolList(visibleTools)) : null}{isAssistant ? planCard(visiblePlan) : null}<div className="message-content">{renderContent(message.content || (message.streaming ? '正在分析…' : ''), message.role)}</div>{message.attachments?.length ? <div className="message-attachments">{message.attachments.map(file => <span key={file.file_token}><Icon name="file" size={13} /> {file.filename || '附件'}</span>)}</div> : null}<div className="message-actions">{isAssistant && index === messages.length - 1 && previousUser && message.run_id && onRegenerate && <button type="button" disabled={inputProps.disabled || inputProps.sendBlocked || inputProps.uploading || inputProps.loading || Boolean(inputProps.value.trim()) || Boolean(inputProps.attachments?.length)} title="保留已有回复和上下文；请先发送或清空当前草稿" onClick={() => onRegenerate(previousUser, message.run_id!)}>重新生成</button>}{message.content && <button type="button" onClick={() => void copyMessage(message.content)}>复制</button>}{message.role === 'user' && composerAvailable && <button type="button" disabled={inputProps.disabled || inputProps.sendBlocked} onClick={() => inputProps.onChange(message.content)}>编辑并重发</button>}</div>{message.graphs?.length ? <button className="inline-result" onClick={onOpenGraph}><Icon name="graph" size={16} /> 查看图结果 <span>→</span></button> : null}</div></div>;
      })}
      {inputProps.loading && liveAssistantIndex < 0 && (plan?.steps?.length || runTools?.length) ? <div className="live-analysis-card">{toolList(runTools)}{planCard(plan)}</div> : null}
      {hasGraph && messages.length > 0 ? <button className="graph-context-link" onClick={onOpenGraph}><Icon name="graph" size={15} /> 当前图已加载 · 在右侧工作台查看</button> : null}
    </div>
    {showJumpToLatest ? <div className="jump-to-latest-anchor"><button className="jump-to-latest" type="button" onClick={() => scrollToLatest()} aria-label="滚动到最新消息" title="滚动到最新消息"><Icon name="chevron" size={18} /></button></div> : null}
    {copyStatus && <span role="status" className="message-copy-status">{copyStatus}</span>}
    <ConversationInputArea {...inputProps} />
  </main>;
}
