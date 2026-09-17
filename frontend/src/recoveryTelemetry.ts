/** Local diagnostic events: deliberately excludes identities, prompts and output. */
export function recordRecovery(kind: 'retry' | 'gap' | 'synchronized', detail: { attempt?: number; reason?: string; durationMs?: number } = {}) {
  window.dispatchEvent(new CustomEvent('igraph:recovery', { detail: { kind, ...detail } }));
}
