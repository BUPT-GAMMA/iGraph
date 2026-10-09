import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DatasetDescriptionEditor } from './DatasetDescriptionEditor';
import type { Dataset } from '../types';
import { UserCaseContext } from '../userCases';
import { I18nProvider } from '../i18n';

const mocks = vi.hoisted(() => ({ update: vi.fn() }));
vi.mock('../api', () => ({ updateProjectDataset: mocks.update }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const dataset: Dataset = { dataset_id: 'd1', project_id: null, name: 'People', original_filename: 'people.csv', file_type: 'csv', status: 'ready', description: 'Existing context' };

describe('dataset description', () => {
  it('localizes only the bundled description without saving or overwriting user edits', () => {
    const example = { id: 'finance', filename: 'finance.npz', name: '金融', description: '默认描述', questions: [], available: true,
      translations: { en: { name: 'Finance', description: 'Default description', questions: [] } } };
    const props = { ...dataset, description: '默认描述' };
    const { rerender } = render(<I18nProvider initialLanguage="en"><UserCaseContext.Provider value={[example]}><DatasetDescriptionEditor dataset={props} /></UserCaseContext.Provider></I18nProvider>);
    expect((screen.getByLabelText('Dataset description') as HTMLTextAreaElement).value).toBe('Default description');
    expect((screen.getByRole('button', { name: 'Save description' }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.update).not.toHaveBeenCalled();
    rerender(<I18nProvider initialLanguage="en"><UserCaseContext.Provider value={[example]}><DatasetDescriptionEditor dataset={{ ...props, description: '我自己的描述' }} /></UserCaseContext.Provider></I18nProvider>);
    expect((screen.getByLabelText('Dataset description') as HTMLTextAreaElement).value).toBe('我自己的描述');
    rerender(<I18nProvider key="restore-language" initialLanguage="zh"><span /></I18nProvider>);
  });
  it('saves trimmed description and reports the updated dataset', async () => {
    const updated = { ...dataset, description: 'New context' };
    mocks.update.mockResolvedValue({ dataset: updated });
    const onSaved = vi.fn();
    render(<DatasetDescriptionEditor dataset={dataset} onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText('数据集描述'), { target: { value: '  New context  ' } });
    fireEvent.click(screen.getByRole('button', { name: '保存描述' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(updated));
    expect(mocks.update).toHaveBeenCalledWith(null, 'd1', { description: 'New context' });
    expect((screen.getByLabelText('数据集描述') as HTMLTextAreaElement).value).toBe('New context');
  });

  it('retains the draft when saving fails and supports clearing descriptions', async () => {
    mocks.update.mockRejectedValue(new Error('Network unavailable'));
    render(<DatasetDescriptionEditor dataset={dataset} />);
    fireEvent.change(screen.getByLabelText('数据集描述'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存描述' }));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Network unavailable');
    expect(mocks.update).toHaveBeenCalledWith(null, 'd1', { description: '' });
    expect((screen.getByLabelText('数据集描述') as HTMLTextAreaElement).value).toBe('');
  });
});
