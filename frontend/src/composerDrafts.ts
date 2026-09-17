import type { AttachmentRef, DraftAttachment } from './types';
import { tr } from './i18n';

export type ComposerDraft = { draft: string; attachments: AttachmentRef[]; pendingAttachments: DraftAttachment[]; preserveDuringRun?: boolean };
export const composerStorageKey = (userId: string, sessionId: string) => `igraph-composer:v1:${encodeURIComponent(userId)}:${encodeURIComponent(sessionId)}`;

export function clearComposerDraft(userId: string, sessionId: string) {
  try { localStorage.removeItem(composerStorageKey(userId, sessionId)); } catch { /* Storage is optional. */ }
}

export function writeComposerDraft(userId: string, sessionId: string, value: ComposerDraft) {
  if (!userId) return;
  if (!value.draft && !value.attachments.length && !value.pendingAttachments.length) {
    clearComposerDraft(userId, sessionId);
    return;
  }
  try {
    localStorage.setItem(composerStorageKey(userId, sessionId), JSON.stringify({
      draft: value.draft, attachments: value.attachments,
      ...(value.preserveDuringRun ? { preserveDuringRun: true } : {}),
      pendingAttachments: value.pendingAttachments.map(item => ({
        localId: item.localId, filename: item.filename, status: 'needs_reselect', error: tr('Select the file again after refreshing', '刷新后需要重新选择文件'),
      })),
    }));
  } catch { /* Quota or privacy settings must not interrupt editing. */ }
}

export function readComposerDraft(userId: string, sessionId: string): ComposerDraft | null {
  try {
    const value = JSON.parse(localStorage.getItem(composerStorageKey(userId, sessionId)) || 'null');
    if (!value || typeof value.draft !== 'string' || !Array.isArray(value.attachments) || !Array.isArray(value.pendingAttachments)) return null;
    return {
      draft: value.draft,
      ...(value.preserveDuringRun === true ? { preserveDuringRun: true } : {}),
      attachments: value.attachments.filter((item: any) => typeof item?.file_token === 'string' && typeof item?.filename === 'string')
        .map((item: AttachmentRef) => ({ file_token: item.file_token, filename: item.filename })),
      pendingAttachments: value.pendingAttachments.filter((item: any) => typeof item?.localId === 'string' && typeof item?.filename === 'string')
        .map((item: DraftAttachment) => ({ localId: item.localId, filename: item.filename, status: 'needs_reselect', error: tr('Select the file again after refreshing', '刷新后需要重新选择文件') })),
    };
  } catch { return null; }
}
