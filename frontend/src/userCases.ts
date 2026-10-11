import type { Dataset, Message } from './types';
import { editableLatestTurn } from './latestTurn';
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

export type ExampleQuestionSuggestion = { question: string; number: number; total: number };

export function nextExampleQuestion(example: UserCase | undefined, messages: Message[], t: Translate): ExampleQuestionSuggestion | undefined {
  if (!example) return undefined;
  const turn = editableLatestTurn(messages);
  if (!turn || turn.assistant.run_status !== 'completed') return undefined;
  // Match either language so a UI language change never resets the sequence.
  const normalize = (text: string) => text.normalize('NFKC').trim().replace(/\s+/g, ' ').toLocaleLowerCase();
  const prompt = normalize(turn.user.content);
  const index = example.questions.findIndex((question, number) =>
    normalize(question) === prompt || (example.translations?.en?.questions[number]
      && normalize(example.translations.en.questions[number]) === prompt));
  if (index < 0 || index + 1 >= example.questions.length) return undefined;
  return { question: localizeUserCase(example, t).questions[index + 1], number: index + 2, total: example.questions.length };
}
