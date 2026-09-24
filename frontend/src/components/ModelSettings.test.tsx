import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '../api';
import { ModelSettings } from './ModelSettings';
import type { ModelConfiguration, ModelProviderConnection } from '../api';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'),
  getProviderCredentials: vi.fn(), listModelProviders: vi.fn(), listModels: vi.fn(), saveModelProvider: vi.fn(), discoverProviderModels: vi.fn(),
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
let keys: Record<string, string>;
const user = () => userEvent.setup();
async function openProvider() {
  await screen.findByRole('button', { name: /我的网关/ });
  fireEvent.click(screen.getByRole('button', { name: /我的网关/ }));
  await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).disabled).toBe(false));
}
beforeEach(() => {
  vi.resetAllMocks(); connections = [{ ...provider }]; models = []; keys = { p1: 'stored-secret', 'shared-zzz': 'stored-secret' };
  vi.mocked(api.listModelProviders).mockImplementation(async () => ({ providers: connections, presets: [
    { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1', icon: 'openai' },
    { id: 'custom', name: '自定义提供商', base_url: '', icon: 'unknown' },
  ] }));
  vi.mocked(api.listModels).mockImplementation(async () => ({ models, default_model: '' }));
  vi.mocked(api.getProviderCredentials).mockImplementation(async p => ({ provider_id: p.provider_id, revision: p.revision, api_key: keys[p.provider_id] || '' }));
  vi.mocked(api.saveModelProvider).mockImplementation(async input => {
    const id = input.provider_id || 'new-provider';
    keys[id] = input.clear_api_key ? '' : input.api_key || keys[id] || '';
    const saved = { ...provider, ...input, provider_id: id, revision: (input.revision || 0) + 1, has_api_key: !!keys[id], model_count: 0 };
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
    await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).disabled).toBe(false));
    await u.type(screen.getByLabelText('API 密钥'), 'new-secret');
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存');
    expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ name: 'OpenAI', api_key: 'new-secret' }));
    await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('new-secret'));
    await u.click(screen.getByRole('button', { name: '添加' }));
    await u.type(screen.getByLabelText('模型 ID'), 'team/model');
    await u.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(api.saveModelConfiguration).toHaveBeenCalledWith(expect.objectContaining({ provider_id: 'new-provider', provider_revision: 2, upstream_model_id: 'team/model' })));
    expect(vi.mocked(api.saveModelConfiguration).mock.calls[0][0]).not.toHaveProperty('api_key');
  });

  it('preserves a saved key and asks before discarding connection edits', async () => {
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await openProvider();
    expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('stored-secret');
    expect(screen.getByLabelText('API 密钥').getAttribute('type')).toBe('password');
    await u.click(screen.getByRole('button', { name: '编辑提供商' }));
    await u.clear(screen.getByLabelText('提供商名称')); await u.type(screen.getByLabelText('提供商名称'), '新名称');
    expect((screen.getByRole('button', { name: '检测' }) as HTMLButtonElement).disabled).toBe(true);
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).not.toHaveBeenCalled(); expect(screen.getByRole('alertdialog').textContent).toContain('放弃');
    await u.click(screen.getByRole('button', { name: '取消' }));
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存');
    expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ api_key: 'stored-secret', name: '新名称' }));
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


it('opens the actual shared provider and exposes the same connection and model controls to admins', async () => {
  const shared = { ...provider, provider_id: 'shared-zzz', name: '智增增', preset_id: 'zhizengzeng', scope: 'shared' as const, editable: true, model_count: 1 };
  connections = [provider, shared]; models = [{ ...model, source: 'deployment', provider_id: shared.provider_id, provider_name: shared.name }];
  vi.mocked(api.listModels).mockImplementation(async () => ({ models, default_model: model.config_id }));
  const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
  await screen.findByRole('heading', { name: '智增增' });
  expect(screen.queryByText('系统模型')).toBeNull();
  expect((screen.getByLabelText('API 地址') as HTMLInputElement).value).toBe(shared.base_url);
  await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('stored-secret'));
  expect(screen.getByRole('button', { name: '管理' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '编辑 团队模型' })).toBeTruthy();
  await u.click(screen.getByRole('button', { name: '删除提供商' }));
  expect(screen.getByRole('alertdialog').textContent).toContain('所有账户');
  expect(api.deleteModelProvider).not.toHaveBeenCalled();
});

it('keeps a shared connection visible but read-only for members and preserves personal model choices', async () => {
  connections = [{ ...provider, name: '智增增', scope: 'shared', editable: false }];
  models = [{ ...model, source: 'deployment' }];
  const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
  await screen.findByRole('heading', { name: '智增增' });
  expect((screen.getByLabelText('API 地址') as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByLabelText('API 密钥').getAttribute('placeholder')).toBe('已配置');
  expect(api.getProviderCredentials).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '删除提供商' })).toBeNull();
  expect(screen.queryByRole('button', { name: '管理' })).toBeNull();
  expect(screen.queryByRole('button', { name: '移除 团队模型' })).toBeNull();
  await u.click(screen.getByRole('button', { name: '将 团队模型 设为默认' }));
  await waitFor(() => expect(api.setDefaultModel).toHaveBeenCalledWith(model.config_id));
  await u.click(screen.getByRole('button', { name: '添加提供商' }));
  expect(screen.queryByLabelText('使用范围')).toBeNull();
});

it('creates personal providers without an access selector even for administrators', async () => {
  connections = [{ ...provider, scope: 'shared', editable: true }];
  vi.mocked(api.listModelProviders).mockImplementation(async () => ({ providers: connections, presets: [
    { id: 'zhizengzeng', name: '智增增', base_url: 'https://api.zhizengzeng.com/v1', icon: 'unknown' },
    { id: 'custom', name: '自定义提供商', base_url: '', icon: 'unknown' },
  ] }));
  const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
  await openProvider(); await u.click(screen.getByRole('button', { name: '添加提供商' }));
  await u.selectOptions(screen.getByLabelText('提供商类型'), 'zhizengzeng');
  expect(screen.queryByLabelText('使用范围')).toBeNull();
  expect(screen.queryByText('共享')).toBeNull();
  await u.click(screen.getByRole('button', { name: '添加' }));
  await waitFor(() => expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ name: '智增增', base_url: 'https://api.zhizengzeng.com/v1' })));
  expect(vi.mocked(api.saveModelProvider).mock.calls[0][0]).not.toHaveProperty('scope');
});


it('reads and reveals the actual saved key, then masks it again on reopen', async () => {
  const close = vi.fn(); const u = user();
  const first = render(<ModelSettings onClose={close} onChanged={vi.fn()} />); await openProvider();
  const input = screen.getByLabelText('API 密钥') as HTMLInputElement;
  expect(input.value).toBe('stored-secret'); expect(input.type).toBe('password'); expect(input.placeholder).toBe('API Key');
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  await u.click(screen.getByRole('button', { name: '显示 API 密钥' }));
  expect(input.type).toBe('text'); expect(input.value).toBe('stored-secret');
  await u.click(screen.getByRole('button', { name: '隐藏 API 密钥' })); expect(input.type).toBe('password');
  await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
  expect(close).toHaveBeenCalledOnce(); expect(screen.queryByRole('alertdialog')).toBeNull();
  first.unmount(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />); await openProvider();
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('stored-secret');
  expect(screen.getByLabelText('API 密钥').getAttribute('type')).toBe('password');
  expect(api.getProviderCredentials).toHaveBeenCalledTimes(2);
});

it('persists replacement and clearing through the key input', async () => {
  const u = user(); const first = render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
  await u.clear(screen.getByLabelText('API 密钥')); await u.type(screen.getByLabelText('API 密钥'), 'replacement-key');
  await u.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(api.saveModelProvider).toHaveBeenCalledWith(expect.objectContaining({ api_key: 'replacement-key', clear_api_key: false })));
  await waitFor(() => expect((screen.getByRole('button', { name: '检测' }) as HTMLButtonElement).disabled).toBe(false));
  first.unmount(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('replacement-key');
  await u.clear(screen.getByLabelText('API 密钥')); await u.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(api.saveModelProvider).toHaveBeenLastCalledWith(expect.objectContaining({ api_key: '', clear_api_key: true })));
  await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).disabled).toBe(false));
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  await u.click(screen.getByRole('button', { name: '刷新模型服务' }));
  await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).disabled).toBe(false));
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('');
});

it('ignores a late credential response after switching providers', async () => {
  connections.push({ ...provider, provider_id: 'p2', name: '第二网关' });
  let complete!: (result: { provider_id: string; revision: number; api_key: string }) => void;
  vi.mocked(api.getProviderCredentials).mockImplementation(p => p.provider_id === 'p1' ? new Promise(resolve => { complete = resolve; })
    : Promise.resolve({ provider_id: 'p2', revision: 1, api_key: 'second-key' }));
  const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
  await screen.findByRole('heading', { name: '我的网关' });
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('button', { name: '显示 API 密钥' }) as HTMLButtonElement).disabled).toBe(true);
  await u.click(screen.getByRole('button', { name: /第二网关/ }));
  await waitFor(() => expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('second-key'));
  await act(async () => complete({ provider_id: 'p1', revision: 1, api_key: 'late-first-key' }));
  expect((screen.getByLabelText('API 密钥') as HTMLInputElement).value).toBe('second-key');
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
});

it('shows a load error and retries without fake dots', async () => {
  vi.mocked(api.getProviderCredentials).mockRejectedValueOnce(new Error('读取密钥失败'));
  const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
  expect((await screen.findByRole('alert')).textContent).toContain('读取密钥失败');
  const input = screen.getByLabelText('API 密钥') as HTMLInputElement;
  expect(input.value).toBe(''); expect(input.placeholder).toBe('读取失败'); expect(input.disabled).toBe(true);
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  await u.click(screen.getByRole('button', { name: '刷新模型服务' }));
  await waitFor(() => expect(input.value).toBe('stored-secret'));
  expect(input.type).toBe('password'); expect(screen.queryByRole('alert')).toBeNull();
});

describe('unsaved model changes', () => {
  it.each(['返回', '取消', '关闭模型服务', 'Escape', '切换提供商', '刷新模型服务'])('does not prompt on %s after only opening a model', async action => {
    models = [model]; connections.push({ ...provider, provider_id: 'p2', name: '第二网关' });
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await openProvider(); await u.click(screen.getByRole('button', { name: '编辑 团队模型' }));
    expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe(model.upstream_model_id);
    if (action === 'Escape') await u.keyboard('{Escape}');
    else if (action === '切换提供商') await u.click(screen.getByRole('button', { name: /第二网关/ }));
    else await u.click(screen.getByRole('button', { name: action }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(api.saveModelConfiguration).not.toHaveBeenCalled();
    if (action === '关闭模型服务' || action === 'Escape') expect(close).toHaveBeenCalledOnce();
    else if (action === '刷新模型服务') await waitFor(() => expect(api.listModels).toHaveBeenCalledTimes(2));
    else expect(screen.queryByLabelText('模型 ID')).toBeNull();
  });

  it.each([
    ['模型 ID', 'qwen/new'], ['显示名称', '新名称'], ['上下文窗口', '65536'], ['最大输出', '2048'],
    ['工具调用', null], ['视觉', null],
  ])('protects changes to %s, but allows returning after restoring its original value', async (label, changed) => {
    models = [model]; const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />);
    await openProvider(); await u.click(screen.getByRole('button', { name: '编辑 团队模型' }));
    const input = screen.getByLabelText(label!) as HTMLInputElement;
    const initial = input.value;
    if (changed === null) await u.click(input); else fireEvent.change(input, { target: { value: changed } });
    await u.click(screen.getByRole('button', { name: '返回' }));
    expect(screen.getByRole('alertdialog').textContent).toContain('放弃修改');
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取消' }));
    if (changed === null) await u.click(input); else fireEvent.change(input, { target: { value: initial } });
    await u.click(screen.getByRole('button', { name: '返回' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.queryByLabelText('模型 ID')).toBeNull();
    expect(api.saveModelConfiguration).not.toHaveBeenCalled();
  });

  it('leaves untouched new-model defaults directly and protects a newly entered model', async () => {
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '添加' }));
    await u.click(screen.getByRole('button', { name: '返回' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await u.click(screen.getByRole('button', { name: '添加' }));
    await u.type(screen.getByLabelText('模型 ID'), 'new-model');
    await u.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '放弃' }));
    expect(screen.queryByLabelText('模型 ID')).toBeNull();
    await u.click(screen.getByRole('button', { name: '添加' }));
    expect((screen.getByLabelText('模型 ID') as HTMLInputElement).value).toBe('');
    expect(api.saveModelConfiguration).not.toHaveBeenCalled();
  });

  it('resets discarded model edits to saved values when refreshing', async () => {
    models = [model]; const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await openProvider(); await u.click(screen.getByRole('button', { name: '编辑 团队模型' }));
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '未保存名称' } });
    await u.click(screen.getByRole('button', { name: '刷新模型服务' }));
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '放弃' }));
    await waitFor(() => expect((screen.getByLabelText('显示名称') as HTMLInputElement).value).toBe(model.display_name));
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).toHaveBeenCalledOnce(); expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('keeps unsaved edits after a failed save, and is clean after a successful retry', async () => {
    models = [model]; vi.mocked(api.saveModelConfiguration).mockRejectedValueOnce(new Error('保存失败'));
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await openProvider(); await u.click(screen.getByRole('button', { name: '编辑 团队模型' }));
    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: '新名称' } });
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('保存失败');
    await u.click(screen.getByRole('button', { name: '返回' }));
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取消' }));
    expect((screen.getByLabelText('显示名称') as HTMLInputElement).value).toBe('新名称');
    await u.click(screen.getByRole('button', { name: '保存' }));
    await screen.findByText('已保存');
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).toHaveBeenCalledOnce(); expect(screen.queryByRole('alertdialog')).toBeNull();
  });
});

describe('connection change baseline', () => {
  it('does not prompt for credentials that are still loading or have just arrived', async () => {
    let complete!: (value: { provider_id: string; revision: number; api_key: string }) => void;
    vi.mocked(api.getProviderCredentials).mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await screen.findByRole('heading', { name: '我的网关' });
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).toHaveBeenCalledOnce(); expect(screen.queryByRole('alertdialog')).toBeNull();
    await act(async () => complete({ provider_id: 'p1', revision: 1, api_key: 'stored-secret' }));
    await u.click(screen.getByRole('button', { name: '编辑提供商' }));
    await u.click(screen.getByRole('button', { name: '显示 API 密钥' }));
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).toHaveBeenCalledTimes(2); expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('preserves real edits made while credentials load and recognizes restored values', async () => {
    let complete!: (value: { provider_id: string; revision: number; api_key: string }) => void;
    vi.mocked(api.getProviderCredentials).mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }));
    const close = vi.fn(); const u = user(); render(<ModelSettings onClose={close} onChanged={vi.fn()} />);
    await screen.findByRole('heading', { name: '我的网关' });
    await u.click(screen.getByRole('button', { name: '编辑提供商' }));
    fireEvent.change(screen.getByLabelText('提供商名称'), { target: { value: '新名称' } });
    await act(async () => complete({ provider_id: 'p1', revision: 1, api_key: 'stored-secret' }));
    expect((screen.getByLabelText('提供商名称') as HTMLInputElement).value).toBe('新名称');
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).not.toHaveBeenCalled();
    await u.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取消' }));
    fireEvent.change(screen.getByLabelText('提供商名称'), { target: { value: provider.name } });
    fireEvent.change(screen.getByLabelText('API 密钥'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('API 密钥'), { target: { value: 'stored-secret' } });
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: 'https://other.test/v1' } });
    fireEvent.change(screen.getByLabelText('API 地址'), { target: { value: provider.base_url } });
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
    await u.click(screen.getByRole('button', { name: '关闭模型服务' }));
    expect(close).toHaveBeenCalledOnce(); expect(screen.queryByRole('alertdialog')).toBeNull();
  });
});

describe('inline model tests', () => {
  it('shows progress, failure and retry success inside the tested row without a top banner', async () => {
    models = [model];
    let reject!: (reason: Error) => void;
    vi.mocked(api.testModelConfiguration).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }))
      .mockResolvedValueOnce({ success: true });
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    const button = screen.getByRole('button', { name: '检测 团队模型' });
    const row = button.closest('article')!;
    await u.click(button);
    const status = within(row).getByRole('status', { name: '团队模型 检测结果' });
    expect(status.textContent).toContain('检测中');
    expect(button.getAttribute('aria-describedby')).toBe(status.id);
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(row.closest('main')!.getAttribute('aria-busy')).toBe('false');
    await act(async () => reject(new Error('认证失败，请检查 API Key')));
    expect(status.textContent).toBe('检测失败：认证失败，请检查 API Key');
    expect(document.querySelector('.pm-feedback')).toBeNull();
    await u.click(button);
    await waitFor(() => expect(status.textContent).toBe('检测通过'));
    expect(status.textContent).not.toContain('认证失败');
    expect(api.testModelConfiguration).toHaveBeenCalledTimes(2);
    expect(api.testModelConfiguration).toHaveBeenLastCalledWith(model);
    expect(document.querySelector('.pm-feedback')).toBeNull();
  });

  it('keeps results attached to their models across other tests, filtering and provider switches', async () => {
    models = [model, { ...model, config_id: 'second-model', display_name: '第二模型' }];
    connections.push({ ...provider, provider_id: 'p2', name: '第二网关' });
    vi.mocked(api.testModelConfiguration).mockResolvedValueOnce({ success: true }).mockRejectedValueOnce(new Error('连接超时'));
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    await u.click(screen.getByRole('button', { name: '检测 团队模型' }));
    await waitFor(() => expect(screen.getByRole('status', { name: '团队模型 检测结果' }).textContent).toBe('检测通过'));
    await u.click(screen.getByRole('button', { name: '检测 第二模型' }));
    await waitFor(() => expect(screen.getByRole('status', { name: '第二模型 检测结果' }).textContent).toContain('连接超时'));
    expect(screen.getByRole('status', { name: '团队模型 检测结果' }).textContent).toBe('检测通过');
    await u.type(screen.getByLabelText('搜索已添加模型'), '团队');
    expect(screen.queryByRole('status', { name: '第二模型 检测结果' })).toBeNull();
    await u.clear(screen.getByLabelText('搜索已添加模型'));
    expect(screen.getByRole('status', { name: '第二模型 检测结果' }).textContent).toContain('连接超时');
    await u.click(screen.getByRole('button', { name: /第二网关/ }));
    expect(screen.queryByText('检测通过')).toBeNull();
    await u.click(screen.getByRole('button', { name: /我的网关/ }));
    expect(screen.getByRole('status', { name: '团队模型 检测结果' }).textContent).toBe('检测通过');
    expect(screen.getByRole('status', { name: '第二模型 检测结果' }).textContent).toContain('连接超时');
  });

  it('invalidates results after connection or model revisions change, and when a model is removed', async () => {
    models = [model, { ...model, config_id: 'second-model', display_name: '第二模型' }];
    vi.mocked(api.testModelConfiguration).mockResolvedValue({ success: true });
    const u = user(); render(<ModelSettings onClose={vi.fn()} onChanged={vi.fn()} />); await openProvider();
    for (const name of ['团队模型', '第二模型']) {
      await u.click(screen.getByRole('button', { name: `检测 ${name}` }));
      await waitFor(() => expect(screen.getByRole('status', { name: `${name} 检测结果` }).textContent).toBe('检测通过'));
    }
    // Provider edits bump every attached model's revision on the server.
    connections = [{ ...provider, revision: 2 }];
    models = [{ ...model, revision: 2 }];
    await u.click(screen.getByRole('button', { name: '刷新模型服务' }));
    await waitFor(() => expect(screen.getByRole('status', { name: '团队模型 检测结果' }).textContent).toBe(''));
    expect(screen.queryByRole('status', { name: '第二模型 检测结果' })).toBeNull();
    await waitFor(() => expect((screen.getByRole('button', { name: '检测 团队模型' }) as HTMLButtonElement).disabled).toBe(false));
    await u.click(screen.getByRole('button', { name: '检测 团队模型' }));
    await waitFor(() => expect(screen.getByRole('status', { name: '团队模型 检测结果' }).textContent).toBe('检测通过'));
    expect(api.testModelConfiguration).toHaveBeenLastCalledWith(expect.objectContaining({ revision: 2 }));
  });
});
