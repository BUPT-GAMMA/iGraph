import { describe, expect, it } from 'vitest';
import { labelBudget } from './graphLabels';
describe('label zoom hysteresis', () => {
  it('keeps the budget stable during repeated small reversals', () => {
    let count = labelBudget(2, 0, 180);
    for (let i = 0; i < 60; i++) {
      count = labelBudget(i % 2 ? 2.02 : 1.98, count, 180);
      expect(count).toBe(40);
    }
  });
  it('responds to deliberate zoom in and out and respects small graphs', () => {
    expect(labelBudget(3, 40, 180)).toBe(60);
    expect(labelBudget(1, 40, 180)).toBe(20);
    expect(labelBudget(.15, 40, 180)).toBe(8);
    expect(labelBudget(8, 40, 3)).toBe(3);
    expect(labelBudget(8, 40, 0)).toBe(0);
  });
});
