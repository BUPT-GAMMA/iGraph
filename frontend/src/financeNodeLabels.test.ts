import { describe, expect, it } from 'vitest';
import { graphIndex, previewGraph } from './graphPreview';
import { financeNodeLabel } from './financeNodeLabels';
import type { Translate } from './i18n';
const zh: Translate = (_, chinese) => chinese;
const en: Translate = english => english;
const company = { id: 'row_company', label: 'institution_main · #1', attrs: { type: 'institution_main', properties: { table: 'institution_main', row_index: 0, data: { CHN_NM: '测试公司', ENG_NM: 'Test Company', UUID: 'SYN-ENT-000001' } } } };
const person = { id: 'row_person', attrs: { type: 'individual_info', properties: { table: 'individual_info', row_index: 0, data: { IDV_NM: '张三' } } } };
const share = { id: 'row_share', attrs: { type: 'institution_shareholder_relationship', properties: { table: 'institution_shareholder_relationship', row_index: 0, data: { INST_CHN_NM: '测试公司' } } } };
describe('financial graph node names', () => {
  it('uses source names and localized shareholding labels without changing identities or evidence', () => {
    const data = { nodes: [company, person, share], edges: [{ source: 'row_share', target: 'row_company' }] };
    const original = JSON.stringify(data);
    const cn = graphIndex(data, zh), english = graphIndex(data, en);
    expect([...cn.nodes.values()].map(node => node.label)).toEqual(['测试公司', '张三', '持股']);
    expect([...english.nodes.values()].map(node => node.label)).toEqual(['Test Company', '张三', 'Shareholding']);
    expect([...english.nodes.keys()]).toEqual([...cn.nodes.keys()]);
    expect(english.edges).toEqual(cn.edges);
    expect(JSON.stringify(data)).toBe(original);
    for (const query of ['测试公司', 'Test Company', 'SYN-ENT-000001']) {
      expect([...previewGraph(english, [], query, true, false, '', 0).matches]).toEqual(['row_company']);
    }
    expect([...previewGraph(cn, [], 'Shareholding', true, false, '', 0).matches]).toEqual(['row_share']);
  });
  it('handles construction rows, missing translations, aliases and unrelated graphs', () => {
    expect(financeNodeLabel({ id: 'x', type: 'institution_info', properties: { IDV_NM: '李四', IDV_ENG_NM: 'Li Si' } }, en)).toBe('Li Si');
    expect(financeNodeLabel({ id: 'x', type: 'institution_main', properties: { CHN_NM: '公司', ENG_NM: '  ' } }, en)).toBe('公司');
    expect(financeNodeLabel({ id: 'x', type: 'share' }, zh)).toBe('持股');
    expect(financeNodeLabel({ id: 'x', type: 'institution_main', properties: { UUID: 'SYN-ENT-000044', ENG_NM: 'Synthetic 工程技术 Company 00044' } }, en)).toBe('Synthetic Engineering Technology Company 00044');
    expect(financeNodeLabel({ id: 'x', type: 'institution_main', properties: { UUID: 'real', ENG_NM: 'Synthetic 工程技术 Company 00044' } }, en)).toBe('Synthetic 工程技术 Company 00044');
    expect(financeNodeLabel({ id: 'x', type: 'character', properties: { name: '王语嫣' } }, en)).toBeUndefined();
    expect(graphIndex({ nodes: [{ id: 'a', label: '王语嫣' }], edges: [] }, en).nodes.get('a')?.label).toBe('王语嫣');
    expect(graphIndex({ visualization_kind: 'schema', nodes: [share], edges: [] }, en).nodes.get('row_share')?.label).toBeUndefined();
  });
});
