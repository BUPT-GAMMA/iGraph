import { afterEach, describe, expect, it, vi } from 'vitest';
import { RunConnectionManager } from './RunConnectionManager';

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});
describe('RunConnectionManager', () => {
  it('caps live connections, detaches overflow, and resumes it when a slot opens', () => {
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
});
