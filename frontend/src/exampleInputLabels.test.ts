import { describe, it, expect } from 'vitest';
import { localizeInputLabel } from './userCases';
describe('bundled source labels', () => {
  it('switches example names without translating user names', () => {
    expect(localizeInputLabel('金融风控（CSV 原始三表）', en => en)).toBe('Financial risk (three source CSV tables)');
    expect(localizeInputLabel('StackExchange (SQL source)', (_, zh) => zh)).toBe('StackExchange（SQL 原始数据）');
    expect(localizeInputLabel('My renamed dataset', en => en)).toBe('My renamed dataset');
  });
});
