import { afterEach, describe, expect, it, vi } from 'vitest';
import { randomUUID } from './randomId';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('randomUUID', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uses the native implementation in secure contexts', () => {
    const native = vi.fn(() => '00000000-0000-4000-8000-000000000000');
    vi.stubGlobal('crypto', { randomUUID: native, getRandomValues: crypto.getRandomValues.bind(crypto) });
    expect(randomUUID()).toBe('00000000-0000-4000-8000-000000000000');
    expect(native).toHaveBeenCalledOnce();
  });

  it('falls back to getRandomValues on plain HTTP where randomUUID is absent', () => {
    vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const ids = Array.from({ length: 200 }, () => randomUUID());
    for (const id of ids) expect(id).toMatch(UUID_V4);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
