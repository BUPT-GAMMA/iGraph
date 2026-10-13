import type { Dataset, Message } from './types';
import { editableLatestTurn } from './latestTurn';
import { createContext, useContext } from 'react';
import { useI18n, type Translate } from './i18n';
export type BorylationAnalysisStep = 'properties' | 'rings' | 'structure' | 'symmetry' | 'products' | 'conditions' | 'plan';
export type BorylationStoryStep = { id: string; title: string; analysis_step: BorylationAnalysisStep; question: string };
type CaseText = { name: string; description: string; questions: string[]; question_titles?: string[]; story_steps?: BorylationStoryStep[] };
export type UserCase = CaseText & { id: string; filename: string; available: boolean; translations?: { en?: CaseText } };
export const UserCaseContext = createContext<UserCase[]>([]);
const INPUT_LABELS: Record<string, [string, string]> = {
  '金融风控（CSV 原始三表）': ['Financial risk (three source CSV tables)', '金融风控（CSV 原始三表）'],
  'StackExchange（SQL 原始数据）': ['StackExchange (SQL source)', 'StackExchange（SQL 原始数据）'],
  'arxiv（PT 图）': ['arxiv (PT graph)', 'arxiv（PT 图）'],
  '机构、个人与持股关系三张原始 CSV，已配置主外键，可体验构图、检查并手动保存。': ['Three original CSV tables with configured keys. Build, inspect, and save your own graph.', '机构、个人与持股关系三张原始 CSV，已配置主外键，可体验构图、检查并手动保存。'],
  'StackExchange SQL 数据源，可体验 SQL 解析、主外键构图和手动保存。': ['A StackExchange SQL source for parsing, key-based graph construction, and manual saving.', 'StackExchange SQL 数据源，可体验 SQL 解析、主外键构图和手动保存。'],
  'arxiv 引文图，包含节点特征、论文文本、分类标签与数据划分，可直接载入分析。': ['The arxiv citation graph includes node features, paper text, labels, and splits, ready for analysis.', 'arxiv 引文图，包含节点特征、论文文本、分类标签与数据划分，可直接载入分析。'],
};
export function localizeInputLabel(text: string, t: Translate) {
  const value = INPUT_LABELS[text] || Object.values(INPUT_LABELS).find(pair => pair[0] === text);
  return value ? t(value[0], value[1]) : text;
}
export function useExampleLabels() {
  const cases = useContext(UserCaseContext);
  const { t } = useI18n();
  return {
    name: (name: string) => localizeInputLabel(localizeExampleName(name, cases, t) || name, t),
    description: (description: string) => {
      const item = cases.find(example => example.description === description || example.translations?.en?.description === description);
      return item ? localizeUserCase(item, t).description : localizeInputLabel(description, t);
    },
  };
}
export function localizeUserCase(item: UserCase, t: Translate): UserCase {
  const en = item.translations?.en;
  return en ? { ...item, name: t(en.name, item.name), description: t(en.description, item.description),
    questions: item.questions.map((question, index) => t(en.questions[index] || question, question)),
    question_titles: item.questions.map((question, index) => t(en.question_titles?.[index] || en.questions[index] || question, item.question_titles?.[index] || question)),
    story_steps: item.story_steps?.map(step => {
      const translated = en.story_steps?.find(value => value.id === step.id);
      return translated ? { ...step, title: t(translated.title, step.title), question: t(translated.question, step.question) } : step;
    }) } : item;
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
