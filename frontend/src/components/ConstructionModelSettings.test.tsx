import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as api from '../api';
import { ConstructionModelSettings } from './ConstructionModelSettings';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'), getConstructionModelSettings: vi.fn(), saveConstructionModelSettings: vi.fn() }));
const initial = { model: 'deepseek-v4-pro', has_base_url: true, has_api_key: true, has_server_config: true };
function Harness() {
  const [model, setModel] = useState('deepseek-v4-pro');
  const [models, setModels] = useState(['deepseek-v4-pro', 'claude-opus-5-5']);
  const [dirty, setDirty] = useState(false);
  return <><ConstructionModelSettings model={model} models={models} disabled={false} onModelChange={setModel}
    onDirtyChange={setDirty} onSavedModel={value => { setModel(value); setModels([...new Set([...models, value])]); }} />
    <button disabled={dirty}>开始构图</button></>;
}
beforeEach(() => {
  vi.mocked(api.getConstructionModelSettings).mockResolvedValue(initial);
  vi.mocked(api.saveConstructionModelSettings).mockImplementation(async value => ({ ...initial, model: value.model, has_api_key: !value.clear_api_key }));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('starts with blank URL and key even when the server connection is configured', async () => {
  render(<Harness />); await screen.findByText('连接已配置');
  expect((screen.getByLabelText('构图 API URL') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('构图 API Key') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('构图大模型') as HTMLSelectElement).value).toBe('deepseek-v4-pro');
  fireEvent.change(screen.getByLabelText('构图大模型'), { target: { value: 'claude-opus-5-5' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await waitFor(() => expect(api.saveConstructionModelSettings).toHaveBeenCalledWith({ model: 'claude-opus-5-5', base_url: '', api_key: '', clear_api_key: false }));
});

it('saves a custom model and connection before enabling construction and resets secret fields', async () => {
  render(<Harness />); await screen.findByText('连接已配置');
  fireEvent.change(screen.getByLabelText('构图大模型'), { target: { value: '__custom__' } });
  fireEvent.change(screen.getByLabelText('构图模型 ID'), { target: { value: 'team/extractor' } });
  fireEvent.change(screen.getByLabelText('构图 API URL'), { target: { value: 'https://gateway.test/v1' } });
  fireEvent.change(screen.getByLabelText('构图 API Key'), { target: { value: 'entered-secret' } });
  expect((screen.getByRole('button', { name: '开始构图' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  await screen.findByRole('status');
  expect(api.saveConstructionModelSettings).toHaveBeenCalledWith({ model: 'team/extractor', base_url: 'https://gateway.test/v1', api_key: 'entered-secret', clear_api_key: false });
  expect((screen.getByLabelText('构图 API Key') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('构图 API URL') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('button', { name: '开始构图' }) as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByLabelText('构图大模型') as HTMLSelectElement).value).toBe('team/extractor');
});

it('keeps edited settings and construction blocked when saving fails', async () => {
  vi.mocked(api.saveConstructionModelSettings).mockRejectedValue(new Error('无法连接服务器'));
  render(<Harness />); await screen.findByText('连接已配置');
  fireEvent.change(screen.getByLabelText('构图 API URL'), { target: { value: 'https://new.test/v1' } });
  fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }));
  expect((await screen.findByRole('alert')).textContent).toBe('无法连接服务器');
  expect((screen.getByLabelText('构图 API URL') as HTMLInputElement).value).toBe('https://new.test/v1');
  expect((screen.getByRole('button', { name: '开始构图' }) as HTMLButtonElement).disabled).toBe(true);
});
