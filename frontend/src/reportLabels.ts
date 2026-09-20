import legacyLabels from './reportLabels.json';

// Legacy reports lack bilingual metadata. Only presentation fields use this
// catalogue; node IDs, table data, user titles and narratives stay untouched.
export function reportLabel(item: Record<string, unknown>, field: string, locale: string): unknown {
  const language = locale.startsWith('zh') ? 'zh' : 'en';
  const translations = item[`${field}_i18n`];
  if (translations && typeof translations === 'object') {
    const translated = (translations as Record<string, unknown>)[language];
    if (typeof translated === 'string') return translated;
  }
  const value = item[field];
  if (typeof value !== 'string') return value;
  const pairs = Object.entries(legacyLabels);
  const match = pairs.find(([zh, en]) => value === zh || value === en);
  return match ? match[language === 'zh' ? 0 : 1] : value;
}
