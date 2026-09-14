import { activateSession, ApiError, getRunControl, getRunSnapshot, resumeRun, streamChat, streamFeedback, StreamProtocolError } from './api';
import { applyBatch, createSessionView } from './sessionState';
import type { ConversationAction, SessionViewState } from './sessionState';
import type { ActiveRun, AttachmentRef, FeedbackRequest, StreamBatch } from './types';
import { RunConnectionManager } from './RunConnectionManager';
import type { RunConnection } from './RunConnectionManager';
import { recordRecovery } from './recoveryTelemetry';

type Options = {
  controlEnabled?: boolean;
  manager: RunConnectionManager;
  view: (sessionId: string) => SessionViewState | undefined;
  identity: () => string;
  paused: (sessionId: string) => boolean;
  foreground: (sessionId: string) => boolean;
  dispatch: (action: ConversationAction) => void;
  settled: (sessionId: string) => void;
  accepted: (sessionId: string) => void;
  synchronize?: (sessionId: string, runId: string) => void;
};

class RecoveryRequired extends Error {}
const terminal = (status?: string) => ['completed', 'failed', 'error', 'cancelled'].includes(status || '');
const fatal = (error: unknown) => error instanceof ApiError && [400, 401, 403, 404, 422].includes(error.status);

/** One owner for reconnect, snapshot polling, terminal checks and idle detection.
 * No retry path can POST a question or an approval decision.
 */
export class RunRecovery {
  private firstTurnEvidence = new Map<string, { generation: string; previousIds: Set<string>; message: string }>();
  private checks = new WeakMap<RunConnection, Promise<void>>();
  private terminalConnections = new WeakSet<RunConnection>();
  constructor(private options: Options) {}
  configure(options: Options) { this.options = options; }
  reset() { this.firstTurnEvidence.clear(); this.options.manager.abortAll(); }

  open(sessionId: string, generation: string, active: ActiveRun, foreground = this.options.foreground(sessionId)) {
    const o = this.options;
    if (!active.run_id || o.paused(sessionId) || o.manager.get(sessionId)) return;
    const retry = () => {
      const run = o.view(sessionId)?.activeRun;
      if (run?.runId && run.generation === generation) this.open(sessionId, generation, { run_id: run.runId, status: run.status, last_event_index: -1 });
    };
    const connection = o.manager.acquire({ sessionId, runId: active.run_id, generation, foreground, retry,
      onDetached: () => o.dispatch({ type: 'SET_RECEPTION', sessionId, generation, reception: 'capacity_suspended', synchronized: false }) });
    if (!connection) return;
    const run = o.view(sessionId)?.activeRun;
    if (run?.snapshotOnly || active.status.toLowerCase() === 'paused') void this.check(connection, true);
    else void this.consume(connection, 'resume');
  }

  async consume(connection: RunConnection, operation: 'start' | 'resume' | 'feedback',
    start?: { message: string; attachments: AttachmentRef[]; regenerateOf?: string; replaceRunId?: string }, feedback?: FeedbackRequest | null,
    decision?: 'approve' | 'reject', decisionId?: string) {
    const o = this.options;
    const { sessionId, generation } = connection;
    const identity = o.identity();
    const valid = () => this.valid(connection, identity);
    let shadow = o.view(sessionId);
    if (!shadow?.activeRun || shadow.activeRun.generation !== generation) {
      shadow = { ...createSessionView(sessionId), messagesById: shadow?.messagesById || {}, messageOrder: shadow?.messageOrder || [],
        activeRun: { generation, runId: connection.runId, status: 'starting',
        lastAppliedEventIndex: -1, toolsById: {}, toolOrder: [], plan: null, feedback: null } };
    }
    let runId = connection.runId || shadow.activeRun?.runId || '';
    if (operation === 'start' && start) this.firstTurnEvidence.set(sessionId, { generation,
      previousIds: new Set(shadow.messageOrder.filter(id => id !== `assistant:${generation}`)), message: start.message });
    let caughtUp = operation !== 'resume';
    let legacy = false;
    let sentAttachmentsCleared = false;
    let error: unknown;
    const dispatch = (action: ConversationAction) => { if (valid()) o.dispatch(action); };
    const cursor = shadow.activeRun!.lastAppliedEventIndex;
    if (operation === 'resume' && cursor === -1 && !shadow.activeRun?.snapshotOnly) {
      // Stage cold replay separately from the displayed persisted partial result.
      dispatch({ type: 'BEGIN_REPLAY', sessionId, generation });
      const ids = new Set([shadow.activeRun?.assistantMessageId, `assistant:${runId}`]);
      shadow = { ...shadow, messagesById: Object.fromEntries(Object.entries(shadow.messagesById).filter(([id]) => !ids.has(id))),
        messageOrder: shadow.messageOrder.filter(id => !ids.has(id)), activeRun: { ...shadow.activeRun!, recoveryRequired: false, replay: undefined } };
    }
    dispatch({ type: 'SET_RECEPTION', sessionId, generation, reception: 'connecting', synchronized: operation === 'start' });
    connection.observing = true;
    this.watch(connection, identity);
    const handlers = {
      onActivity: () => { if (valid()) o.manager.activity(connection); },
      onBatch: (incoming: StreamBatch) => {
        if (!valid()) return;
        const batch = { ...incoming, session_id: incoming.session_id || sessionId, run_id: incoming.run_id || runId };
        if (batch.session_id !== sessionId || (runId && batch.run_id && runId !== batch.run_id)) throw new RecoveryRequired('事件身份不匹配');
        runId = batch.run_id || runId;
        if (runId) o.manager.updateRunId(sessionId, generation, runId);
        if (runId) dispatch({ type: 'SET_RUN_ID', sessionId, generation, runId });
        legacy ||= batch.protocol_version === 1;
        if (batch.events.some(event => event.type === 'stream_recovery_required')) throw new RecoveryRequired('事件回放不连续');
        if (operation === 'resume' && legacy) throw new RecoveryRequired('V1 通过快照恢复');
        if (shadow!.activeRun?.snapshotOnly) {
          if (batch.events.some(event => event.type === 'feedback_accepted')) o.accepted(sessionId);
          throw new RecoveryRequired('继续同步完整快照');
        }
        const next = applyBatch(shadow!, generation, batch);
        dispatch({ type: 'APPLY_BATCH', sessionId, generation, batch });
        if (next.activeRun?.recoveryRequired) throw new RecoveryRequired('事件回放不连续');
        shadow = next;
        if (runId) o.accepted(sessionId);
        if (operation === 'start' && runId && start && !start.replaceRunId && !sentAttachmentsCleared) {
          sentAttachmentsCleared = true;
          const sent = new Set(start.attachments.map(item => item.file_token));
          dispatch({ type: 'PATCH_VIEW', sessionId, patch: { attachments: (o.view(sessionId)?.attachments || []).filter(item => !sent.has(item.file_token)) } });
        }
        if (batch.events.some(event => event.type === 'stream_status' && event.status === 'subscribed')) caughtUp = true;
        // Phase-boundary events need authoritative verification even on a live stream.
        if (terminal(shadow.activeRun?.status) || shadow.activeRun?.status === 'awaiting_feedback') {
          dispatch({ type: 'SET_RECEPTION', sessionId, generation, reception: 'snapshot_sync', synchronized: false });
          if (terminal(shadow.activeRun?.status) && o.controlEnabled && o.synchronize) {
            // React may not have committed APPLY_BATCH yet.
            this.terminalConnections.add(connection);
            void this.check(connection, true);
          }
        } else if (caughtUp) dispatch({ type: 'REPLAY_CAUGHT_UP', sessionId, generation });
        if (connection.wakeScheduled == null) this.watch(connection, identity);
      },
    };
    try {
      if (operation === 'start' && start) {
        if (start.replaceRunId) await streamChat(sessionId, start.message, start.attachments, handlers, connection.controller.signal, shadow.activeRun?.startRequestId || generation, undefined, start.replaceRunId);
        else if (start.regenerateOf) await streamChat(sessionId, start.message, start.attachments, handlers, connection.controller.signal, shadow.activeRun?.startRequestId || generation, start.regenerateOf);
        else await streamChat(sessionId, start.message, start.attachments, handlers, connection.controller.signal, shadow.activeRun?.startRequestId || generation);
      }
      else if (operation === 'feedback' && feedback && decision && decisionId) await streamFeedback(sessionId, feedback, decision, decisionId, handlers, connection.controller.signal);
      else if (runId) await resumeRun(sessionId, runId, cursor, handlers, connection.controller.signal);
    } catch (cause) { error = cause; }
    connection.observing = false;
    if (!valid()) return;
    if (o.manager.isCancelling(sessionId)) return;
    if (operation === 'start' && start && error instanceof ApiError && (error.status === 409 && (error.activeRun || start.replaceRunId) || [400, 422].includes(error.status))) {
      dispatch({ type: 'ROLLBACK_RUN_START', sessionId, generation, restoreDraft: start.message, restoreAttachments: start.attachments });
      if (error.activeRun) {
        dispatch({ type: 'ADOPT_RUN', sessionId, generation, activeRun: error.activeRun, feedback: error.pendingFeedback });
        o.manager.updateRunId(sessionId, generation, error.activeRun.run_id);
        this.retry(connection, error, true);
      } else {
        dispatch({ type: 'PATCH_VIEW', sessionId, patch: { error: error.message } });
        o.manager.release(sessionId, generation);
      }
      return;
    }
    if (operation === 'feedback' && decisionId && error instanceof ApiError && error.feedbackAccepted === false) {
      dispatch({ type: 'FEEDBACK_NOT_ACCEPTED', sessionId, generation, decisionId });
    }
    if (fatal(error)) return this.stop(connection, error);
    const needsSnapshot = error instanceof RecoveryRequired || error instanceof StreamProtocolError || (error instanceof ApiError && error.code === 'REPLAY_UNAVAILABLE') || legacy;
    if (needsSnapshot) {
      recordRecovery('gap', { reason: error instanceof ApiError && typeof (error.detail as any)?.reason === 'string'
        ? String((error.detail as any).reason).replace(/[^a-z_]/g, '').slice(0, 40) : legacy ? 'legacy_stream' : 'invalid_batch' });
      dispatch({ type: 'SET_RECEPTION', sessionId, generation, reception: 'snapshot_sync', synchronized: false, snapshotOnly: true });
    }
    const finished = !error && (terminal(shadow?.activeRun?.status) || shadow?.activeRun?.status === 'awaiting_feedback');
    if (finished) {
      // Verify the latest phase before opening approval or the composer. A
      // historical pause may already have continued on another client.
      await this.check(connection, true);
      return;
    }
    if (needsSnapshot) {
      await this.check(connection, true);
      return;
    }
    this.retry(connection, error, operation === 'feedback' || !runId);
  }

  private valid(connection: RunConnection, identity: string) {
    const o = this.options;
    if (o.identity() !== identity || !o.manager.isCurrent(connection) || o.paused(connection.sessionId)) return false;
    const run = o.view(connection.sessionId)?.activeRun;
    return !run || (run.generation === connection.generation && (!run.runId || !connection.runId || run.runId === connection.runId));
  }

  private stop(connection: RunConnection, error: unknown) {
    this.options.dispatch({ type: 'SET_RECEPTION', sessionId: connection.sessionId, generation: connection.generation,
      reception: 'idle', synchronized: false, error: error instanceof Error ? error.message : '恢复已停止，请核对会话权限' });
    this.options.manager.release(connection.sessionId, connection.generation);
  }

  private retry(connection: RunConnection, error?: unknown, checkFirst = false) {
    const o = this.options;
    const identity = o.identity();
    if (!this.valid(connection, identity)) return;
    o.dispatch({ type: 'SET_RECEPTION', sessionId: connection.sessionId, generation: connection.generation,
      reception: o.view(connection.sessionId)?.activeRun?.snapshotOnly ? 'snapshot_sync' : 'retrying', synchronized: false });
    o.manager.scheduleReconnect(connection.sessionId, connection.generation, () => {
      if (!this.valid(connection, identity)) return;
      const next = o.manager.restart(connection.sessionId, connection.generation);
      if (!next) return;
      if (checkFirst || !next.runId || o.view(next.sessionId)?.activeRun?.snapshotOnly) void this.check(next, true);
      else void this.consume(next, 'resume');
    }, error instanceof ApiError ? error.retryAfterMs : 0);
    recordRecovery('retry', { attempt: connection.reconnectAttempt });
  }

  check(connection: RunConnection, resumeAfter = false): Promise<void> {
    const pending = this.checks.get(connection);
    if (pending) return pending;
    const check = this.checkOnce(connection, resumeAfter).finally(() => {
      if (this.checks.get(connection) === check) this.checks.delete(connection);
    });
    this.checks.set(connection, check);
    return check;
  }

  private async checkOnce(connection: RunConnection, resumeAfter: boolean) {
    const o = this.options;
    const { sessionId, generation } = connection;
    const identity = o.identity();
    if (!this.valid(connection, identity)) return;
    if (o.manager.isCancelling(sessionId)) return;
    connection.lastCheckAt = Date.now();
    const startedAt = Date.now();
    try {
      if (!connection.runId) {
        const startId = o.view(sessionId)?.activeRun?.startRequestId;
        if (startId && o.controlEnabled) {
          const control = await o.manager.query(sessionId, generation, signal => getRunControl(sessionId, { start_request_id: startId }, signal));
          if (!this.valid(connection, identity)) return;
          if (!control.run_id || control.run_status === 'preparing') { this.retry(connection, undefined, true); return; }
          o.dispatch({ type: 'SET_RUN_ID', sessionId, generation, runId: control.run_id });
          o.manager.updateRunId(sessionId, generation, control.run_id);
          if (control.execution_stopped && control.terminal_persisted) o.dispatch({ type: 'APPLY_CONTROL', sessionId, generation, control });
          if (control.execution_stopped && control.terminal_persisted && control.native_run_created === false) {
            o.manager.abort(sessionId);
            o.settled(sessionId);
            return;
          }
          this.retry(connection, undefined, true);
          return;
        }
        const response = await o.manager.query(sessionId, generation, signal => activateSession(sessionId, signal));
        if (!this.valid(connection, identity)) return;
        const active = response.active_run;
        if (active) {
          o.dispatch({ type: 'ADOPT_RUN', sessionId, generation, activeRun: active, feedback: response.pending_feedback });
          o.manager.updateRunId(sessionId, generation, active.run_id);
          o.accepted(sessionId);
          this.retry(connection, undefined, true);
          return;
        }
        // A lost first response can belong to an already-completed run. The
        // session snapshot contains its stable message id even without active_run.
        const evidence = this.firstTurnEvidence.get(sessionId);
        const last = [...(response.messages || [])].reverse().find(message => {
          if (!message.id?.startsWith('assistant:') || evidence?.previousIds.has(message.id)) return false;
          const userMessage = response.messages?.find(item => item.id === message.id!.replace('assistant:', 'user:'));
          return evidence?.generation === generation && userMessage?.content === evidence.message;
        });
        if (last?.id) {
          const runId = last.id.slice('assistant:'.length);
          o.manager.updateRunId(sessionId, generation, runId);
          o.dispatch({ type: 'SET_RUN_ID', sessionId, generation, runId });
          this.retry(connection, undefined, true);
          return;
        }
        // No durable evidence yet: keep querying without resending the request.
        this.retry(connection, undefined, true);
        return;
      }
      const current = o.view(sessionId)?.activeRun;
      if (o.controlEnabled && o.synchronize && (this.terminalConnections.has(connection) || terminal(current?.status)) && !current?.snapshotOnly) {
        const control = await o.manager.query(sessionId, generation, signal => getRunControl(sessionId, { run_id: connection.runId! }, signal))
          .catch(error => { if (error instanceof ApiError && error.status === 404 && error.code === 'RUN_NOT_FOUND') return null; throw error; });
        if (!this.valid(connection, identity) || o.manager.isCancelling(sessionId)) return;
        // Runs created before control registration retain snapshot-based recovery.
        if (control && control.registration_status === 'registered') {
          if (control.run_id !== connection.runId) throw new RecoveryRequired('控制身份不匹配');
          if (control.revision < (o.view(sessionId)?.activeRun?.controlRevision || 0)) throw new RecoveryRequired('控制版本过期');
          o.dispatch({ type: 'APPLY_CONTROL', sessionId, generation, control });
          if (control.execution_stopped && control.terminal_persisted &&
            (control.can_send || (control.active_run && control.active_run.run_id !== connection.runId))) {
            o.accepted(sessionId);
            this.firstTurnEvidence.delete(sessionId);
            o.manager.release(sessionId, generation);
            if (control.can_send) o.settled(sessionId);
            else o.dispatch({ type: 'ADOPT_RUN', sessionId, generation: `resume:${control.active_run!.run_id}`, activeRun: control.active_run! });
            o.synchronize(sessionId, connection.runId!);
            return;
          }
          // Native completion may precede executor exit and its durable acknowledgement.
          // Recheck promptly using the existing timer; content/graph I/O is independent.
          o.manager.schedule(connection, () => void this.check(connection, true), Math.min(250 * 2 ** connection.reconnectAttempt++, 30000));
          return;
        }
      }
      const snapshot = await o.manager.query(sessionId, generation, signal => getRunSnapshot(sessionId, connection.runId!, signal));
      if (!this.valid(connection, identity)) return;
      if (snapshot.session_id !== sessionId || snapshot.run_id !== connection.runId) throw new RecoveryRequired('快照身份不匹配');
      if (snapshot.snapshot_complete) {
        const run = o.view(sessionId)?.activeRun;
        if (snapshot.status === 'paused' && run?.feedbackSubmission && run.feedbackSubmission.callId === snapshot.pending_feedback?.call_id) {
          this.retry(connection, undefined, true);
          return;
        }
        o.dispatch({ type: 'APPLY_SNAPSHOT', sessionId, generation, snapshot });
        recordRecovery('synchronized', { durationMs: Date.now() - startedAt });
        o.manager.receivedEvent(sessionId, generation);
        o.accepted(sessionId);
        this.firstTurnEvidence.delete(sessionId);
        o.manager.release(sessionId, generation);
        if (!snapshot.active_run) o.settled(sessionId);
        // A later active run is adopted by the committed-state effect.
        return;
      }
      if (resumeAfter) this.retry(connection);
      else this.watch(connection, identity);
    } catch (error) {
      if (!this.valid(connection, identity)) return;
      if (fatal(error)) this.stop(connection, error);
      else if (resumeAfter) this.retry(connection, error, true);
      else {
        // A failed terminal probe also uses the shared backoff (heartbeats do
        // not reset attempts). Keep the current observer while the probe retries.
        o.manager.scheduleReconnect(sessionId, generation, () => {
          if (Date.now() - connection.lastActivityAt >= 90000) {
            const next = o.manager.restart(sessionId, generation);
            if (next) this.retry(next, error, true);
          } else void this.check(connection);
        }, Math.max(30000, error instanceof ApiError ? error.retryAfterMs || 0 : 0));
      }
    }
  }

  private watch(connection: RunConnection, identity: string) {
    const o = this.options;
    if (!this.valid(connection, identity)) return;
    const now = Date.now();
    const due = Math.min(connection.lastActivityAt + 90000, Math.max(connection.lastProgressAt, connection.lastCheckAt) + 30000);
    o.manager.schedule(connection, () => {
      if (!this.valid(connection, identity)) return;
      if (Date.now() - connection.lastActivityAt >= 90000) {
        const next = o.manager.restart(connection.sessionId, connection.generation);
        if (next) this.retry(next);
      } else if (Date.now() - connection.lastProgressAt >= 30000) void this.check(connection);
      else this.watch(connection, identity);
    }, Math.max(1, due - now));
  }

  wake() {
    // The manager keeps the same timer and query slot for visibility/online
    // wakeups, so repeated browser events cannot multiply requests.
    for (const connection of this.options.manager.all()) {
      if (!this.options.paused(connection.sessionId) && Date.now() - connection.lastCheckAt >= 1000) void this.check(connection, !connection.observing);
    }
  }
}
