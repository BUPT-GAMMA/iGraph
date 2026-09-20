import type { ToolCall } from './types';
import type { Translate } from './i18n';

export type ExecutionStatus = 'queued' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'unavailable';

function resultRecord(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try { return resultRecord(JSON.parse(value)); } catch { return {}; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function normalize(value: unknown): ExecutionStatus | undefined {
  const status = String(value || '').toLowerCase().replace(/^job_/, '');
  if (status === 'done' || status === 'completed') return 'completed';
  if (['queued', 'running', 'cancelling', 'failed', 'cancelled', 'unavailable'].includes(status)) return status as ExecutionStatus;
}

export function executionStatus(tool: ToolCall & { status_unavailable?: boolean } = {}): ExecutionStatus {
  if (tool.status_unavailable) return 'unavailable';
  const result = resultRecord(tool.result);
  // Job snapshots/events describe the work itself; tool completion may only mean submission.
  const jobStatus = normalize(result.job_status) || normalize(tool.progress?.status) || normalize(tool.progress?.kind);
  const status = normalize(result.status);
  if (jobStatus && ['completed', 'failed', 'cancelled', 'unavailable'].includes(jobStatus)) return jobStatus;
  if (status === 'cancelled' || status === 'failed') return status;
  if (result.success === false || result.error || tool.status === 'failed') return 'failed';
  if (typeof tool.result === 'string' && !Object.keys(result).length && /validation errors?|error (?:from|executing) .*|invalid (?:argument|input)|tool.*failed/i.test(tool.result)) return 'failed';
  return jobStatus || status || normalize(tool.status) || (tool.done ? 'completed' : 'running');
}

export function executionPending(tool: ToolCall): boolean {
  return ['queued', 'running', 'cancelling'].includes(executionStatus(tool));
}

export function executionLabel(tool: ToolCall, t: Translate): string {
  const labels: Record<ExecutionStatus, [string, string]> = {
    queued: ['Queued', '排队中'], running: ['Running', '运行中'], cancelling: ['Stopping', '正在停止'],
    completed: ['Completed', '已完成'], failed: ['Failed', '失败'], cancelled: ['Stopped', '已终止'], unavailable: ['Status unavailable', '状态不可用'],
  };
  return t(...labels[executionStatus(tool)]);
}

export function executionError(tool: ToolCall): string {
  if (executionStatus(tool) !== 'failed') return '';
  const result = resultRecord(tool.result);
  const error = result.error || tool.progress?.error;
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object' && 'message' in error) return String(error.message);
  if (typeof tool.result === 'string' && !Object.keys(result).length) return tool.result;
  return String(tool.progress?.message || '');
}
