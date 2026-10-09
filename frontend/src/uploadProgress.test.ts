import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { bindRequestAccount, setUserId, uploadFile } from './api';

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload = { onprogress: undefined as any };
  onload?: () => void; onerror?: () => void; onabort?: () => void; ontimeout?: () => void;
  status = 200; responseText = ''; headers: Record<string, string> = {}; sent = false; withCredentials = false;
  constructor() { FakeXHR.instances.push(this); }
  open = vi.fn();
  setRequestHeader(key: string, value: string) { this.headers[key] = value; }
  getResponseHeader() { return null; }
  send() { this.sent = true; }
  abort() { this.onabort?.(); }
  progress(loaded: number) { this.upload.onprogress?.({ lengthComputable: true, loaded, total: 100 }); }
  respond(data: unknown, status = 200) { this.status = status; this.responseText = JSON.stringify(data); this.onload?.(); }
}
let unbind = () => {};
beforeEach(() => { localStorage.clear(); setUserId('account-a'); unbind = bindRequestAccount('account-a'); FakeXHR.instances = []; vi.stubGlobal('XMLHttpRequest', FakeXHR); });
afterEach(() => { unbind(); vi.unstubAllGlobals(); });
const file = () => new File(['x'], 'data.csv');
it('keeps registration fields and waits for the server after upload reaches 100%', async () => {
  const progress = vi.fn(); const settled = vi.fn();
  const result = uploadFile('session', file(), undefined, progress).then(value => { settled(); return value; });
  const xhr = FakeXHR.instances[0];
  xhr.progress(42); xhr.progress(100);
  await Promise.resolve();
  expect(progress.mock.calls).toEqual([[42], [100]]);
  expect(settled).not.toHaveBeenCalled();
  expect(xhr.headers['X-User-ID']).toBe('admin');
  const payload = { success: true, file_token: 'token', filename: 'data.csv', dataset: { dataset_id: 'dataset' }, construction_required: true };
  xhr.respond(payload);
  expect(await result).toEqual(payload);
});
it('ignores a legacy account selection for uploads', async () => {
  setUserId('account-b');
  const result = uploadFile('session-a', file(), undefined, vi.fn());
  expect(FakeXHR.instances[0].headers['X-User-ID']).toBe('admin');
  FakeXHR.instances[0].respond({ success: true, file_token: 'token', filename: 'data.csv' });
  await expect(result).resolves.toMatchObject({ file_token: 'token' });
});it('rejects a late success after the rendered account changes', async () => {
  const result = uploadFile('session', file(), undefined, vi.fn());
  const rejected = expect(result).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
  const next = bindRequestAccount('account-b'); setUserId('account-b');
  FakeXHR.instances[0].respond({ success: true, file_token: 'old-token' });
  await rejected; next();
});
it('detects an account switch away and back before the response', async () => {
  const result = uploadFile('session', file(), undefined, vi.fn());
  const rejected = expect(result).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
  bindRequestAccount('account-b'); const next = bindRequestAccount('account-a');
  FakeXHR.instances[0].respond({ success: true, file_token: 'old-token' });
  await rejected; next();
});
it('cancels a transfer and ignores its late completion', async () => {
  const controller = new AbortController();
  const result = uploadFile('session', file(), controller.signal, vi.fn());
  const rejected = expect(result).rejects.toMatchObject({ name: 'AbortError' });
  controller.abort(); FakeXHR.instances[0].respond({ success: true, file_token: 'late' });
  await rejected;
});
it('preserves structured backend errors for failed uploads', async () => {
  const result = uploadFile('session', file(), undefined, vi.fn());
  const rejected = expect(result).rejects.toMatchObject({ status: 413, code: 'UPLOAD_TOO_LARGE' });
  FakeXHR.instances[0].respond({ detail: { code: 'UPLOAD_TOO_LARGE', message: '文件过大' } }, 413);
  await rejected;
});
it('rejects an invalid successful response', async () => {
  const result = uploadFile('session', file(), undefined, vi.fn());
  const rejected = expect(result).rejects.toThrow('上传未成功');
  FakeXHR.instances[0].respond({ success: true, file_token: 42 });
  await rejected;
});
