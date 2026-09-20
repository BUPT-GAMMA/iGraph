import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { NodeProperties, nodePropertyType } from './NodeProperties';

afterEach(cleanup);
describe('instance node properties', () => {
  it('unwraps constructed rows while keeping source schema metadata out of record values', () => {
    const node = { attrs: { type: 'customers', properties: { table: 'customers', row_index: 0, data: { customer_id: 'C001', name: '张三', city: '北京' }, relational_schema: { primary_key: ['customer_id'] } } } };
    render(<NodeProperties node={node} />);
    expect(nodePropertyType(node)).toBe('customers');
    expect(screen.getByText('customer_id')).toBeTruthy();
    expect(screen.getByText('张三')).toBeTruthy();
    expect(screen.getByText('北京')).toBeTruthy();
    expect(screen.queryByText('relational_schema')).toBeNull();
  });
  it('supports ordinary graph properties, zero, false, null and nested attributes safely', () => {
    const { container } = render(<NodeProperties node={{ properties: { count: 0, enabled: false, missing: null, html: '<script>bad()</script>', nested: { city: '北京' } }, attrs: { text: 'paper text', label: 1 } }} />);
    for (const text of ['0', 'false', 'null', '<script>bad()</script>', '{city: 北京}', 'paper text']) expect(screen.getByText(text)).toBeTruthy();
    expect(container.querySelector('script')).toBeNull();
  });
  it('shows complete long text in a scrollable field while bounding large arrays', () => {
    const { rerender, container } = render(<NodeProperties node={{ attrs: { text: 'x'.repeat(5000), features: Array.from({ length: 2000 }, (_, i) => i) } }} />);
    const fullText = screen.getByLabelText('text 的完整内容');
    expect(fullText.textContent).toHaveLength(5000);
    expect(fullText.parentElement?.classList.contains('is-scrollable-text')).toBe(true);
    expect(fullText.getAttribute('tabindex')).toBe('0');
    expect(screen.getByText(/0, 1, 2, 3/).textContent).toContain('…');
    expect(screen.getByText('仅显示已加载属性；大型数组和嵌套对象以摘要显示。')).toBeTruthy();
    rerender(<NodeProperties node={{ id: 'empty' }} />);
    expect(screen.getByText('当前预览未包含其他属性。')).toBeTruthy();
  });
});
