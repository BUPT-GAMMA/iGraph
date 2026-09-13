import type { ProcessItem, ToolCall } from '../types';
import { Icon } from './Icon';

function toolStatus(tool?: ToolCall) {
  if (tool?.status === 'failed') return { label: '失败', className: 'is-error' };
  if (tool?.status === 'cancelled') return { label: '已取消', className: 'is-cancelled' };
  if (tool?.done || tool?.status === 'completed') return { label: '完成', className: 'is-complete' };
  return { label: '运行中', className: 'is-running' };
}

function formatError(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) return String(error.message).split('\n')[0];
  return typeof error === 'string' ? error.split('\n')[0] : '工具执行失败';
}

function safeError(tool: ToolCall) {
  if (tool.status !== 'failed') return '';
  const result = tool.result;
  if (result && typeof result === 'object' && 'error' in result) return formatError((result as { error?: unknown }).error);
  return '工具执行失败';
}

export function ProcessTimeline({ items = [], tools: liveTools = [], compact = false, streaming = false, thinkingDurationMs }: { items?: ProcessItem[]; tools?: ToolCall[]; compact?: boolean; streaming?: boolean; thinkingDurationMs?: number }) {
  if (!items.length) return null;
  const ordered = items.map(item => {
    if (item.kind !== 'tool') return item;
    const live = liveTools.find(tool => tool.call_id && tool.call_id === item.tool?.call_id);
    if (!live) return item;
    // Graph jobs can continue after the agent turn; dev's job state remains authoritative.
    const result = live.result && typeof live.result === 'object' ? live.result as Record<string, unknown> : {};
    const jobId = live.job_id || result.job_id || live.progress?.job_id;
    const failed = result.success === false || result.status === 'failed' || live.progress?.kind === 'job_failed';
    const cancelled = result.status === 'cancelled' || live.progress?.kind === 'job_cancelled';
    const status: ToolCall['status'] = cancelled ? 'cancelled' : failed ? 'failed' : live.done ? 'completed' : 'running';
    return { ...item, tool: { ...item.tool, progress: live.progress || item.tool?.progress,
      ...(jobId ? { ...live, status } : {}) } };
  }).sort((a, b) => a.sequence - b.sequence);
  const tools = ordered.filter(item => item.kind === 'tool');
  const running = tools.filter(item => !item.tool?.done && !['completed', 'failed', 'cancelled'].includes(item.tool?.status || '')).length;
  const label = running ? `${running} 项运行中` : `${ordered.length} 项`;
  const seconds = thinkingDurationMs === undefined ? undefined : Math.max(0, Math.round(thinkingDurationMs / 1000));
  const heading = seconds === undefined ? (streaming ? '正在执行' : '执行过程')
    : streaming ? '思考中 · ' + seconds + 's' : '思考了 ' + seconds + 's';
  return <details className={`process-panel ${compact ? 'is-compact' : ''}`} open={compact && running > 0}>
    <summary><Icon name="chevron" size={13} /><span>{heading}</span><span className="process-count">{label}</span></summary>
    <div className="process-timeline">
    {ordered.map(item => {
      if (item.kind === 'text') return <div className="process-item process-text-item" key={item.id}>
        <span className="process-kind">Agent</span><span className="process-summary">{item.content || ''}</span>
      </div>;
      if (item.kind === 'reasoning') return <div className="process-item process-reasoning-item" key={item.id}>
        <span className="process-kind">Reasoning</span><span className="process-summary">{item.content || 'Agent 正在分析上下文…'}</span>
      </div>;
      const tool = item.tool;
      const status = toolStatus(tool);
      const epoch = Number(tool?.progress?.epoch);
      const epochs = Number(tool?.progress?.epochs);
      const progress = Number.isFinite(epoch) && Number.isFinite(epochs) && epochs > 0
        ? '轮次 ' + epoch + '/' + epochs : tool?.progress?.message;
      const summary = safeError(tool || {}) || String(progress || status.label);
      return <div className={`process-item process-tool-item ${status.className}`} key={item.id} role="status" aria-label={(tool?.tool || "graph_tool") + "：" + summary}>
        <span className="process-kind">{tool?.tool || 'Graph Tool'}</span><span className="process-summary">{summary}</span>
      </div>;
    })}
    </div>
  </details>;
}
