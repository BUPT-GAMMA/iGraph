import { constructionProperties } from './constructionProperties';
import type { Translate } from './i18n';

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (data: Record<string, unknown>, keys: string[]) => keys.map(key => data[key]).find(value => typeof value === 'string' && value.trim()) as string | undefined;

// The bundled synthetic CSV has mixed-language ENG_NM values. Translate only
// its exact template; never rewrite real companies' supplied legal names.
const SYNTHETIC_INDUSTRIES: Record<string, string> = {
  '城市发展': 'Urban Development', '高端装备': 'Advanced Equipment',
  '生物医药': 'Biopharmaceutical', '智能设备': 'Smart Devices',
  '供应链': 'Supply Chain', '数字科技': 'Digital Technology',
  '金融服务': 'Financial Services', '清洁能源': 'Clean Energy',
  '产业投资': 'Industrial Investment', '工程技术': 'Engineering Technology',
};
function englishCompanyName(value: string | undefined, data: Record<string, unknown>) {
  if (!value || !/^SYN-ENT-\d+$/.test(String(data.UUID ?? ''))) return value;
  const match = /^Synthetic (.+) Company (\d+)$/.exec(value.trim());
  return match && SYNTHETIC_INDUSTRIES[match[1]]
    ? `Synthetic ${SYNTHETIC_INDUSTRIES[match[1]]} Company ${match[2]}` : value;
}

function financeNode(node: Record<string, unknown>) {
  const attrs = record(node.attrs);
  const raw = record(attrs.properties ?? node.properties);
  const data = constructionProperties({ properties: raw });
  const type = String(raw.table ?? attrs.type ?? node.type ?? '').toLowerCase();
  const kind = type === 'institution_main' ? 'company'
    : ['individual_info', 'institution_info'].includes(type) ? 'person'
    : ['institution_shareholder_relationship', 'share'].includes(type) ? 'share' : undefined;
  return { kind, data };
}

/** Display aliases never replace node IDs, original properties, or topology. */
export function financeNodeLabel(node: Record<string, unknown>, t: Translate): string | undefined {
  const { kind, data } = financeNode(node);
  if (kind === 'share') return t('Shareholding', '持股');
  if (!kind) return undefined;
  const zh = text(data, kind === 'company' ? ['CHN_NM', 'name_zh', 'name', 'LCL_NM'] : ['IDV_NM', 'name_zh', 'name']);
  const suppliedEn = text(data, kind === 'company' ? ['ENG_NM', 'name_en'] : ['IDV_ENG_NM', 'ENG_NM', 'name_en']);
  const en = kind === 'company' ? englishCompanyName(suppliedEn, data) : suppliedEn;
  // Proper names without a supplied translation retain their source spelling.
  return t(en?.trim() || zh?.trim() || '', zh?.trim() || en?.trim() || '') || undefined;
}

export function graphNodeLabel(node: Record<string, unknown>, t: Translate): string {
  return financeNodeLabel(node, t) ?? String(node.label ?? node.id ?? '');
}

export function financeNodeTypeLabel(node: Record<string, unknown>, t: Translate): string | undefined {
  const { kind } = financeNode(node);
  return kind === 'company' ? t('Company', '公司') : kind === 'person' ? t('Person', '个人') : kind === 'share' ? t('Shareholding', '持股') : undefined;
}

export function financeNodeSearchNames(node: Record<string, unknown>): string[] {
  const { kind, data } = financeNode(node);
  if (!kind) return [];
  const keys = kind === 'company' ? ['CHN_NM', 'ENG_NM', 'LCL_NM', 'name', 'name_zh', 'name_en', 'UUID']
    : kind === 'person' ? ['IDV_NM', 'IDV_ENG_NM', 'ENG_NM', 'name', 'name_zh', 'name_en', 'IDV_ID'] : [];
  return [...(kind === 'share' ? ['持股', 'Shareholding'] : []), ...keys.flatMap(key => typeof data[key] === 'string' && data[key].trim() ? [data[key] as string] : [])];
}
