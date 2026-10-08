import type { Dataset } from './types';
export type UserCase = { id: string; name: string; filename: string; description: string; questions: string[]; available: boolean };
export function userCaseForDataset(dataset: Dataset | undefined, cases: UserCase[]) {
  if (!dataset) return undefined;
  return cases.find(item => item.id === dataset.metadata?.user_case_id || item.filename === dataset.original_filename || item.filename === dataset.metadata?.source_relative_path);
}
