const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const text = (locale, zh, en) => locale === 'zh' ? zh : en;

// Rank distinct scores within each task column; ties share the same rank.
export function scoreRank(rows, column, value) {
  return [...new Set(rows.map(row => row.scores[column]))].sort((a, b) => b - a).indexOf(value) + 1;
}

function scoreHtml(block, row, column, locale) {
  const value = row.scores[column];
  const score = value.toFixed(2);
  const rank = scoreRank(block.rows, column, value);
  if (rank === 1) return `<strong title="${text(locale, '最高分', 'Highest score')}">${score}</strong>`;
  if (rank === 2) return `<u title="${text(locale, '次高分', 'Second-highest score')}">${score}</u>`;
  return score;
}

export function experimentTableHtml(block, locale, id) {
  const caption = `${block.title[locale]} · SR (%) ↑`;
  const columns = block.groups.flatMap(group => group.tasks.map((task, index) => ({task, group: group.label, first: index === 0})));
  const groupAttributes = column => `data-graph="${escape(column.group)}"${column.first ? ' class="experiment-group-start"' : ''}`;
  const groups = block.groups.map(group => `<th scope="colgroup" colspan="${group.tasks.length}" data-graph="${escape(group.label)}" class="experiment-group-start" title="${escape(group.name.en)}"><span class="experiment-group-label">${escape(group.label)}</span><span class="experiment-group-name">${escape(group.name[locale])}</span></th>`).join('');
  const tasks = columns.map(column => `<th scope="col" ${groupAttributes(column)} title="${escape(column.task.name[locale])}">${escape(column.task.label)}</th>`).join('');
  const rows = block.rows.map(row => `<tr${row.harness === 'iGraph' ? ' class="experiment-igraph"' : ''}><th scope="row">${escape(row.harness)}</th>${columns.map((column, index) => `<td ${groupAttributes(column)}>${scoreHtml(block, row, index, locale)}</td>`).join('')}</tr>`).join('');
  const selector = `<fieldset class="experiment-selector"><legend class="sr-only">${text(locale, '选择图类型', 'Select graph type')} · ${escape(block.title[locale])}</legend>${block.groups.map((group, index) => `<label><input class="sr-only" type="radio" name="experiment-${escape(id)}" value="${escape(group.label)}"${index === 0 ? ' checked' : ''}><span>${escape(group.label)}</span></label>`).join('')}</fieldset>`;
  return `<div class="experiment-card"><div class="experiment-panel"><div class="experiment-toolbar"><div class="experiment-model"><span>${text(locale, '基座模型', 'Base model')}</span><b>${escape(block.baseModel)}</b></div><span class="experiment-metric">SR <small>(%)</small> <span aria-hidden="true">↑</span></span></div>${selector}<div class="experiment-scroll" role="region" aria-label="${escape(caption)}" tabindex="0"><table class="experiment-table"><caption class="sr-only">${escape(caption)}</caption><thead><tr><th rowspan="2" scope="col" class="experiment-harness">Agent harness</th>${groups}</tr><tr>${tasks}</tr></thead><tbody>${rows}</tbody></table></div><div class="experiment-legend"><span><strong>${text(locale, '最高分', 'Best')}</strong><u>${text(locale, '次高分', 'Second best')}</u></span><span>${text(locale, '数值越高越好', 'Higher is better')}</span></div></div></div>`;
}

export function experimentTableMarkdown(block, locale) {
  const columns = block.groups.flatMap(group => group.tasks.map(task => `${group.label} · ${task.label}`));
  const header = ['Agent harness', ...columns];
  const rows = block.rows.map(row => [row.harness, ...row.scores.map((_, column) => scoreHtml(block, row, column, locale))]);
  // HTML emphasis keeps the underline in Markdown readers, including copied pages.
  return `**${block.title[locale]} · ${block.baseModel} · SR (%) ↑**\n\n` + [header, header.map(() => '---'), ...rows].map(row => `| ${row.join(' | ')} |`).join('\n');
}
