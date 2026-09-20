import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MarkdownContent } from './MarkdownContent';

afterEach(cleanup);

describe('MarkdownContent', () => {
  it('preserves underscores in identifiers and supports nested emphasis and lists', () => {
    const { container } = render(<MarkdownContent content={'train_val_test and __bold__ and **strong *nested***\n\n3. first\n   - child\n4. second\n\n> quoted text'} />);
    expect(container.textContent).toContain('train_val_test');
    expect(container.querySelector('strong em')?.textContent).toBe('nested');
    expect(container.querySelector('ol')?.start).toBe(3);
    expect(container.querySelector('ol li ul li')?.textContent).toBe('child');
    expect(container.querySelector('blockquote')?.textContent).toContain('quoted text');
  });
  it('renders images as images, with failure feedback and no referrer', () => {
    render(<MarkdownContent content={'![Graph preview](https://example.com/graph.png "Graph")'} />);
    const img = screen.getByRole('img', { name: 'Graph preview' });
    expect(img.getAttribute('src')).toBe('https://example.com/graph.png');
    expect(img.getAttribute('referrerpolicy')).toBe('no-referrer');
    fireEvent.error(img);
    expect(screen.getByText(/图片不可用/)).toBeTruthy();
    expect(screen.getByRole('link', { name: '打开图片' }).getAttribute('rel')).toContain('noreferrer');
  });
  it.each(['javascript:alert(1)', 'data:text/html,bad', 'file:///etc/passwd', 'vbscript:msgbox(1)'])('blocks executable or local URL %s', url => {
    const { container } = render(<MarkdownContent content={`[bad](${url})\n\n![bad image](${url})`} />);
    expect(container.querySelector('a')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });
  it('never executes raw HTML or entity-encoded script links', () => {
    const { container } = render(<MarkdownContent content={'<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript&#58;alert%281%29)'} />);
    expect(container.querySelector('script, img, a')).toBeNull();
  });
  it('supports GFM tables with escaped pipes, task lists, and safe links', () => {
    const { container } = render(<MarkdownContent content={'| key | value |\n| --- | --- |\n| train_val_test | a\\|b |\n\n- [x] done\n- [ ] pending\n\n[docs](https://example.com/a_(b))'} />);
    expect(container.querySelectorAll('tbody td')).toHaveLength(2);
    expect(container.querySelectorAll('tbody td')[1].textContent).toBe('a|b');
    expect(container.querySelector('table')?.parentElement?.className).toBe('markdown-table-scroll');
    expect(screen.getAllByRole('checkbox').every(input => (input as HTMLInputElement).disabled)).toBe(true);
    expect(screen.getByRole('link', { name: 'docs' }).getAttribute('href')).toBe('https://example.com/a_(b)');
  });
  it('keeps incomplete streamed code readable and resolves syntax as more content arrives', () => {
    const { container, rerender } = render(<MarkdownContent content={'```python\ncheck_status(job_id="partial'} />);
    expect(container.querySelector('pre code')?.textContent).toBe('check_status(job_id="partial\n');
    rerender(<MarkdownContent content={'```python\ncheck_status(job_id="partial")\n```\n\n**Ready**'} />);
    expect(container.querySelector('pre code')?.textContent).toBe('check_status(job_id="partial")\n');
    expect(screen.getByText('Ready').tagName).toBe('STRONG');
  });
});
