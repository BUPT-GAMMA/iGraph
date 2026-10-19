import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CsvBundleDialog } from './CsvBundleDialog';

afterEach(cleanup);

function files() {
  return [
    new File(['company_id,name\nC1,Company\n'], 'companies.csv'),
    new File(['person_id,name\nP1,Person\n'], 'people.csv'),
    new File(['id,company_id,person_id,share\nr1,C1,P1,25%\n'], 'holdings.csv'),
  ];
}

describe('CSV node and edge table configuration', () => {
  it('keeps an explicitly cleared primary key empty and submits an edge table', async () => {
    const onSubmit = vi.fn();
    const { container } = render(<CsvBundleDialog files={files()} onCancel={vi.fn()} onSubmit={onSubmit} />);
    const submit = screen.getByRole('button', { name: '上传 3 张表' });
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    // people.person_id needs explicit selection because the name is not inferred.
    const cards = container.querySelectorAll<HTMLElement>('.csv-table-card');
    fireEvent.click(within(cards[1]).getByRole('checkbox', { name: 'person_id' }));
    fireEvent.click(within(cards[2]).getByRole('checkbox', { name: 'id' }));
    // A relationship to companies is suggested. Add the other endpoint to people.
    fireEvent.click(within(cards[2]).getByRole('button', { name: '添加关系' }));
    const entries = cards[2].querySelectorAll<HTMLElement>('.csv-fk-entry');
    const added = within(entries[entries.length - 1]);
    fireEvent.change(added.getByLabelText('目标表'), { target: { value: 'people' } });
    const source = added.getByLabelText('来源列') as HTMLSelectElement;
    for (const option of source.options) option.selected = option.value === 'person_id';
    fireEvent.change(source);
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledOnce();
    expect(onSubmit.mock.calls[0][1].tables[2]).toMatchObject({ primary_key: [], foreign_keys: [
      { columns: ['company_id'], ref_table: 'companies' },
      { columns: ['person_id'], ref_table: 'people' },
    ] });
    expect(screen.getByText(/关系表：配置两组外键列/)).toBeTruthy();
  });

  it('asks for endpoint configuration instead of uploading a keyless single table', async () => {
    const onSubmit = vi.fn();
    render(<CsvBundleDialog files={[new File(['id,name\n1,Alice\n'], 'people.csv')]} onCancel={vi.fn()} onSubmit={onSubmit} />);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'id' }));
    fireEvent.click(screen.getByRole('button', { name: '上传 1 张表' }));
    expect(screen.getByRole('alert').textContent).toContain('请设置主键以生成节点，或配置两组外键列以生成边');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
