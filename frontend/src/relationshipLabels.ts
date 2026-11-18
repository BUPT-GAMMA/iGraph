import type { Translate } from './i18n';

type Fields = Record<string, unknown>;
function record(value: unknown): Fields {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Fields : {};
}
const generated = /^fk_[0-9a-f]{20,64}$/i;
// Older conversation snapshots may retain only the stable FK type fingerprint.
const financeRoles = [
  { id: 'fk_464ac5706e769fd8efe7b986bec5bb7b9cf4e13c', column: 'UUID', target: 'institution_main', key: 'UUID', en: 'Investee company', zh: '被持股企业' },
  { id: 'fk_8cc261eadd35b2bfa6c388bc4a9d7e0f53ecf913', column: 'REL_SHRHLR_CUST_ID', target: 'institution_main', key: 'UUID', en: 'Corporate shareholder', zh: '企业股东' },
  { id: 'fk_9d72fe9964550b576ddfc822a3188b9a2d301b7e', column: 'REL_SHRHLR_CUST_ID', target: 'individual_info', key: 'IDV_ID', en: 'Individual shareholder', zh: '个人股东' },
];

export function relationshipDisplayName(edge: Fields, t: Translate): string | undefined {
  const attrs = record(edge.attrs), props = record(edge.properties ?? attrs.properties);
  const type = String(edge.type ?? edge.edge_type ?? props.type_name ?? attrs.type ?? '');
  if (type === 'ownership' && (props.table === 'institution_shareholder_relationship' ||
      props.target_type === 'institution_main' || props.source_type === 'institution_main')) return t('Shareholding', '持股');
  const tableView = props.schema_kind === 'foreign_key';
  if (!tableView && !generated.test(type)) return undefined;
  const fk: Fields = tableView ? { ...props, table: edge.source, ref_table: edge.target } : record(props.foreign_key);
  const columns = Array.isArray(fk.columns) ? fk.columns : [];
  const keys = Array.isArray(fk.ref_columns) ? fk.ref_columns : [];
  const role = financeRoles.find(item => item.id === type || (fk.table === 'institution_shareholder_relationship' &&
    columns.length === 1 && keys.length === 1 && columns[0] === item.column && fk.ref_table === item.target && keys[0] === item.key));
  if (role) return t(role.en, role.zh);
  if (tableView) return undefined;
  if (columns.length && columns.length === keys.length && typeof fk.ref_table === 'string') {
    return columns.join(', ') + ' → ' + fk.ref_table + '.' + keys.join(', ');
  }
  if (typeof edge.label === 'string' && edge.label && !generated.test(edge.label)) return edge.label;
  const target = props.target_type;
  return target ? t(`References ${target}`, `关联 ${target}`) : t('Foreign-key reference', '外键关联');
}
