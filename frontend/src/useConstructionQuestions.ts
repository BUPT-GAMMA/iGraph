import { useEffect, useState } from 'react';
import { getConstructionQuestions } from './api';
import { supportsConstruction } from './construction';
import type { Dataset } from './types';
import type { ExampleQuestions } from './userCases';

export function useConstructionQuestions(dataset?: Dataset) {
  const construction = dataset?.metadata?.construction as { status?: string; revision?: number } | undefined;
  const revision = construction?.revision ?? 0;
  const datasetId = dataset && dataset.status === 'ready' && supportsConstruction(dataset) &&
    revision > 0 && (!construction?.status || construction.status === 'ready') ? dataset.dataset_id : undefined;
  const projectId = dataset?.project_id ?? null;
  const key = datasetId && JSON.stringify([projectId, datasetId, revision]);
  const [loaded, setLoaded] = useState<{ key: string; questions: ExampleQuestions }>();

  useEffect(() => {
    if (!datasetId || !key) return;
    const controller = new AbortController();
    getConstructionQuestions({ dataset_id: datasetId, project_id: projectId }, controller.signal).then(result => {
      if (!controller.signal.aborted && result.dataset_id === datasetId && result.revision >= revision) {
        setLoaded({ key, questions: result });
      }
    }).catch(() => { /* Suggestions are optional; the composer remains usable. */ });
    return () => controller.abort();
  }, [datasetId, projectId, revision, key]);

  return key && loaded?.key === key ? loaded.questions : undefined;
}
