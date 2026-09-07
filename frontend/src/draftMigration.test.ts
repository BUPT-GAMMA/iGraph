import { beforeEach, expect, it } from 'vitest';
import { migrateGeneralDraft, readRecoveredDrafts } from './draftMigration';
import { conversationReducer, initialConversationState } from './sessionState';
import type { NewConversationDraft } from './types';

beforeEach(() => localStorage.clear());
const draft: NewConversationDraft = { draftId: 'old', projectId: 'general', text: '旧草稿', model: 'model', phase: 'editing', activeDatasetId: null, attachments: [] };
it('migrates an old General draft once and preserves conflicting edits for recovery', () => {
  localStorage.setItem('igraph-new-session-draft:v1:user:general', JSON.stringify(draft));
  localStorage.setItem('igraph-new-session-draft:v1:user:personal', JSON.stringify({ ...draft, draftId: 'new', projectId: null, text: '新编辑' }));
  migrateGeneralDraft('user', 'general'); migrateGeneralDraft('user', 'general');
  expect(readRecoveredDrafts('user')).toEqual([{ ...draft, projectId: null }]);
  expect(JSON.parse(localStorage.getItem('igraph-new-session-draft:v1:user:personal')!).text).toBe('新编辑');
  expect(localStorage.getItem('igraph-new-session-draft:v1:user:general')).toBeNull();
});
it('keeps personal and project drafts separate and ignores a late materialization when another draft is active', () => {
  let state = conversationReducer(initialConversationState, { type: 'OPEN_DRAFT', draft: { ...draft, projectId: null } });
  expect(state.activeDraftKey).toBe('personal');
  state = conversationReducer(state, { type: 'OPEN_DRAFT', draft: { ...draft, draftId: 'project', projectId: 'p' } });
  state = conversationReducer(state, { type: 'MATERIALIZE_DRAFT', projectId: null, draftId: 'old', sessionId: 's', generation: 'g', model: 'model', activeDatasetId: null });
  expect(state.activeDraftKey).toBe('p');
  expect(state.activeSessionId).toBeNull();
  expect(state.viewsBySessionId.s).toBeDefined();
  expect(state.draftsByScope.p.text).toBe('旧草稿');
});
