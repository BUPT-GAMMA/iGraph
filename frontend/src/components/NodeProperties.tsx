import { constructionProperties } from '../constructionProperties';
import { useI18n } from '../i18n';
import './NodeProperties.css';

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const isScrollableText = (value: unknown): value is string => typeof value === 'string' && value.length > 240;

// Graph previews may contain long text or feature vectors; keep inspection bounded.
function displayValue(value: unknown, depth = 0): string {
  if (value === null) return 'null';
  if (typeof value !== 'object') {
    const text = String(value);
    return text.length > 1000 ? `${text.slice(0, 1000)}…` : text;
  }
  if (depth >= 3) return Array.isArray(value) ? '[…]' : '{…}';
  if (Array.isArray(value)) return `[${value.slice(0, 20).map(item => displayValue(item, depth + 1)).join(', ')}${value.length > 20 ? ', …' : ''}]`;
  const entries = Object.entries(record(value));
  return `{${entries.slice(0, 20).map(([key, item]) => `${key}: ${displayValue(item, depth + 1)}`).join(', ')}${entries.length > 20 ? ', …' : ''}}`;
}

export function nodePropertyType(node: Record<string, unknown>): unknown {
  return node.type ?? node.category ?? record(node.attrs).type;
}

export function NodeProperties({ node }: { node: Record<string, unknown> }) {
  const { t } = useI18n();
  const attrs = record(node.attrs);
  const properties = constructionProperties({ properties: attrs.properties ?? node.properties });
  const groups = [
    { title: t('Business properties', '业务属性'), entries: Object.entries(properties) },
    { title: t('Other features', '其他特征'), entries: Object.entries(attrs).filter(([key]) => !['properties', 'type'].includes(key)) },
  ].filter(group => group.entries.length > 0);
  return <section className="node-properties" aria-label={t('Node properties', '节点属性')}>
    {groups.length ? <>
      {groups.map(group => <div key={group.title}><h4>{group.title}</h4><dl>{group.entries.slice(0, 100).map(([key, value]) => {
        const scrollable = isScrollableText(value);
        return <div className={scrollable ? 'is-scrollable-text' : undefined} key={key}><dt>{key}</dt><dd tabIndex={scrollable ? 0 : undefined}
          aria-label={scrollable ? t(`Full value of ${key}`, `${key} 的完整内容`) : undefined}>{scrollable ? value : displayValue(value)}</dd></div>;
      })}</dl>{group.entries.length > 100 && <p>{t('Only the first 100 fields are shown.', '仅展示前 100 个字段。')}</p>}</div>)}
      <p>{t('Loaded properties only; large arrays and nested objects are summarized.', '仅显示已加载属性；大型数组和嵌套对象以摘要显示。')}</p>
    </> : <p>{t('No additional properties in the loaded preview.', '当前预览未包含其他属性。')}</p>}
  </section>;
}
