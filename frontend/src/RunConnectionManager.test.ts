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
