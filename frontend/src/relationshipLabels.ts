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

const tableName = (value: unknown) => typeof value === 'string' ? value.trim().toLowerCase() : '';
function orderRelationship(table: string, column: string, target: string, key: string, t: Translate) {
  if (key !== column && key !== 'id') return undefined;
  if (/^orders?$/.test(table) && column === 'customer_id' && /^customers?$/.test(target)) return t('Ordering customer', '下单客户');
  if (!/^order_(items?|lines?|details?)$/.test(table)) return undefined;
  if (column === 'order_id' && /^orders?$/.test(target)) return t('Belongs to order', '所属订单');
  if (column === 'product_id' && /^products?$/.test(target)) return t('Ordered product', '订购商品');
  return undefined;
}

function referenceRole(column: string, target: string, t: Translate) {
  const words = column.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase();
  const role = words.replace(/_(?:id|uuid|key)$/, '').replace(/_/g, ' ');
  if (!role || role === words.replace(/_/g, ' ')) return undefined;
  const names: Record<string, [string, string]> = {
    buyer: ['Buyer', '买方'], seller: ['Seller', '卖方'], customer: ['Customer', '客户'], author: ['Author', '作者'],
    manager: ['Manager', '负责人'], supplier: ['Supplier', '供应商'], parent: ['Parent record', '上级记录'],
    owner: ['Owner', '所属方'], department: ['Department', '所属部门'],
  };
  if (names[role]) return t(...names[role]);
  const table = target.replace(/_/g, ' ');
  return t(`References ${table} (${role})`, `关联 ${table}（${role}）`);
}

export function relationshipDisplayName(edge: Fields, t: Translate): string | undefined {
  const attrs = record(edge.attrs), props = record(edge.properties ?? attrs.properties);
  const type = String(edge.type ?? edge.edge_type ?? props.type_name ?? attrs.type ?? '');
  if (type === 'ownership' && (props.table === 'institution_shareholder_relationship' ||
      props.target_type === 'institution_main' || props.source_type === 'institution_main')) return t('Shareholding', '持股');
  const tableView = props.schema_kind === 'foreign_key';
  if (!tableView && !generated.test(type)) {
    if (/^order_(items?|lines?|details?)$/.test(type) && (Array.isArray(props.row_endpoints) ||
        [tableName(props.source_type), tableName(props.target_type)].sort().join(',').match(/^orders?,products?$/))) {
      return t('Order item', '订单商品');
    }
    return undefined;
  }
  const fk: Fields = tableView ? { ...props, table: edge.source, ref_table: edge.target } : record(props.foreign_key);
  const columns = Array.isArray(fk.columns) ? fk.columns : [];
  const keys = Array.isArray(fk.ref_columns) ? fk.ref_columns : [];
  const role = financeRoles.find(item => item.id === type || (fk.table === 'institution_shareholder_relationship' &&
    columns.length === 1 && keys.length === 1 && columns[0] === item.column && fk.ref_table === item.target && keys[0] === item.key));
  if (role) return t(role.en, role.zh);
  if (columns.length === 1 && keys.length === 1) {
    const business = orderRelationship(tableName(fk.table), tableName(columns[0]), tableName(fk.ref_table), tableName(keys[0]), t);
    if (business) return business;
    if (typeof columns[0] === 'string' && typeof fk.ref_table === 'string') {
      const named = referenceRole(columns[0], fk.ref_table, t);
      if (named) return named;
    }
  }
  if (tableView) return undefined;
  if (columns.length && columns.length === keys.length && typeof fk.ref_table === 'string') {
    return columns.join(', ') + ' → ' + fk.ref_table + '.' + keys.join(', ');
  }
  if (typeof edge.label === 'string' && edge.label && !generated.test(edge.label)) return edge.label;
  const target = props.target_type;
  return target ? t(`References ${target}`, `关联 ${target}`) : t('Foreign-key reference', '外键关联');
}
