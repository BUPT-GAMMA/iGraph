import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ModelManager } from './ModelManager';
import { ModelCatalogContext, ModelPicker } from './ModelPicker';
import { Composer } from './Composer';
import { listModels, saveModelConfiguration, setModelEnabled, type ModelCatalog } from '../api';
vi.mock('../api', async () => ({ ...await vi.importActual('../api'), listModels: vi.fn(), saveModelConfiguration: vi.fn(), setModelEnabled: vi.fn() }));
const catalog: ModelCatalog = { default_model: 'a', models: ['a', 'b'].map((id, index) => ({ config_id: id, model_id: id, upstream_model_id: 'qwen-local', display_name: `Server ${id}`, revision: 1, source: 'custom', base_url: `http://localhost:${8000 + index}/v1`, enabled: true, tools: true, vision: false, context_window: 32768, max_output_tokens: 4096 })) };
beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); vi.mocked(listModels).mockResolvedValue(catalog); });
afterEach(cleanup);
it('shows identical upstream models as independent connections and supports search', () => {
  const change = vi.fn(); const manage = vi.fn();
  render(<ModelCatalogContext.Provider value={{ catalog, userId: 'user', manage, refresh: vi.fn() }}><ModelPicker model="a" disabled={false} onChange={change} /></ModelCatalogContext.Provider>);
  fireEvent.click(screen.getByLabelText('选择模型'));
  expect(screen.getByRole('button', { name: /Server a/ })).toBeTruthy();
  fireEvent.change(screen.getByLabelText('搜索模型'), { target: { value: '8001' } });
  expect(screen.queryByRole('button', { name: /Server a/ })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /Server b/ })); expect(change).toHaveBeenCalledWith('b');
});
it('saving an anonymous local configuration only updates the catalogue', async () => {
  const update = vi.fn(); render(<ModelManager onClose={vi.fn()} onUpdated={update} />);
  await screen.findByText('Server a'); fireEvent.click(screen.getByText('添加模型'));
  fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'Local' } });
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'local' } });
  fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'http://localhost:1234/v1' } });
  fireEvent.click(screen.getByText('保存配置'));
  await waitFor(() => expect(saveModelConfiguration).toHaveBeenCalledWith(expect.objectContaining({ upstream_model_id: 'local', api_key: '' })));
  await screen.findByText('已保存，当前会话使用的模型未改变。');
});
it('keeps a configuration conflict visible and refreshes current revisions', async () => {
  vi.mocked(setModelEnabled).mockRejectedValue(new Error('配置已变化，请刷新后重试'));
  render(<ModelManager onClose={vi.fn()} onUpdated={vi.fn()} />); await screen.findByText('Server a');
  fireEvent.click(screen.getAllByText('停用')[0]);
  await screen.findByRole('alert'); expect(listModels).toHaveBeenCalledTimes(2);
});
it('blocks send for an unavailable model while keeping selection usable', () => {
  const send = vi.fn();
  render(<ModelCatalogContext.Provider value={{ catalog, userId: 'user', unavailable: 'MODEL_DISABLED', manage: vi.fn(), refresh: vi.fn() }}><Composer value="hello" model="a" models={[]} onChange={vi.fn()} onModelChange={vi.fn()} onDataset={vi.fn()} onSend={send} onUpload={vi.fn()} onRemoveAttachment={vi.fn()} attachments={[]} loading={false} uploading={false} disabled={false} /></ModelCatalogContext.Provider>);
  expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '选择模型' }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' }); expect(send).not.toHaveBeenCalled();
});
