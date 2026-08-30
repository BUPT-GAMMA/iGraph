export type RunConnection = {
  sessionId: string;
  runId?: string;
  generation: string;
  controller: AbortController;
  reconnectAttempt: number;
  reconnectTimer?: number;
  openedAt: number;
  retry: () => void;
  onDetached: () => void;
};

type DetachedConnection = Omit<RunConnection, 'controller' | 'reconnectTimer'>;

const RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30];

export class RunConnectionManager {
  private connections = new Map<string, RunConnection>();
  private detached = new Map<string, DetachedConnection>();

  constructor(private readonly maxConnections = 4) {}

  get(sessionId: string) { return this.connections.get(sessionId); }

  acquire(options: {
    sessionId: string;
    runId?: string;
    generation: string;
    foreground: boolean;
    retry: () => void;
    onDetached: () => void;
  }): RunConnection | null {
    this.abort(options.sessionId, false);
    if (this.connections.size >= this.maxConnections) {
      const candidates = [...this.connections.values()].sort((left, right) => left.openedAt - right.openedAt);
      const displaced = options.foreground ? candidates[0] : undefined;
      if (!displaced) {
        this.detached.set(options.sessionId, {
          ...options,
          reconnectAttempt: 0,
          openedAt: Date.now(),
        });
        options.onDetached();
        return null;
      }
      this.connections.delete(displaced.sessionId);
      displaced.controller.abort();
      if (displaced.reconnectTimer) window.clearTimeout(displaced.reconnectTimer);
      this.detached.set(displaced.sessionId, {
        sessionId: displaced.sessionId,
        runId: displaced.runId,
        generation: displaced.generation,
        reconnectAttempt: displaced.reconnectAttempt,
        openedAt: displaced.openedAt,
        retry: displaced.retry,
        onDetached: displaced.onDetached,
      });
      displaced.onDetached();
    }
    const connection: RunConnection = {
      ...options,
      controller: new AbortController(),
      reconnectAttempt: 0,
      openedAt: Date.now(),
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
    if (connection?.generation === generation) connection.reconnectAttempt = 0;
  }

  restart(sessionId: string, generation: string) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation) return null;
    if (connection.reconnectTimer) window.clearTimeout(connection.reconnectTimer);
    connection.reconnectTimer = undefined;
    connection.controller = new AbortController();
    connection.openedAt = Date.now();
    return connection;
  }

  scheduleReconnect(sessionId: string, generation: string, callback: () => void) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation || connection.controller.signal.aborted) return;
    const seconds = RECONNECT_DELAYS[Math.min(connection.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    connection.reconnectAttempt += 1;
    const jitter = .8 + Math.random() * .4;
    connection.reconnectTimer = window.setTimeout(callback, seconds * 1000 * jitter);
  }

  abort(sessionId: string, pump = true) {
    const connection = this.connections.get(sessionId);
    if (connection) {
      connection.controller.abort();
      if (connection.reconnectTimer) window.clearTimeout(connection.reconnectTimer);
      this.connections.delete(sessionId);
    }
    this.detached.delete(sessionId);
    if (pump) this.pumpDetached();
  }

  release(sessionId: string, generation: string) {
    const connection = this.connections.get(sessionId);
    if (!connection || connection.generation !== generation) return;
    if (connection.reconnectTimer) window.clearTimeout(connection.reconnectTimer);
    this.connections.delete(sessionId);
    this.pumpDetached();
  }

  abortAll() {
    for (const connection of this.connections.values()) {
      connection.controller.abort();
      if (connection.reconnectTimer) window.clearTimeout(connection.reconnectTimer);
    }
    this.connections.clear();
    this.detached.clear();
  }

  private pumpDetached() {
    const next = [...this.detached.values()].sort((left, right) => left.openedAt - right.openedAt)[0];
    if (!next || this.connections.size >= this.maxConnections) return;
    this.detached.delete(next.sessionId);
    window.setTimeout(next.retry, 0);
  }
}
