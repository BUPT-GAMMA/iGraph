import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { helpModels, helpTools } from '../helpCatalog';
import { HelpPage } from './HelpPage';

afterEach(cleanup);

describe('HelpPage', () => {
  it.each(['zh', 'en'] as const)('omits the workflow banner and model-kind badges in %s', language => {
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
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: ' SHORTEST minimum ' } });
    expect(screen.getByText('shortest_path')).toBeTruthy();
    expect(screen.queryByText('train')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Close help' }));
    fireEvent.animationEnd(screen.getByRole('dialog', { name: 'Help & capabilities' }).parentElement!);
    expect(close).toHaveBeenCalledOnce();
  });
});
