import { describe, expect, it } from 'vitest';
import { metricNumber, metricSegments } from './mlMetrics';

describe('ML measurements', () => {
  it.each([null, undefined, '', '  ', false, true, [], [0], {}, NaN, Infinity, -Infinity, 'NaN', 'Infinity', '0x10', 'unknown'])('rejects missing or invalid measurement %j', value => {
    expect(metricNumber(value)).toBeNull();
  });
  it.each([[0, 0], ['0', 0], [' 0.25 ', .25], ['1e-4', .0001], [-.1, -.1], ['.5', .5]])('preserves measurement %s', (value, expected) => {
    expect(metricNumber(value)).toBe(expected);
  });
  it('breaks curves at missing measurements and keeps valid zeroes', () => {
    expect(metricSegments([{ epoch: 1, loss: 1 }, { epoch: 2, loss: null }, { epoch: 3, loss: 0 }, { epoch: 4, loss: .1 }], 'loss')).toEqual([
      [{ epoch: 1, value: 1 }], [{ epoch: 3, value: 0 }, { epoch: 4, value: .1 }],
    ]);
    expect(metricSegments([{ epoch: null, loss: .5 }, { epoch: 1, loss: '' }, { epoch: 2, loss: false }], 'loss')).toEqual([]);
  });
});
