import type { Dataset } from './types';
import { createContext, useContext } from 'react';
import { useI18n, type Translate } from './i18n';
type CaseText = { name: string; description: string; questions: string[] };
export type UserCase = CaseText & { id: string; filename: string; available: boolean; translations?: { en?: CaseText } };
export const UserCaseContext = createContext<UserCase[]>([]);
export function useExampleLabels() {
  const cases = useContext(UserCaseContext);
  const { t } = useI18n();
  return {
    name: (name: string) => localizeExampleName(name, cases, t) || name,
    description: (description: string) => {
      const item = cases.find(example => example.description === description || example.translations?.en?.description === description);
      return item ? localizeUserCase(item, t).description : description;
    },
  };
}
export function localizeUserCase(item: UserCase, t: Translate): UserCase {
  const en = item.translations?.en;
  return en ? { ...item, name: t(en.name, item.name), description: t(en.description, item.description),
    questions: item.questions.map((question, index) => t(en.questions[index] || question, question)) } : item;
}
export function localizeExampleName(name: string | undefined, cases: UserCase[], t: Translate) {
  if (!name) return name;
  const item = cases.find(example => example.name === name || example.translations?.en?.name === name);
  return item ? localizeUserCase(item, t).name : name;
}
export function userCaseForDataset(dataset: Dataset | undefined, cases: UserCase[]) {
  if (!dataset) return undefined;
  return cases.find(item => item.id === dataset.metadata?.user_case_id || item.filename === dataset.original_filename || item.filename === dataset.metadata?.source_relative_path);
}
