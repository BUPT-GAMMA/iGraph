import { describe, expect, it } from 'vitest';
import { executionError, executionPending, executionStatus } from './toolStatus';

describe('execution status', () => {
  it.each(['failed', 'cancelled', 'cancelling', 'queued', 'running', 'unavailable', 'completed'] as const)('honors %s instead of done', status => {
    expect(executionStatus({ done: true, result: { job_status: status } })).toBe(status);
  });
  it('uses latest job progress instead of the completed submission', () => {
    expect(executionStatus({ done: true, status: 'completed', result: { async: true, status: 'queued' }, progress: { status: 'failed' } })).toBe('failed');
    expect(executionPending({ done: true, result: { async: true, status: 'queued' } })).toBe(true);
    expect(executionStatus({ done: true, progress: { kind: 'job_cancelled' } })).toBe('cancelled');
  });
  it('recognizes structured, JSON and validation failures', () => {
    for (const result of [{ success: false, error: { message: 'Invalid model' } }, JSON.stringify({ success: false, error: 'Invalid model' }), 'Validation error: Invalid model']) {
      expect(executionStatus({ done: true, result })).toBe('failed');
      expect(executionError({ done: true, result })).toContain('Invalid model');
    }
    expect(executionStatus({ done: true, progress: { status: 'running' }, result: { success: false } })).toBe('failed');
  });
  it('does not turn cancellation errors into training failures', () => {
    expect(executionStatus({ done: true, result: { success: false, error: 'Cancelled', job_status: 'cancelled' } })).toBe('cancelled');
  });
});
