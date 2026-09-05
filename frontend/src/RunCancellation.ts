import { ApiError, cancelJob, cancelRun, getJobControl, getRunControl, getRunSnapshot } from './api';
import type { RunControl, RunTarget } from './api';
import type { ConversationAction, SessionViewState } from './sessionState';
import { RunConnectionManager } from './RunConnectionManager';

type Intent = { identity: string; sessionId: string; generation: string; cancelRequestId: string; target: RunTarget;
  attempt: number; revision: number; requestVersion: number; busy: boolean; detached?: boolean; requiresManualRetry?: boolean; queryOnly?: boolean };
type Options = { manager: RunConnectionManager; identity: () => string; view: (sid: string) => SessionViewState | undefined;
  dispatch: (action: ConversationAction) => void; settled: (sid: string, control: RunControl) => void };
const delays = [1000, 2000, 4000, 8000, 15000, 30000];
const key = (uid: string, sid: string) => `igraph-cancel:v1:${uid}:${sid}`;
const jobKey = (uid: string, sid: string) => `igraph-job-cancel:v1:${uid}:${sid}`;

/** Cancellation has its own attempts but shares the session's query/timer lane.
 * The observer may continue receiving partial output while stop is confirmed.
 */
export class RunCancellation {
  private intents = new Map<string, Intent>();
  private historical = new Map<string, Intent>();
  private syncs = new Map<string, { sid: string; rid: string; identity: string; epoch: number; attempt: number }>();
  private jobs = new Map<string, { sid: string; jid: string; identity: string; requestId: string; attempt: number; busy: boolean; notify: (status: string) => void }>();
  private epoch = 0;
  constructor(private options: Options) {}
  configure(options: Options) { this.options = options; }
  pending(sid: string) { return this.intents.has(sid); }

  observe(sid: string) {
    const o = this.options, run = o.view(sid)?.activeRun;
    if (this.intents.has(sid) || !run?.runId || run.cancellation === 'confirmed') return;
    if (!run.cancellation && !(run.serverCanSend === false && ['completed', 'cancelled', 'failed'].includes(run.status))) return;
    const intent: Intent = { identity: o.identity(), sessionId: sid, generation: run.generation,
      cancelRequestId: crypto.randomUUID(), target: { run_id: run.runId }, queryOnly: true,
      attempt: 0, revision: run.controlRevision || 0, requestVersion: 0, busy: false };
    this.intents.set(sid, intent);
    o.manager.prioritizeCancellation(sid);
    void this.step(intent, true);
  }

  request(sid: string) {
    const existing = this.intents.get(sid);
    const o = this.options;
    const run = o.view(sid)?.activeRun;
    if (!run || (!run.runId && !run.startRequestId)) return;
    if (existing && !existing.detached && existing.generation === run.generation) {
      existing.queryOnly = false;
      this.persistIntents(sid);
      existing.requiresManualRetry = false;
      if (!existing.busy) void this.step(existing, false);
      return;
    }
    if (existing) {
      existing.detached = true;
      this.historical.set(existing.cancelRequestId, existing);
      this.intents.delete(sid);
    }
    const intent: Intent = { identity: o.identity(), sessionId: sid, generation: run.generation,
      cancelRequestId: run.cancelRequestId || crypto.randomUUID(), target: run.runId ? { run_id: run.runId } : { start_request_id: run.startRequestId! },
      attempt: 0, revision: 0, requestVersion: 0, busy: false };
    this.install(intent);
    void this.step(intent, false);
  }

  restore(sid: string) {
    this.restoreJobs(sid);
    if (this.intents.has(sid)) return;
    const o = this.options;
    try {
      const payload = JSON.parse(localStorage.getItem(key(o.identity(), sid)) || 'null');
      for (const saved of Array.isArray(payload) ? payload : [payload]) {
        if (!saved || saved.identity !== o.identity() || saved.sessionId !== sid || typeof saved.cancelRequestId !== 'string') continue;
        if (this.historical.has(saved.cancelRequestId)) continue;
        if (!saved.target || (typeof saved.target.run_id === 'string') === (typeof saved.target.start_request_id === 'string')) continue;
        const run = o.view(sid)?.activeRun;
        const detached = !run || (saved.target.run_id ? run.runId !== saved.target.run_id : run.startRequestId !== saved.target.start_request_id);
        const intent = { ...saved, generation: run?.generation || saved.generation, detached, attempt: 0, revision: 0, requestVersion: 0, busy: false } as Intent;
        this.install(intent);
        void this.step(intent, true);
      }
    } catch { /* Optional persistence never blocks an in-page stop. */ }
  }

  private install(intent: Intent) {
    const o = this.options;
    if (intent.detached) this.historical.set(intent.cancelRequestId, intent);
    else this.intents.set(intent.sessionId, intent);
    if (!intent.detached) {
      o.manager.prioritizeCancellation(intent.sessionId);
      o.dispatch({ type: 'REQUEST_CANCEL', sessionId: intent.sessionId, generation: intent.generation,
        cancelRequestId: intent.cancelRequestId, runId: intent.target.run_id, startRequestId: intent.target.start_request_id });
    }
    this.persistIntents(intent.sessionId);
  }

  private persistIntents(sid: string) {
    try {
      const values = [...this.intents.values(), ...this.historical.values()].filter(intent => intent.sessionId === sid && !intent.queryOnly)
        .map(({ identity, sessionId, generation, cancelRequestId, target }) => ({ identity, sessionId, generation, cancelRequestId, target }));
      if (values.length) localStorage.setItem(key(this.options.identity(), sid), JSON.stringify(values));
      else localStorage.removeItem(key(this.options.identity(), sid));
    } catch { /* optional */ }
  }

  private valid(intent: Intent) {
    const view = this.options.view(intent.sessionId);
    const run = view?.activeRun;
    return !!view && (this.intents.get(intent.sessionId) === intent || this.historical.get(intent.cancelRequestId) === intent) && this.options.identity() === intent.identity
      && (intent.detached || !run || run.generation === intent.generation);
  }

  private async step(intent: Intent, queryFirst: boolean) {
    if (!this.valid(intent) || intent.busy) return;
    const o = this.options;
    const version = ++intent.requestVersion;
    intent.busy = true;
    let minimumDelay = 0;
    let stopped = false;
    try {
      if (intent.detached && o.manager.hasControl(intent.sessionId)) return;
      let control = await o.manager.query(intent.sessionId, `cancel:${intent.cancelRequestId}:${version}`, signal =>
        queryFirst || intent.queryOnly ? getRunControl(intent.sessionId, intent.target, signal) : cancelRun(intent.sessionId, intent.target, intent.cancelRequestId, signal), 5000, true);
      if (!this.valid(intent) || version !== intent.requestVersion) return;
      if (!intent.queryOnly && queryFirst && (control.registration_status === 'not_seen' || control.cancel_status === 'none')) {
        control = await o.manager.query(intent.sessionId, `cancel:${intent.cancelRequestId}:${version}:post`,
          signal => cancelRun(intent.sessionId, intent.target, intent.cancelRequestId, signal), 5000, true);
      }
      if (!this.valid(intent) || version !== intent.requestVersion || control.revision < intent.revision) return;
      intent.revision = control.revision;
      const currentRun = o.view(intent.sessionId)?.activeRun;
      if (intent.detached && currentRun?.runId === control.run_id && !this.intents.has(intent.sessionId)) {
        intent.detached = false;
        this.historical.delete(intent.cancelRequestId);
        this.intents.set(intent.sessionId, intent);
        intent.generation = currentRun.generation;
        o.manager.prioritizeCancellation(intent.sessionId);
        o.dispatch({ type: 'REQUEST_CANCEL', sessionId: intent.sessionId, generation: intent.generation, cancelRequestId: intent.cancelRequestId });
      }
      if (!intent.detached) o.dispatch({ type: 'APPLY_CONTROL', sessionId: intent.sessionId, generation: intent.generation, control });
      if (control.registration_status === 'registered' && control.execution_stopped && control.terminal_persisted) {
        stopped = true;
        if (this.intents.get(intent.sessionId) === intent) this.intents.delete(intent.sessionId);
        this.historical.delete(intent.cancelRequestId);
        if (!intent.detached) {
          o.manager.finishCancellation(intent.sessionId);
          o.manager.abort(intent.sessionId);
        }
        this.persistIntents(intent.sessionId);
        if (!intent.detached) o.settled(intent.sessionId, control);
        // Server admission is already authoritative. Content synchronization is
        // independently scoped and cannot clear a newer run or its graph.
        if (control.run_id && control.native_run_created !== false) {
          this.syncs.set(control.run_id, { sid: intent.sessionId, rid: control.run_id, identity: intent.identity, epoch: this.epoch, attempt: 0 });
          void this.syncResult(control.run_id);
        }
        return;
      }
    } catch (cause) {
      if (!this.valid(intent)) return;
      if (!intent.detached) o.dispatch({ type: 'CANCEL_UNCONFIRMED', sessionId: intent.sessionId, generation: intent.generation });
      if (cause instanceof ApiError) {
        minimumDelay = cause.retryAfterMs || 0;
        if ([400, 401, 403, 404, 409, 422].includes(cause.status)) {
          stopped = true;
          intent.requiresManualRetry = true;
          o.dispatch({ type: 'PATCH_VIEW', sessionId: intent.sessionId, patch: { error: cause.message } });
          // Intent remains available for a deliberate retry after correction.
        }
      }
    } finally {
      intent.busy = false;
      if (!stopped && this.valid(intent)) {
        const delay = delays[Math.min(intent.attempt++, delays.length - 1)] * (.8 + Math.random() * .4);
        if (intent.detached) o.manager.scheduleAuxiliary(intent.sessionId, `cancel:${intent.cancelRequestId}`, () => void this.step(intent, true), Math.max(delay, minimumDelay));
        else o.manager.scheduleControl(intent.sessionId, () => void this.step(intent, true), Math.max(delay, minimumDelay));
      } else if (!this.valid(intent) && this.intents.get(intent.sessionId) === intent) {
        this.intents.delete(intent.sessionId);
        o.manager.finishCancellation(intent.sessionId);
        if (o.identity() === intent.identity && o.view(intent.sessionId)) {
          intent.detached = true;
          this.historical.set(intent.cancelRequestId, intent);
          this.persistIntents(intent.sessionId);
          o.manager.scheduleAuxiliary(intent.sessionId, `cancel:${intent.cancelRequestId}`, () => void this.step(intent, true), 1000);
        }
      }
    }
  }

  private async syncResult(rid: string) {
    const sync = this.syncs.get(rid);
    if (!sync) return;
    const { sid, identity, epoch } = sync;
    const o = this.options;
    if (epoch !== this.epoch || o.identity() !== identity || !o.view(sid)) { this.syncs.delete(rid); return; }
    try {
      if (o.manager.hasControl(sid) || o.manager.isCancelling(sid)) return;
      const snapshot = await o.manager.query(sid, `cancel-snapshot:${rid}`, signal => getRunSnapshot(sid, rid, signal));
      if (epoch === this.epoch && o.identity() === identity && o.view(sid)) {
        o.dispatch({ type: 'MERGE_RUN_SNAPSHOT', sessionId: sid, snapshot });
        if (snapshot.snapshot_complete) this.syncs.delete(rid);
      }
    } catch (cause) {
      if (cause instanceof ApiError && [401, 403, 404, 422].includes(cause.status)) this.syncs.delete(rid);
    } finally {
      if (this.syncs.get(rid) === sync) o.manager.scheduleAuxiliary(sid, `snapshot:${rid}`, () => void this.syncResult(rid),
        delays[Math.min(sync.attempt++, delays.length - 1)] * (.8 + Math.random() * .4));
    }
  }

  requestJob(sid: string, jid: string, notify: (status: string) => void) {
    let job = this.jobs.get(jid);
    if (!job) {
      job = { sid, jid, identity: this.options.identity(), requestId: crypto.randomUUID(), attempt: 0, busy: false, notify };
      this.jobs.set(jid, job);
    }
    job.notify = notify;
    this.persistJobs(sid);
    if (!this.options.manager.isCancelling(sid)) this.options.manager.cancelControl(sid);
    void this.stopJob(jid);
  }

  private persistJobs(sid: string) {
    try {
      const values = [...this.jobs.values()].filter(job => job.sid === sid).map(({ sid, jid, identity, requestId }) => ({ sid, jid, identity, requestId }));
      if (values.length) localStorage.setItem(jobKey(this.options.identity(), sid), JSON.stringify(values));
      else localStorage.removeItem(jobKey(this.options.identity(), sid));
    } catch { /* Optional persistence never blocks a stop. */ }
  }

  private restoreJobs(sid: string) {
    try {
      const values = JSON.parse(localStorage.getItem(jobKey(this.options.identity(), sid)) || '[]');
      if (!Array.isArray(values)) return;
      for (const saved of values) {
        if (saved.sid !== sid || saved.identity !== this.options.identity() || typeof saved.jid !== 'string' || typeof saved.requestId !== 'string' || this.jobs.has(saved.jid)) continue;
        this.jobs.set(saved.jid, { ...saved, attempt: 0, busy: false, notify: () => {} });
        void this.stopJob(saved.jid, true);
      }
    } catch { /* optional */ }
  }

  private async stopJob(jid: string, queryFirst = false) {
    const job = this.jobs.get(jid);
    const o = this.options;
    if (!job || job.busy || job.identity !== o.identity()) return;
    let finished = false;
    let minimumDelay = 0;
    try {
      if (o.manager.hasControl(job.sid) || o.manager.isCancelling(job.sid)) return;
      job.busy = true;
      job.notify('stopping');
      let response = await o.manager.query(job.sid, `job-cancel:${job.requestId}`, signal => queryFirst
        ? getJobControl(job.sid, jid, signal) : cancelJob(job.sid, jid, job.requestId, signal), 5000);
      if (this.jobs.get(jid) !== job || job.identity !== o.identity()) return;
      if (queryFirst && response.cancel_status === 'none' && !response.execution_stopped) {
        response = await o.manager.query(job.sid, `job-cancel:${job.requestId}:post`, signal => cancelJob(job.sid, jid, job.requestId, signal), 5000);
      }
      if (this.jobs.get(jid) !== job || job.identity !== o.identity()) return;
      if (response.execution_stopped && response.terminal_persisted) { finished = true; job.notify('confirmed'); this.jobs.delete(jid); this.persistJobs(job.sid); }
    } catch (cause) {
      if (this.jobs.get(jid) !== job || job.identity !== o.identity()) return;
      job.notify('unconfirmed');
      if (cause instanceof ApiError) {
        minimumDelay = cause.retryAfterMs || 0;
        finished = [400, 401, 403, 404, 409, 422].includes(cause.status);
      }
    } finally {
      job.busy = false;
      if (!finished && this.jobs.get(jid) === job) o.manager.scheduleAuxiliary(job.sid, `job:${jid}`, () => void this.stopJob(jid),
        Math.max(minimumDelay, delays[Math.min(job.attempt++, delays.length - 1)] * (.8 + Math.random() * .4)));
    }
  }

  wake() { for (const intent of [...this.intents.values(), ...this.historical.values()]) if (!intent.busy && !intent.requiresManualRetry) void this.step(intent, true); }
  forget(sid: string) {
    this.intents.delete(sid);
    for (const [id, intent] of this.historical) if (intent.sessionId === sid) this.historical.delete(id);
    for (const [rid, sync] of this.syncs) if (sync.sid === sid) this.syncs.delete(rid);
    for (const [jid, job] of this.jobs) if (job.sid === sid) this.jobs.delete(jid);
    this.options.manager.forget(sid);
    try { localStorage.removeItem(key(this.options.identity(), sid)); localStorage.removeItem(jobKey(this.options.identity(), sid)); } catch { /* optional */ }
  }
  reset() { this.epoch++; this.intents.clear(); this.historical.clear(); this.syncs.clear(); this.jobs.clear(); this.options.manager.abortAll(); }
}
