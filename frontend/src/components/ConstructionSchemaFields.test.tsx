import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ConstructionSchemaFields } from './ConstructionSchemaFields';
import type { Dataset } from '../types';

afterEach(cleanup);
const dataset: Dataset = { dataset_id: 'd1', project_id: null, name: 'Sales', original_filename: 'sales.csv', file_type: 'csv', status: 'ready', metadata: { csv_bundle: { tables: [
  { name: 'customers', columns: ['id', 'name'], primary_key: ['id'], foreign_keys: [] },
  { name: 'orders', columns: ['order_id', 'customer_id'], primary_key: ['order_id'], foreign_keys: [{ columns: ['customer_id'], ref_table: 'customers', ref_columns: ['id'] }] },
] } } };

describe('construction schema field visualization', () => {
  it('shows PK/FK field roles and relationship mappings without instance values', () => {
    render(<ConstructionSchemaFields dataset={dataset} schema={null} graph={null} />);
    expect(screen.getAllByText('PK')).toHaveLength(2);
    expect(screen.getByText('FK')).toBeTruthy();
    expect(screen.getByText('→ customers.id')).toBeTruthy();
    expect(screen.getByLabelText('外键关系').textContent).toContain('orders.(customer_id) → customers.(id)');
  });
  it('uses source provenance to locate fields after a node type is renamed', () => {
    render(<ConstructionSchemaFields dataset={dataset} schema={null} graph={{ nodes: [{ id: 'record-1', type: 'Purchase', properties: { table: 'orders', data: { customer_id: 'secret-customer' } } }], edges: [] }} selectedType="Purchase" />);
    expect(screen.getAllByText('PK')).toHaveLength(1);
    expect(screen.getByText('order_id')).toBeTruthy();
    expect(screen.queryByText('name')).toBeNull();
    expect(screen.queryByText('secret-customer')).toBeNull();
  });
});
