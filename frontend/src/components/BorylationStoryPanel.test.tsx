import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getBorylationCase, type BorylationCaseResponse } from '../api';
import { nextExampleQuestion, type BorylationAnalysisStep, type UserCase } from '../userCases';
import { I18nProvider, useI18n } from '../i18n';
import { ChatPane } from './ChatPane';
import { BorylationStoryPanel } from './BorylationStoryPanel';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'), getBorylationCase: vi.fn() }));
const steps: Array<[string, string, BorylationAnalysisStep]> = [
  ['structure', '从底物开始', 'structure'], ['symmetry', '排除等价位点', 'symmetry'], ['products', '看看已有产物', 'products'],
  ['condition_screen', '反应有多难做', 'conditions'], ['condition_comparison', '比较成功条件', 'conditions'], ['experiment_plan', '安排下一轮实验', 'plan'],
];
const userCase: UserCase = { id: 'borylation', name: '分子硼化反应示例', filename: 'borylation-story.npz', description: '', available: true,
  questions: steps.map(step => `问题：${step[1]}`), story_steps: steps.map(([id, title, analysis_step]) => ({ id, title, analysis_step, question: `问题：${title}` })) };
const response = (step: BorylationAnalysisStep, result: Record<string, unknown>): BorylationCaseResponse => ({ success: true, step, result, prediction_status: 'not_run' });

afterEach(cleanup);
beforeEach(() => { vi.mocked(getBorylationCase).mockReset(); localStorage.setItem('igraph-language', 'zh'); });

it('keeps molecular conversations consistent with other datasets and preserves free-form drafts', () => {
  const onSend = vi.fn();
  function Conversation() {
    const [value, setValue] = useState('自由问题');
    return <ChatPane exampleCase={userCase} artifactSessionId="session-a" conversationKey="session-a"
      messages={[{ id: 'u1', role: 'user', content: '前一个问题' }, { id: 'a1', role: 'assistant', content: '前一个回答', run_status: 'completed' }]}
      value={value} model="model" models={['model']} onChange={setValue} onModelChange={() => {}} onDataset={() => {}}
      onSend={onSend} onUpload={() => {}} onRemoveAttachment={() => {}} attachments={[]} loading={false} uploading={false} disabled={false}
      onOpenGraph={() => {}} interaction={{ kind: 'composer' }} />;
  }
  render(<Conversation />);
  expect(screen.getByText('前一个回答')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /分子硼化反应示例.*展开/ })).toBeNull();
  expect(screen.queryByRole('navigation', { name: '案例问题' })).toBeNull();
  expect(getBorylationCase).not.toHaveBeenCalled();
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('自由问题');
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '我想自由追问' } });
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('我想自由追问');
  expect(onSend).not.toHaveBeenCalled();
});

it('ignores a late response from a previous step and clears evidence on errors', async () => {
  let resolveStructure!: (value: BorylationCaseResponse) => void;
  vi.mocked(getBorylationCase).mockImplementation((_sid, step) => step === 'structure'
    ? new Promise(resolve => { resolveStructure = resolve; })
    : step === 'symmetry' ? Promise.resolve(response(step, { candidate_atom_count: 8, distinct_site_count: 6, site_classes: [{ symmetry_class: 1, display_numbers: [5, 14] }] }))
      : Promise.reject(new Error('案例记录读取失败')));
  render(<BorylationStoryPanel userCase={userCase} sessionId="session-a" onQuestion={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: /分子硼化反应示例.*展开/ }));
  fireEvent.click(screen.getByRole('button', { name: '2 排除等价位点' }));
  await screen.findByText('5, 14');
  await act(async () => { resolveStructure(response('structure', { heavy_atom_count: 22 })); });
  expect(screen.queryByText('重原子')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '3 看看已有产物' }));
  await screen.findByRole('alert');
  expect(screen.getByText('案例记录读取失败')).toBeTruthy();
  expect(screen.queryByText('5, 14')).toBeNull();
  expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
});

it('renders measured conversions and preserves missing matrix cells', async () => {
  const record = { rxn_id: 'RXN-1', ligand_id: 'L1', solvent_id: 'S1', ligand_name: '配体甲', solvent_name: '溶剂甲', conversion_percent: 36.75, source_file: 'screen.csv', source_line: 7 };
  vi.mocked(getBorylationCase).mockImplementation(async (_sid, step) => response(step, {
    record_count: 1, success_count: 1, negative_count: 0, ligand_count: 1, solvent_count: 2, conversion_max_percent: 36.75,
    success_definition: '单硼化转化率 ≥ 5% 才记为成功。', nonzero_count: 1, subthreshold_nonzero_count: 0,
    top_conditions: [record], ligands: [{ id: 'L1', name: '配体甲' }], solvents: [{ id: 'S1', name: '溶剂甲' }, { id: 'S2', name: '溶剂乙' }], matrix: [record],
  }));
  render(<BorylationStoryPanel userCase={userCase} sessionId="session-a" onQuestion={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: /分子硼化反应示例.*展开/ }));
  fireEvent.click(screen.getByRole('button', { name: '5 比较成功条件' }));
  await screen.findByText('36.75%');
  expect(screen.getByText('成功阈值：单硼化转化率 ≥ 5%。')).toBeTruthy();
  expect(screen.getByText('有非零转化的记录：1 · 非零但未达阈值：0')).toBeTruthy();
  fireEvent.click(screen.getByText(/展开配体 × 溶剂筛选网格/));
  expect(screen.getByText('36.75')).toBeTruthy();
  expect(screen.getByTitle('无记录').textContent).toBe('—');
  expect(screen.getByRole('columnheader', { name: '单硼化转化率' })).toBeTruthy();
  expect(screen.getByRole('img').getAttribute('src')).toBe('/examples/borylation/BO-01-observed.png');
});

it('localizes all six steps, questions, evidence labels and subsequent Tab hints', async () => {
  vi.mocked(getBorylationCase).mockImplementation(async (_sid, step) => response(step, {}));
  const english = userCase.questions.map((_, i) => `English question ${i + 1}?`);
  const bilingual: UserCase = { ...userCase, translations: { en: { name: 'Borylation', description: '', questions: english,
    story_steps: userCase.story_steps!.map((step, i) => ({ ...step, title: `Step ${i + 1}`, question: english[i] })) } } };
  const onQuestion = vi.fn();
  function Control() { const { setLanguage } = useI18n(); return <button onClick={() => setLanguage('en')}>English</button>; }
  render(<I18nProvider initialLanguage="zh"><Control /><BorylationStoryPanel userCase={bilingual} sessionId="s" onQuestion={onQuestion} /></I18nProvider>);
  fireEvent.click(screen.getByRole('button', { name: /分子硼化反应示例.*展开/ }));
  fireEvent.click(screen.getByRole('button', { name: 'English' }));
  await screen.findByRole('navigation', { name: 'Case questions' });
  for (let i = 0; i < 6; i++) {
    fireEvent.click(screen.getByRole('button', { name: `${i + 1} Step ${i + 1}` }));
    expect(onQuestion).toHaveBeenLastCalledWith(english[i]);
    const messages = [{ role: 'user' as const, content: userCase.questions[i], run_id: 'r' },
      { role: 'assistant' as const, content: 'answer', run_id: 'r', run_status: 'completed' }];
    const hint = nextExampleQuestion(bilingual, messages, en => en);
    expect(hint?.question).toBe(english[i + 1]);
    if (hint) expect(hint.total).toBe(6);
  }
});

it('follows a question sent through Tab without overwriting the composer draft', async () => {
  vi.mocked(getBorylationCase).mockImplementation(async (_sid, step) => response(step, {}));
  const onQuestion = vi.fn();
  const { rerender } = render(<I18nProvider initialLanguage="zh"><BorylationStoryPanel userCase={userCase} sessionId="s" activeQuestion={userCase.questions[0]} onQuestion={onQuestion} /></I18nProvider>);
  fireEvent.click(screen.getByRole('button', { name: /分子硼化反应示例.*展开/ }));
  rerender(<I18nProvider initialLanguage="zh"><BorylationStoryPanel userCase={userCase} sessionId="s" activeQuestion={userCase.questions[1]} onQuestion={onQuestion} /></I18nProvider>);
  await waitFor(() => expect(getBorylationCase).toHaveBeenLastCalledWith('s', 'symmetry', expect.any(AbortSignal)));
  expect(screen.getByRole('button', { name: '2 排除等价位点' }).getAttribute('aria-pressed')).toBe('true');
  expect(onQuestion).not.toHaveBeenCalled();
});


const fourSteps = ['properties', 'rings', 'structure', 'symmetry'] as const;
const fourCase: UserCase = { ...userCase, questions: fourSteps.map(s => `中文 ${s}`),
  story_steps: fourSteps.map(s => ({ id:s, title:`中文 ${s}`, analysis_step:s, question:`中文 ${s}` })),
  translations: { en: { name:'Molecular example', description:'', questions:fourSteps.map(s => `English ${s}`),
    story_steps:fourSteps.map(s => ({ id:s, title:`English ${s}`, analysis_step:s, question:`English ${s}` })) } } };

it('shows computed properties separately from missing measurements, and ring types separately from counts', async () => {
  vi.mocked(getBorylationCase).mockImplementation(async (_sid, step) => response(step, step === 'properties' ? {
    descriptors:{ formula:'C15H9ClFN3OS', molecular_weight_g_mol:333.775, clogp:4.25, tpsa_angstrom2:83.12, h_bond_donors:1, h_bond_acceptors:4 },
    method:{version:'2026.09.1'}, solubility:{recorded_reaction_solvents:[{name:'环己烷'}]},
  } : {ring_count:3,ring_type_count:2,rings:[{ring_id:2,display_numbers:[8,9,10,11,12],elements:{C:2,N:2,S:1},aromatic:true}]}));
  render(<I18nProvider initialLanguage="zh"><BorylationStoryPanel userCase={fourCase} sessionId="s" onQuestion={()=>{}} /></I18nProvider>);
  fireEvent.click(screen.getByRole('button',{name:/分子硼化反应示例.*展开/}));
  await screen.findByText('C15H9ClFN3OS');
  expect(screen.getByText('毒性：未知，未提供毒性测定或模型预测。')).toBeTruthy();
  expect(screen.getByRole('img').getAttribute('src')).toBe('/examples/borylation/BO-01-molecule.svg');
  fireEvent.click(screen.getByRole('button',{name:'2 中文 rings'}));
  await screen.findByText('环类型');
  expect(screen.getByText('C2 N2 S1')).toBeTruthy();
  expect(screen.getByText('环数量').nextElementSibling?.textContent).toBe('3');
  expect(screen.getByText('环类型').nextElementSibling?.textContent).toBe('2');
});

it('localizes the new four-question sequence and ends the Tab sequence at symmetry', async () => {
  vi.mocked(getBorylationCase).mockImplementation(async (_sid, step) => response(step, {}));
  const onQuestion=vi.fn();
  render(<I18nProvider initialLanguage="en"><BorylationStoryPanel userCase={fourCase} sessionId="s" onQuestion={onQuestion} /></I18nProvider>);
  fireEvent.click(screen.getByRole('button',{name:/Molecular example.*Expand/}));
  await screen.findByText('Toxicity: unknown. No toxicity measurements or model predictions are provided.');
  for(let i=0;i<fourSteps.length;i++) {
    fireEvent.click(screen.getByRole('button',{name:`${i+1} English ${fourSteps[i]}`}));
    expect(onQuestion).toHaveBeenLastCalledWith(`English ${fourSteps[i]}`);
    const hint=nextExampleQuestion(fourCase,[{role:'user',content:fourCase.questions[i],run_id:'r'}, {role:'assistant',content:'answer',run_id:'r',run_status:'completed'}],en=>en);
    expect(hint?.question).toBe(fourCase.translations!.en!.questions[i+1]);
    if(hint) expect(hint.total).toBe(4);
  }
});
