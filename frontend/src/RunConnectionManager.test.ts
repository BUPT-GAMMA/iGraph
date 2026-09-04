import { afterEach, describe, expect, it, vi } from 'vitest';
import { RunConnectionManager } from './RunConnectionManager';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
describe('RunConnectionManager', () => {
  it('foreground acquisition evicts only the old observer and invalidates its pending work', async () => {
    vi.useFakeTimers();
    const manager = new RunConnectionManager(1), detached = vi.fn(), retry = vi.fn();
    const first = manager.acquire({ sessionId: 'A', generation: 'g', foreground: true, retry, onDetached: detached })!;
    manager.schedule(first, vi.fn(), 30000);
    const pending = manager.query('A', 'g', async () => new Promise(() => {})).catch(error => error);
    const next = manager.acquire({ sessionId: 'B', generation: 'b', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    expect(first.controller.signal.aborted).toBe(true);
    expect(manager.isCurrent(first)).toBe(false);
    expect(detached).toHaveBeenCalledTimes(1);
    expect(manager.all()).toEqual([next]);
    await pending;
    manager.release('B', 'b'); await Promise.resolve();
    expect(retry).toHaveBeenCalledTimes(1);
    manager.abortAll(); expect(vi.getTimerCount()).toBe(0);
  });
  it('caps live connections, detaches overflow, and resumes it when a slot opens', async () => {
    vi.useFakeTimers();
    const manager = new RunConnectionManager(2);
    const retry = vi.fn();
    const detached = vi.fn();
    manager.acquire({ sessionId: 'A', generation: 'A', foreground: false, retry: vi.fn(), onDetached: vi.fn() });
    manager.acquire({ sessionId: 'B', generation: 'B', foreground: false, retry: vi.fn(), onDetached: vi.fn() });

    expect(manager.acquire({ sessionId: 'C', generation: 'C', foreground: false, retry, onDetached: detached })).toBeNull();
    expect(detached).toHaveBeenCalledTimes(1);

    manager.release('A', 'A');
    vi.runAllTimers();
    await Promise.resolve();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('uses bounded jittered backoff and resets it after an event', () => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(.5);
    const manager = new RunConnectionManager(1);
    const callback = vi.fn();
    manager.acquire({ sessionId: 'A', generation: 'gen', foreground: true, retry: vi.fn(), onDetached: vi.fn() });
    manager.scheduleReconnect('A', 'gen', callback);
    vi.advanceTimersByTime(999);
    expect(callback).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(callback).toHaveBeenCalledTimes(1);

    manager.receivedEvent('A', 'gen');
    const next = vi.fn();
    manager.scheduleReconnect('A', 'gen', next);
    vi.advanceTimersByTime(1000);
    expect(next).toHaveBeenCalledTimes(1);
  });

  it('does not replace an existing same-session connection', () => {
    const manager = new RunConnectionManager(2);
    const first = manager.acquire({ sessionId: 'A', generation: 'first', foreground: true, retry: vi.fn(), onDetached: vi.fn() });
    const abort = vi.spyOn(first!.controller, 'abort');

    const duplicate = manager.acquire({ sessionId: 'A', generation: 'second', foreground: true, retry: vi.fn(), onDetached: vi.fn() });

    expect(duplicate).toBeNull();
    expect(abort).not.toHaveBeenCalled();
    expect(manager.get('A')?.generation).toBe('first');
  });

  it('never displaces a protected feedback connection', () => {
    const manager = new RunConnectionManager(1);
    const protectedConnection = manager.acquire({ sessionId: 'A', generation: 'feedback', foreground: true, protected: true, retry: vi.fn(), onDetached: vi.fn() });
    const abort = vi.spyOn(protectedConnection!.controller, 'abort');
    const detached = vi.fn();

    expect(manager.acquire({ sessionId: 'B', generation: 'new', foreground: true, retry: vi.fn(), onDetached: detached })).toBeNull();
    expect(abort).not.toHaveBeenCalled();
    expect(detached).toHaveBeenCalledTimes(1);
  });
});
