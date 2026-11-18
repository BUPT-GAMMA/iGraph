import { useEffect, useState } from 'react';
import { getConstructionQuestions, type ConstructionQuestions } from './api';
import { supportsConstruction } from './construction';
import type { Dataset } from './types';

export function useConstructionQuestions(dataset?: Dataset, savedRevision?: number, refresh = 0) {
  const construction = dataset?.metadata?.construction as { status?: string; revision?: number } | undefined;
  const revision = savedRevision ?? construction?.revision ?? 0;
  const ready = savedRevision !== undefined || dataset?.status === 'ready' && (!construction?.status || construction.status === 'ready');
  const datasetId = dataset && ready && revision > 0 && supportsConstruction(dataset) ? dataset.dataset_id : undefined;
  const projectId = dataset?.project_id ?? null;
  const key = datasetId && JSON.stringify([projectId, datasetId, revision]);
  const [loaded, setLoaded] = useState<{ key: string; questions: ConstructionQuestions }>();

  useEffect(() => {
    if (!datasetId || !key) return;
    const controller = new AbortController();
    const scope = { dataset_id: datasetId, project_id: projectId };
    const requestKey = key;
    const started = Date.now();
    let timer: number | undefined;
    let pending = false;
    async function poll() {
      try {
        const result = await getConstructionQuestions(scope, controller.signal);
        if (controller.signal.aborted || result.dataset_id !== datasetId || result.revision < revision) return;
        setLoaded({ key: requestKey, questions: result });
        pending = result.status === 'generating';
      } catch {
        // Keep a known generation pending through brief network interruptions.
      }
      if (pending && !controller.signal.aborted && Date.now() - started < 120_000) {
        timer = window.setTimeout(poll, 2_000);
      }
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [datasetId, projectId, revision, key, refresh]);

  return key && loaded?.key === key ? loaded.questions : undefined;
}
