import { describe, expect, it } from 'vitest';
import { constructionProperties } from './constructionProperties';

describe('construction business property access', () => {
  it('preserves literal legacy fields and nested JSON values', () => {
    const properties = { data: { phone: '123' }, table: 'ordinary value', phone: '456' };
    expect(constructionProperties({ properties })).toEqual(properties);
  });
  it('reads automatic rows without exposing schema fields or interpreting paths', () => {
    const data = JSON.parse('{"table":"business","data":{},"a.b":null,"__proto__":"literal"}');
    expect(constructionProperties({ properties: { table: 'customers', row_index: 0, data } })).toEqual(data);
    expect({}.hasOwnProperty('literal')).toBe(false);
  });
  it('handles empty row data and bridge-edge data', () => {
    expect(constructionProperties({ properties: { table: 'customers', row_index: 0, data: {} } })).toEqual({});
    expect(constructionProperties({ source: 'a', target: 'b', properties: {
      table: 'orders', row_index: 0, data: { quantity: 2 }, row_endpoints: [],
    } })).toEqual({ quantity: 2 });
  });
  it('shows relocated values under their business aliases', () => {
    const edge = { source: 'a', target: 'b', properties: { weight: 1, relocated_attributes: [
      { row_id: 'a', column: 'phone', name: 'contact', value: '123' },
      { row_id: 'b', column: 'age', value: 29 },
    ] } };
    expect(constructionProperties(edge)).toEqual({ weight: 1, contact: '123', age: 29 });
    expect(edge.properties.relocated_attributes).toHaveLength(2);
  });
  it('keeps both conflicting endpoint values visible', () => {
    expect(constructionProperties({ source: 'a', target: 'b', properties: { relocated_attributes: [
      { row_id: 'a', column: 'phone', value: '123' }, { row_id: 'b', column: 'phone', value: '456' },
    ] } })).toEqual({ phone: ['123', '456'] });
  });
});
