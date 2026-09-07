import type { NewConversationDraft } from './types';

export const recoveredDraftsKey = (userId: string) => `igraph-recovered-drafts:v1:${userId}`;
export function readRecoveredDrafts(userId: string): NewConversationDraft[] {
  try { return JSON.parse(localStorage.getItem(recoveredDraftsKey(userId)) || '[]'); } catch { return []; }
}
export function migrateGeneralDraft(userId: string, legacyId: string) {
  const oldKey = `igraph-new-session-draft:v1:${userId}:${legacyId}`;
  const newKey = `igraph-new-session-draft:v1:${userId}:personal`;
  try {
    const raw = localStorage.getItem(oldKey);
    if (!raw) return;
    const draft = JSON.parse(raw) as NewConversationDraft;
    if (draft.projectId !== legacyId || typeof draft.draftId !== 'string') return;
    draft.projectId = null;
    const current = JSON.parse(localStorage.getItem(newKey) || 'null') as NewConversationDraft | null;
    if (!current) localStorage.setItem(newKey, JSON.stringify(draft));
    else if (current.draftId !== draft.draftId) {
      const recovered = readRecoveredDrafts(userId);
      if (!recovered.some(item => item.draftId === draft.draftId)) {
        localStorage.setItem(recoveredDraftsKey(userId), JSON.stringify([...recovered, draft]));
      }
    }
    // Remove only after the destination write succeeded.
    localStorage.removeItem(oldKey);
  } catch { /* A full or unavailable store must keep the original draft. */ }
}
