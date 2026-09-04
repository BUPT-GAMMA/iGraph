export type RunConnection = {
  sessionId: string;
  runId?: string;
  generation: string;
  controller: AbortController;
  reconnectAttempt: number;
  wakeScheduled?: boolean;
  openedAt: number;
  epoch: number;
  lastActivityAt: number;
  lastProgressAt: number;
  lastCheckAt: number;
  observing?: boolean;
  protected?: boolean;
  retry: () => void;
  onDetached: () => void;
};

type DetachedConnection = Omit<RunConnection, 'controller' | 'wakeScheduled'>;
type ClockTask = { at: number; callback: () => void };
type ClockSlot = { timer?: number; wake?: ClockTask; deadline?: ClockTask };

const RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30];

export class RunConnectionManager {
  private connections = new Map<string, RunConnection>();
  private detached = new Map<string, DetachedConnection>();
  private epoch = 0;
  private attempts = new Map<string, { generation: string; count: number }>();
  private controls = new Map<string, { controller: AbortController; promise: Promise<unknown>; generation: string }>();
  private clocks = new Map<string, ClockSlot>();

  constructor(private readonly maxConnections = 4) {}

  get(sessionId: string) { return this.connections.get(sessionId); }
  all() { return [...this.connections.values()]; }

  acquire(options: {
    sessionId: string;
    runId?: string;
    generation: string;
    foreground: boolean;
    protected?: boolean;
    retry: () => void;
    onDetached: () => void;
  }): RunConnection | null {
    if (this.connections.has(options.sessionId)) return null;
    this.detached.delete(options.sessionId);
    if (this.connections.size >= this.maxConnections) {
      const candidates = [...this.connections.values()].filter(connection => !connection.protected).sort((left, right) => left.openedAt - right.openedAt);
      const displaced = options.foreground ? candidates[0] : undefined;
      if (!displaced) {
        this.detached.set(options.sessionId, {
          ...options,
          reconnectAttempt: 0,
          openedAt: Date.now(),
          epoch: ++this.epoch, lastActivityAt: Date.now(), lastProgressAt: Date.now(), lastCheckAt: Date.now(),
        });
        options.onDetached();
        return null;
      }
      this.connections.delete(displaced.sessionId);
      displaced.controller.abort();
      this.cancelControl(displaced.sessionId);
      this.clearScheduled(displaced);
      this.detached.set(displaced.sessionId, {
        sessionId: displaced.sessionId,
        runId: displaced.runId,
        generation: displaced.generation,
        reconnectAttempt: displaced.reconnectAttempt,
        openedAt: displaced.openedAt,
        epoch: displaced.epoch, lastActivityAt: displaced.lastActivityAt, lastProgressAt: displaced.lastProgressAt, lastCheckAt: displaced.lastCheckAt,
        retry: displaced.retry,
        onDetached: displaced.onDetached,
      });
      displaced.onDetached();
    }
    const connection: RunConnection = {
      ...options,
      controller: new AbortController(),
      reconnectAttempt: this.attempts.get(options.sessionId)?.generation === options.generation ? this.attempts.get(options.sessionId)!.count : 0,
      openedAt: Date.now(),
      epoch: ++this.epoch, lastActivityAt: Date.now(), lastProgressAt: Date.now(), lastCheckAt: Date.now(),
    };
    this.detached.delete(options.sessionId);
    this.connections.set(options.sessionId, connection);
    return connection;
  }

  updateRunId(sessionId: string, generation: string, runId: string) {
    const connection = this.connections.get(sessionId);
    if (connection?.generation === generation) connection.runId = runId;
  }

  receivedEvent(sessionId: string, generation: string) {
    const connection = this.connections.get(sessionId);
    if (connection?.generation === generation) {
      connection.reconnectAttempt = 0;
      connection.lastProgressAt = Date.now();
      this.attempts.delete(sessionId);
    }
  }

  isCurrent(connection: RunConnection) { return this.connections.get(connection.sessionId) === connection && !connection.controller.signal.aborted; }

  activity(connection: RunConnection) { if (this.isCurrent(connection)) connection.lastActivityAt = Date.now(); }

  cancelControl(sessionId: string) {
    this.controls.get(sessionId)?.controller.abort();
    this.controls.delete(sessionId);
    this.clockTask(sessionId, 'deadline');
  }

  query<T>(sessionId: string, generation: string, request: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const prior = this.controls.get(sessionId);
    if (prior?.generation === generation) return prior.promise as Promise<T>;
    this.cancelControl(sessionId);
    const controller = new AbortController();
    const signal = controller.signal;
    const deadline = this.clockTask(sessionId, 'deadline', { at: Date.now() + 15000,
      callback: () => controller.abort(new DOMException('恢复查询超时', 'TimeoutError')) });
    const promise = Promise.race([
      Promise.resolve().then(() => { if (signal.aborted) throw signal.reason; return request(signal); }),
      new Promise<never>((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true })),
    ]).finally(() => {
      if (this.clocks.get(sessionId)?.deadline === deadline) this.clockTask(sessionId, 'deadline');
      if (this.controls.get(sessionId)?.controller === controller) this.controls.delete(sessionId);
    });
    this.controls.set(sessionId, { controller, promise, generation });
    return promise;
  }

  restart(sessionId: string, generation: string) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation) return null;
    this.clearScheduled(connection);
    connection.controller.abort();
    this.cancelControl(sessionId);
    const next = { ...connection, observing: false, controller: new AbortController(), epoch: ++this.epoch, openedAt: Date.now(), lastActivityAt: Date.now() };
    this.connections.set(sessionId, next);
    return next;
  }

  scheduleReconnect(sessionId: string, generation: string, callback: () => void, minimumDelayMs = 0) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation || connection.controller.signal.aborted) return;
    const seconds = RECONNECT_DELAYS[Math.min(connection.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    connection.reconnectAttempt += 1;
    this.attempts.set(sessionId, { generation, count: connection.reconnectAttempt });
    const jitter = .8 + Math.random() * .4;
    this.schedule(connection, callback, Math.max(minimumDelayMs, seconds * 1000 * jitter));
  }

  schedule(connection: RunConnection, callback: () => void, delay: number) {
    if (!this.isCurrent(connection)) return;
    connection.wakeScheduled = true; // A scheduled wake marker, not a second timer.
    this.clockTask(connection.sessionId, 'wake', { at: Date.now() + delay, callback: () => {
      connection.wakeScheduled = undefined;
      if (this.isCurrent(connection)) callback();
    } });
  }

  private clearScheduled(connection: RunConnection) {
    connection.wakeScheduled = undefined;
    this.clockTask(connection.sessionId, 'wake');
  }

  /** Control deadlines, retry delays and stream watchdogs share one clock. */
  private clockTask(sessionId: string, kind: 'wake' | 'deadline', task?: ClockTask) {
    const slot = this.clocks.get(sessionId) || {};
    if (slot.timer != null) window.clearTimeout(slot.timer);
    slot[kind] = task;
    const tasks = [slot.wake, slot.deadline].filter((value): value is ClockTask => Boolean(value));
    if (!tasks.length) { this.clocks.delete(sessionId); return task; }
    this.clocks.set(sessionId, slot);
    slot.timer = window.setTimeout(() => {
      if (this.clocks.get(sessionId) !== slot) return;
      slot.timer = undefined;
      const ready: ClockTask[] = [];
      for (const key of ['deadline', 'wake'] as const) {
        const current = slot[key];
        if (current && current.at <= Date.now()) { ready.push(current); slot[key] = undefined; }
      }
      // Re-arm any remaining task before callbacks can schedule replacements.
      this.clockTask(sessionId, 'wake', slot.wake);
      for (const current of ready) current.callback();
    }, Math.max(0, Math.min(...tasks.map(value => value.at)) - Date.now()));
    return task;
  }

  abort(sessionId: string, pump = true) {
    this.cancelControl(sessionId);
    const connection = this.connections.get(sessionId);
    if (connection) {
      connection.controller.abort();
      this.clearScheduled(connection);
      this.connections.delete(sessionId);
    }
    this.detached.delete(sessionId);
    if (pump) this.pumpDetached();
  }

  release(sessionId: string, generation: string) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation) return;
    this.clearScheduled(connection);
    connection.controller.abort();
    this.cancelControl(sessionId);
    this.connections.delete(sessionId);
    this.pumpDetached();
  }

  abortAll() {
    for (const sessionId of this.controls.keys()) this.cancelControl(sessionId);
    for (const connection of this.connections.values()) {
      connection.controller.abort();
      this.clearScheduled(connection);
    }
    this.connections.clear();
    this.detached.clear();
    this.attempts.clear();
  }

  private pumpDetached() {
    const next = [...this.detached.values()].sort((left, right) => left.openedAt - right.openedAt)[0];
    if (!next || this.connections.size >= this.maxConnections) return;
    queueMicrotask(() => {
      if (this.detached.get(next.sessionId) !== next || this.connections.size >= this.maxConnections) return;
      this.detached.delete(next.sessionId);
      next.retry();
      this.pumpDetached();
    });
  }
}
