import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider, useI18n } from '../i18n';
import { helpModels, helpTools } from '../helpCatalog';
import { HelpPage } from './HelpPage';

afterEach(cleanup);

describe('HelpPage', () => {
  it.each(['zh', 'en'] as const)('keeps the model catalog concise alongside the pipeline in %s', language => {
    const { container } = render(<I18nProvider initialLanguage={language}><HelpPage /></I18nProvider>);
    expect(container.querySelector('.help-page-header p')).toBeNull();
    expect(container.querySelector('.help-eyebrow')).toBeNull();
    expect(container.querySelector('.help-note')).toBeNull();
    expect(container.querySelector('.help-workflow')).toBeNull();
    expect(screen.queryByText(/先选择数据集并构图或加载已构图数据|Start with a dataset, build or load its graph/)).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: language === 'zh' ? /图学习模型/ : /Graph learning models/ }));
    expect(container.querySelectorAll('.help-model-card')).toHaveLength(helpModels.length);
    expect(container.querySelector('.help-model-kind')).toBeNull();
    const requirements = container.querySelector<HTMLDetailsElement>('.help-requirements');
    expect(requirements?.open).toBe(false);
    expect(requirements?.textContent).toContain('HIV/PCBA');
    expect(container.querySelector('.help-model-card p')?.textContent).toBeTruthy();
    for (const label of ['GNN 主干', '节点嵌入', 'GNN backbone', 'Node embedding']) expect(screen.queryByText(label, { exact: true })).toBeNull();
  });

  it('searches by Chinese purpose and narrows results to the selected category', () => {
    render(<I18nProvider initialLanguage="zh"><HelpPage /></I18nProvider>);
    expect(screen.getByRole('heading', { name: '帮助与能力清单' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /图分析计算工具/ }));
    expect(screen.getAllByRole('article')).toHaveLength(helpTools.length);
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索帮助' }), { target: { value: '最短' } });
    expect(screen.getByText('shortest_path')).toBeTruthy();
    expect(screen.queryByText('community_detection')).toBeNull();
    fireEvent.change(screen.getByLabelText('工具分类'), { target: { value: 'communities' } });
    expect(screen.getByText('没有匹配的工具，请更换关键词或分类。')).toBeTruthy();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } });
    expect(screen.getByText('community_detection')).toBeTruthy();
    expect(screen.queryByText('shortest_path')).toBeNull();
  });

  it('lists registered graph models and supported tasks separately from language models', () => {
    render(<I18nProvider initialLanguage="en"><HelpPage /></I18nProvider>);
    fireEvent.click(screen.getByRole('tab', { name: /Graph learning models/ }));
    expect(screen.getByRole('tab', { name: /Graph learning models/ }).getAttribute('aria-selected')).toBe('true');
    for (const model of helpModels) expect(screen.getByRole('heading', { name: model.name === 'node2vec' ? 'Node2Vec' : model.name })).toBeTruthy();
    const gcn = screen.getByRole('heading', { name: 'GCN' }).closest('article')!;
    expect(within(gcn).getByText('Node classification')).toBeTruthy();
    expect(within(gcn).getByText('Graph classification')).toBeTruthy();
    const node2vec = screen.getByRole('heading', { name: 'Node2Vec' }).closest('article')!;
    expect(within(node2vec).getByText('Pretraining')).toBeTruthy();
    expect(within(node2vec).queryByText('Graph classification')).toBeNull();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'GraphSAGE' } });
    expect(screen.getByRole('heading', { name: 'SAGE' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'GCN' })).toBeNull();
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'GPT' } });
    expect(screen.getByText('No matching models. Try another keyword.')).toBeTruthy();
  });

  it.each(['zh', 'en'] as const)('omits the pretraining introduction and method search results in %s', language => {
    const { container } = render(<I18nProvider initialLanguage={language}><HelpPage /></I18nProvider>);
    fireEvent.click(screen.getByRole('tab', { name: language === 'zh' ? /图学习模型/ : /Graph learning models/ }));
    expect(container.querySelector('.help-pretraining')).toBeNull();
    expect(screen.queryByRole('heading', { name: /GNN (预训练方法|pretraining methods)/ })).toBeNull();
    expect(container.querySelectorAll('.help-model-card')).toHaveLength(helpModels.length);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'graph_mask_autoencoding' } });
    expect(screen.queryByRole('heading', { name: 'mask_autoencoding' })).toBeNull();
    expect(screen.getByText(language === 'zh' ? '没有匹配的模型，请更换关键词。' : 'No matching models. Try another keyword.')).toBeTruthy();
  });

  it('supports closing help and case-insensitive multi-term search', () => {
    const close = vi.fn();
    render(<I18nProvider initialLanguage="en"><HelpPage onClose={close} /></I18nProvider>);
    expect(screen.getByRole('dialog', { name: 'Help & capabilities' })).toBeTruthy();
    expect(screen.getByRole('separator', { name: 'Resize help panel' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /Graph analysis tools/ }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: ' SHORTEST minimum ' } });
    expect(screen.getByText('shortest_path')).toBeTruthy();
    expect(screen.queryByText('train')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Close help' }));
    fireEvent.animationEnd(screen.getByRole('dialog', { name: 'Help & capabilities' }).parentElement!);
    expect(close).toHaveBeenCalledOnce();
  });

  it('opens the pipeline first and keeps the selected file format when visiting the catalogs', () => {
    render(<I18nProvider initialLanguage="zh"><HelpPage /></I18nProvider>);
    expect(screen.getByRole('tab', { name: '使用流程' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('list', { name: 'TXT 到图分析的操作步骤' }).children).toHaveLength(6);
    expect(screen.getByText(/仅使用自动逐行模式不会识别语义关系/)).toBeTruthy();
    expect(screen.queryByRole('searchbox')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /SQL 关系型数据/ }));
    expect(screen.getByText(/SQLite 兼容 CREATE TABLE 与 INSERT/)).toBeTruthy();
    expect(screen.queryByText(/仅使用自动逐行模式不会识别语义关系/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /CSV 单表或多表/ }));
    expect(screen.getByText(/自动建议仅依据列名，需要人工核对/)).toBeTruthy();
    expect(screen.getByText(/点击“保存构图结果”，等待显示/)).toBeTruthy();
    expect(screen.getByText(/在“个人数据集”中选择已保存的图/)).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /图分析计算工具/ }));
    expect(screen.queryByRole('heading', { name: '构建你的图，再开始提问' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '使用流程' }));
    expect(screen.getByRole('button', { name: /CSV 单表或多表/ }).getAttribute('aria-pressed')).toBe('true');
  });

  it('updates the entire selected pipeline immediately when the interface language changes', () => {
    function LanguageSwitch() {
      const { toggleLanguage } = useI18n();
      return <button onClick={toggleLanguage}>Switch language</button>;
    }
    render(<I18nProvider initialLanguage="zh"><LanguageSwitch /><HelpPage /></I18nProvider>);
    fireEvent.click(screen.getByRole('button', { name: /CSV 单表或多表/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Switch language' }));
    const panel = screen.getByRole('tabpanel', { name: 'Usage pipeline' });
    expect(within(panel).getByRole('button', { name: /CSV One or more tables/ }).getAttribute('aria-pressed')).toBe('true');
    expect(within(panel).getByRole('list', { name: 'CSV to graph analysis' }).children).toHaveLength(6);
    expect(within(panel).getByText(/Click “Save construction result” and wait/)).toBeTruthy();
    expect(within(panel).getByText(/open that project’s datasets/)).toBeTruthy();
    expect(within(panel).getByText(/Check connected components and isolated records/)).toBeTruthy();
    expect(panel.textContent).not.toMatch(/[\u3400-\u9fff]/);
    fireEvent.click(screen.getByRole('button', { name: 'Switch language' }));
    expect(screen.getByRole('tabpanel', { name: '使用流程' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /CSV 单表或多表/ }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByText(/检查连通分量与孤立记录/)).toBeTruthy();
  });
});
