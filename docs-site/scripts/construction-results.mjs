const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const text = (locale, zh, en) => locale === 'zh' ? zh : en;
const count = value => value.toLocaleString('en-US');

export function selectedConstructionResults(block) {
  return block.datasets.map(dataset => {
    const result = dataset.results.find(result => result.source === dataset.selectedSource);
    if (!result) throw new Error(`Missing selected construction result: ${dataset.name}`);
    return {name: dataset.name, realRecords: dataset.realRecords, ...result};
  });
}

export function constructionResultsHtml(block, locale) {
  const headings = [text(locale, '数据集', 'Dataset'), text(locale, '真实记录数', 'Real records'), text(locale, '优化查询集', 'Optimization queries'), text(locale, '留出查询集', 'Held-out queries')];
  const gain = value => `<span class="construction-gain">${value.toFixed(2)}<small>%</small></span>`;
  const rows = selectedConstructionResults(block).map(row => `<tr><th scope="row">${escape(row.name)}<small class="construction-mobile-count">${count(row.realRecords)} ${text(locale, '条真实记录', 'real records')}</small></th><td class="construction-record-count">${count(row.realRecords)}</td><td>${gain(row.optimizationCostReduction)}</td><td>${gain(row.holdoutCostReduction)}</td></tr>`).join('');
  return `<div class="experiment-card construction-results"><div class="experiment-toolbar"><div class="experiment-model"><span>${text(locale, '对比基线', 'Baseline')}</span><b>${escape(block.baseline)}</b></div><span class="experiment-metric">${text(locale, '成本下降', 'Cost reduction')} <small>(%)</small> ↑</span></div><div class="experiment-scroll" role="region" aria-label="${escape(block.title[locale])}" tabindex="0"><table class="construction-results-table"><caption class="sr-only">${escape(block.title[locale])}</caption><thead><tr>${headings.map((heading, index) => `<th scope="col"${index === 1 ? ' class="construction-record-count"' : ''}>${escape(heading)}${index > 1 ? `<small>${text(locale, '成本下降', 'Cost reduction')}</small>` : ''}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div><div class="experiment-legend"><span>${text(locale, '按数据集选取最优结果', 'Best supplied result per dataset')}</span><span>${text(locale, '下降幅度越大越好', 'Larger reduction is better')}</span></div></div>`;
}

export function constructionResultsMarkdown(block, locale) {
  const headers = [text(locale, '数据集', 'Dataset'), text(locale, '真实记录数', 'Real records'), text(locale, '优化查询集成本下降', 'Optimization query cost reduction'), text(locale, '留出查询集成本下降', 'Held-out query cost reduction')];
  const rows = selectedConstructionResults(block).map(row => [row.name, count(row.realRecords), `${row.optimizationCostReduction.toFixed(2)}%`, `${row.holdoutCostReduction.toFixed(2)}%`]);
  return `**${block.title[locale]} · ${text(locale, '对比基线', 'Baseline')}: ${block.baseline}**\n\n` + [headers, headers.map(() => '---'), ...rows].map(row => `| ${row.join(' | ')} |`).join('\n');
}
