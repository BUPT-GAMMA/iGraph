import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api';
import { ModelSettings } from './ModelSettings';
import type { ModelConfiguration, ModelProviderConnection } from '../api';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'),
  listModelProviders: vi.fn(), listModels: vi.fn(), saveModelProvider: vi.fn(), discoverProviderModels: vi.fn(),
  saveModelConfiguration: vi.fn(), importProviderModels: vi.fn(), deleteModelConfiguration: vi.fn(), deleteModelProvider: vi.fn(),
  setDefaultModel: vi.fn(), setModelEnabled: vi.fn(), testModelConfiguration: vi.fn(),
}));
const provider: ModelProviderConnection = { provider_id: 'p1', revision: 1, name: '我的网关', preset_id: 'custom',
  protocol: 'openai-compatible', base_url: 'https://example.test/v1', enabled: true, has_api_key: true, model_count: 0 };
const model: ModelConfiguration = { config_id: 'custom-opaque-id', model_id: 'custom-opaque-id', revision: 1,
  upstream_model_id: 'qwen/team', display_name: '团队模型', provider_id: 'p1', provider_name: '我的网关',
  base_url: provider.base_url, source: 'custom', enabled: true, tools: true, vision: false, context_window: 131072, max_output_tokens: 4096 };
let connections: ModelProviderConnection[];
let models: ModelConfiguration[];
const user = () => userEvent.setup();
async function openProvider() {
  await screen.findByRole('button', { name: /我的网关/ });
  fireEvent.click(screen.getByRole('button', { name: /我的网关/ }));
}
beforeEach(() => {
  vi.resetAllMocks(); connections = [{ ...provider }]; models = [];
  vi.mocked(api.listModelProviders).mockImplementation(async () => ({ providers: connections, presets: [
    { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1', icon: 'openai' },
    { id: 'custom', name: '自定义提供商', base_url: '', icon: 'unknown' },
  ] }));
  vi.mocked(api.listModels).mockImplementation(async () => ({ models, default_model: '' }));
  vi.mocked(api.saveModelProvider).mockImplementation(async input => {
    const saved = { ...provider, ...input, provider_id: input.provider_id || 'new-provider', revision: 2, has_api_key: true, model_count: 0 };
    connections = [saved]; return { provider: saved };
  });
  vi.mocked(api.importProviderModels).mockImplementation(async (p, items) => {
    models = [...models, ...items.map(item => ({ ...model, model_id: item.id, config_id: item.id, upstream_model_id: item.id, display_name: item.name }))];
    return { added_count: items.length };
  });
  vi.mocked(api.saveModelConfiguration).mockImplementation(async input => {
    const saved = { ...model, ...input, config_id: input.config_id || input.upstream_model_id, model_id: input.config_id || input.upstream_model_id, revision: 2 };
    models = [...models.filter(m => m.config_id !== saved.config_id), saved]; return { model: saved };
  });
});
afterEach(cleanup);

describe('Model services', () => {
  it('creates a provider from a preset, then opens model configuration with inherited credentials', async () => {
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByRole('button', { name: /我的网关/ });
    await u.click(screen.getByRole('button', { name: '添加提供商' }));
    await u.selectOptions(screen.getByLabelText('提供商类型'), 'openai');
    expect(screen.queryByLabelText('API 地址')).toBeNull();
    await u.click(screen.getByRole('button', { name: '添加' }));
    await screen.findByLabelText('API 地址');
    expect((screen.getByLabelText('API 地址') as HTMLInputElement).value).toBe('https://api.openai.com/v1');
    await u.type(screen.getByLabelText('API 密钥'), 'new-secret');
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存');
    expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ name: 'OpenAI', api_key: 'new-secret' }));
    expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('');
    await u.click(screen.getByRole('button', { name: '添加' }));
    await u.type(screen.getByLabelText('模型 ID'), 'team/model');
    await u.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.saveModelConfiguration).toHaveBeenCalledWith(expect.objectContaining({ provider_id: 'new-provider', provider_revision: 2, upstream_model_id: 'team/model' })));
    expect(vi.mocked(api.saveModelConfiguration).mock.calls[0][0]).not.toHaveProperty('api_key');
  });

  it('preserves a saved key and asks before discarding connection edits', async () => {
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await openProvider();
    expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('');
    expect(screen.getByLabelText('API 密钥').getAttribute('title')).toContain('保留');
    await u.click(screen.getByRole('button', { name: '编辑提供商' }));
    await u.clear(screen.getByLabelText('提供商名称')); await u.type(screen.getByLabelText('提供商名称'), '新名称');
    expect((screen.getByRole('button', { name: '检测' }) as HTMLButtonElement).disabled).toBe(true);
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).not.toHaveBeenCalled(); expect(screen.getByRole('alertdialog').textContent).toContain('放弃');
    await u.click(screen.getByRole('button', { name: '取消' }));
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存');
    expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ api_key: '', name: '新名称' }));
  });

  it('searches discovery results and imports only selected missing models', async () => {
    models = [model];
    vi.mocked(api.discoverProviderModels).mockResolvedValue({ models: [{ id: 'qwen/team', name: 'Existing' }, { id: 'new/a', name: 'New A' }, { id: 'new/b', name: 'New B' }], latency_ms: 30, truncated: false });
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '管理' }));
    await screen.findByLabelText('搜索发现的模型');
    expect(screen.getByLabelText(/Existing/).getAttribute('disabled')).not.toBeNull();
    await u.type(screen.getByLabelText('搜索发现的模型'), 'new/a');
    await u.click(screen.getByRole('button', { name: '全选' }));
    await u.click(screen.getByRole('button', { name: '添加所选（1）' }));
    await screen.findByText('已添加 1 个模型');
    expect(api.importProviderModels).toHaveBeenCalledOnce();
    expect(api.importProviderModels).toHaveBeenCalledWith(provider, [{ id: 'new/a', name: 'New A' }]);
  });

  it('leaves manual addition available when discovery is unsupported', async () => {
    vi.mocked(api.discoverProviderModels).mockRejectedValue(new Error('该接口不支持模型发现，请手动添加模型 ID'));
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '管理' }));
    expect((await screen.findByRole('alert')).textContent).toContain('手动添加');
    await u.click(screen.getByRole('button', { name: '添加' }));
    expect(screen.getByLabelText('模型 ID')).toBeTruthy();
  });

  it('manages model availability and default without changing the active conversation', async () => {
    models = [model]; const changed = vi.fn(); const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={changed} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '将 团队模型 设为默认' }));
    await waitFor(() => expect(api.setDefaultModel).toHaveBeenCalledWith('custom-opaque-id'));
    await waitFor(() => expect((screen.getByRole('switch', { name: '启用 团队模型' }) as HTMLButtonElement).disabled).toBe(false));
    await u.click(screen.getByRole('switch', { name: '启用 团队模型' }));
    await waitFor(() => expect(api.setModelEnabled).toHaveBeenCalledWith(model, false));
    expect(changed).toHaveBeenCalled();
  });

  it('retains the provider and explains an active-run deletion failure', async () => {
    models = [model]; vi.mocked(api.deleteModelConfiguration).mockRejectedValue(new Error('有活动任务正在使用此模型'));
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '移除 团队模型' }));
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '移除' }));
    expect((await screen.findByRole('alert')).textContent).toContain('活动任务');
    expect(screen.getByText('团队模型')).toBeTruthy();
  });
});


it('deletes a provider and all its models without requiring per-model removal', async () => {
  models = [model]; connections = [{ ...provider, model_count: 1 }];
  vi.mocked(api.deleteModelProvider).mockImplementation(async () => { connections = []; models = []; return { success: true }; });
  const changed = vi.fn(); const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={changed} />);
  await openProvider();
  await u.click(screen.getByRole('button', { name: '编辑提供商' }));
  await u.type(screen.getByLabelText('提供商名称'), '尚未保存');
  await u.click(screen.getByRole('button', { name: '删除提供商' }));
  const confirmation = screen.getByRole('alertdialog');
  expect(confirmation.textContent).toContain('同时删除 1 个模型');
  expect(document.activeElement).toBe(within(confirmation).getByRole('button', { name: '取消' }));
  await u.click(within(confirmation).getByRole('button', { name: '删除' }));
  await waitFor(() => expect(api.deleteModelProvider).toHaveBeenCalledWith(expect.objectContaining({ provider_id: 'p1', revision: 1 })));
  await waitFor(() => expect(screen.queryByRole('button', { name: /我的网关/ })).toBeNull());
  expect(api.deleteModelConfiguration).not.toHaveBeenCalled();
  expect(changed).toHaveBeenLastCalledWith({ models: [], default_model: '' });
});

it('cancels provider deletion with Escape and displays an active-task rejection', async () => {
  models = [model];
  vi.mocked(api.deleteModelProvider).mockRejectedValue(new Error('提供商正被任务使用，结束或停止任务后可删除'));
  const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
  await openProvider();
  await u.click(screen.getByRole('button', { name: '删除提供商' }));
  await u.keyboard('{Escape}');
  expect(screen.queryByRole('alertdialog')).toBeNull(); expect(close).not.toHaveBeenCalled();
  expect(api.deleteModelProvider).not.toHaveBeenCalled();
  await u.click(screen.getByRole('button', { name: '删除提供商' }));
  await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '删除' }));
  expect((await screen.findByRole('alert')).textContent).toContain('正被任务使用');
  expect(screen.getByText('团队模型')).toBeTruthy();
});
