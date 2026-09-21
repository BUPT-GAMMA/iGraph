import type { SessionViewState, RunStatus } from './sessionState';

export type SavedSessionActivity = {
  sessionId: string;
  runId?: string;
  startRequestId?: string;
  status?: RunStatus;
  completedRunId?: string;
  readRunId?: string;
};

const prefix = (userId: string) => `igraph-session-activity:v1:${encodeURIComponent(userId)}:`;
export const activityKey = (userId: string, sessionId: string) => prefix(userId) + encodeURIComponent(sessionId);
export const pendingActivity = (value: SavedSessionActivity) => Boolean((value.runId || value.startRequestId)
  && ['starting', 'streaming', 'awaiting_feedback', 'submitting_feedback'].includes(value.status || ''));

export function readSessionActivities(userId: string): SavedSessionActivity[] {
  if (!userId) return [];
  const entries: SavedSessionActivity[] = [];
  try {
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index);
      if (!key?.startsWith(prefix(userId))) continue;
      try {
        const raw = JSON.parse(localStorage.getItem(key) || 'null');
        const sessionId = decodeURIComponent(key.slice(prefix(userId).length));
        if (!raw || raw.sessionId !== sessionId || !sessionId) continue;
        const entry: SavedSessionActivity = { sessionId };
        for (const field of ['runId', 'startRequestId', 'completedRunId', 'readRunId'] as const) {
          if (typeof raw[field] === 'string' && raw[field].length <= 256) entry[field] = raw[field];
        }
        if (['starting', 'streaming', 'awaiting_feedback', 'submitting_feedback', 'completed', 'failed', 'cancelled'].includes(raw.status)) entry.status = raw.status;
        if (pendingActivity(entry) || entry.completedRunId) entries.push(entry);
      } catch { /* Ignore a corrupt entry without hiding other sessions. */ }
    }
  } catch { /* Storage can be unavailable; live state remains usable. */ }
  // Upgrade the existing per-tab run keys so an in-flight background response
  // survives the first refresh after this feature is deployed as well.
  try {
    const legacyPrefix = `igraph-active-run:v2:${userId}:`;
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index);
      if (!key?.startsWith(legacyPrefix)) continue;
      try {
        const sessionId = key.slice(legacyPrefix.length);
        if (!sessionId || entries.some(item => item.sessionId === sessionId)) continue;
        const raw = JSON.parse(sessionStorage.getItem(key) || 'null');
        const runId = typeof raw?.runId === 'string' ? raw.runId : undefined;
        const startRequestId = typeof raw?.startRequestId === 'string' ? raw.startRequestId : undefined;
        if (runId || startRequestId) entries.push({ sessionId, runId, startRequestId, status: 'starting' });
      } catch { /* Ignore malformed legacy records. */ }
    }
  } catch { /* Per-tab storage is optional too. */ }
  return entries;
}

export function saveSessionActivity(userId: string, view: SessionViewState) {
  if (!userId) return;
  const run = view.activeRun;
  // Preparation before sending is not a durable server run.
  if (run?.status === 'preparing') return;
  const entry: SavedSessionActivity = { sessionId: view.sessionId,
    runId: run?.runId, startRequestId: run?.startRequestId, status: run?.status,
    completedRunId: view.lastCompletedRunId, readRunId: view.lastReadRunId };
  try {
    const key = activityKey(userId, view.sessionId);
    if (!pendingActivity(entry) && !entry.completedRunId) {
      // An unloaded shell must not erase a run before hydration/restoration.
      if (view.hydration === 'ready') localStorage.removeItem(key);
      return;
    }
    const value = JSON.stringify(entry);
    if (localStorage.getItem(key) !== value) localStorage.setItem(key, value);
  } catch { /* Persistence is optional; no reply text is stored here. */ }
}

export function forgetSessionActivity(userId: string, sessionId: string) {
  try { localStorage.removeItem(activityKey(userId, sessionId)); } catch { /* optional */ }
}
