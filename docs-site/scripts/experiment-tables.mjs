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

export function experimentTableHtml(block, locale) {
  const caption = `${block.title[locale]} · SR (%) ↑`;
  const groups = block.groups.map(group => `<th scope="colgroup" colspan="${group.tasks.length}">${escape(group.label)}</th>`).join('');
  const names = block.groups.map(group => `<th scope="colgroup" colspan="${group.tasks.length}" class="experiment-graph-name">${escape(group.name.en)}</th>`).join('');
  const tasks = block.groups.flatMap(group => group.tasks.map(task => `<th scope="col" title="${escape(task.name[locale])}">${escape(task.label)}</th>`)).join('');
  const rows = block.rows.map(row => `<tr${row.harness === 'iGraph' ? ' class="experiment-igraph"' : ''}><th scope="row">${escape(row.harness)}</th>${row.scores.map((_, column) => `<td>${scoreHtml(block, row, column, locale)}</td>`).join('')}</tr>`).join('');
  return `<div class="table-scroll experiment-scroll" role="region" aria-label="${escape(caption)}" tabindex="0"><table class="experiment-table"><caption>${escape(caption)}</caption><thead><tr><th rowspan="3" scope="col" class="experiment-base"><span>${escape(block.baseModel)}</span><small>Agent harness</small></th>${groups}</tr><tr>${names}</tr><tr>${tasks}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function experimentTableMarkdown(block, locale) {
  const columns = block.groups.flatMap(group => group.tasks.map(task => `${group.label} · ${task.label}`));
  const header = ['Agent harness', ...columns];
  const rows = block.rows.map(row => [row.harness, ...row.scores.map((_, column) => scoreHtml(block, row, column, locale))]);
  // HTML emphasis keeps the underline in Markdown readers, including copied pages.
  return `**${block.title[locale]} · ${block.baseModel} · SR (%) ↑**\n\n` + [header, header.map(() => '---'), ...rows].map(row => `| ${row.join(' | ')} |`).join('\n');
}
