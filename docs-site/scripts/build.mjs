import { readFile, writeFile, mkdir, copyFile, rm } from 'node:fs/promises';
import { dirname, join, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { diagramNames, diagramTitle, renderDiagram } from './diagrams.mjs';
import { loadPages, toolsIn } from './content.mjs';
import { renderCover, renderCloudFallback } from './cover.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const out = join(root, 'dist');
// Only this builder's generated directory may be removed, never project data.
if (relative(resolve(root), resolve(out)) !== 'dist') throw new Error('Unsafe output directory');
await rm(out, { recursive: true, force: true });
const pages = await loadPages(root);
const updated = '2026-09-12';
const origin = process.env.SITE_URL?.replace(/\/$/, '') || '';
if (origin) {
  const url = new URL(origin);
  if (url.protocol !== 'https:' || url.pathname !== '/' || url.search || url.hash || url.username || url.password) throw new Error('SITE_URL must be an HTTPS origin without a path or credentials.');
}
const groups = { start: ['开始使用', 'Get started'], build: ['构图智能体', 'Construction agent'], engine: ['Graph Engine', 'Graph Engine'], reference: ['接口与配置', 'API & configuration'], ship: ['部署与维护', 'Deploy & maintain'] };
const text = (locale, zh, en) => locale === 'zh' ? zh : en;
const esc = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const path = (locale, slug) => `/${locale}/${slug}/`;
const icon = name => {
  const shapes = {
    graph: '<circle cx="5" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="m7 11 9-5M7 13l9 5"/>',
    sparkles: '<path d="m10 3 2.5 6.5L19 12l-6.5 2.5L10 21l-2.5-6.5L1 12l6.5-2.5L10 3ZM19 2v6m-3-3h6m-1 13v4m-2-2h4"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M15 8V4H4v11h4"/>',
    terminal: '<path d="m6 7 5 5-5 5m7 0h5"/><rect x="2" y="3" width="20" height="18" rx="3"/>',
    code: '<path d="m7 7-5 5 5 5m10-10 5 5-5 5m-7 5 4-20"/>',
    cloud: '<path d="M7 18H6a4 4 0 0 1-1-8 7 7 0 0 1 13-2 5 5 0 0 1 0 10h-1m-5 3V11m-4 4 4-4 4 4"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"/>',
    moon: '<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    close: '<path d="m6 6 12 12M6 18 12-12"/>',
    globe: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    book: '<path d="M12 5c-3-3-8-3-10-2v16c3-1 7-1 10 2m0-16c3-3 8-3 10-2v16c-3-1-7-1-10 2V5Z"/>',
    file: '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 13h6m-6 4h6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name] || shapes.book}</svg>`;
};
function diagram(name, locale) {
  const title=diagramTitle(name,locale);
  return `<figure class="tech-diagram"><div class="diagram-toolbar"><span>${esc(title)}</span><a class="diagram-light" href="/diagrams/${name}-${locale}.svg" target="_blank" rel="noreferrer">${text(locale,'查看大图','Open diagram')} ↗</a><a class="diagram-dark" href="/diagrams/${name}-${locale}-dark.svg" target="_blank" rel="noreferrer">${text(locale,'查看大图','Open diagram')} ↗</a></div><div class="diagram-scroll" tabindex="0" role="region" aria-label="${esc(title)}"><img class="diagram-light" src="/diagrams/${name}-${locale}.svg" alt="${esc(title)}"><img class="diagram-dark" src="/diagrams/${name}-${locale}-dark.svg" alt="${esc(title)}"></div></figure>`;
}
function toolHtml(tool, locale) {
  const parameters=tool.parameters.map(param=>`<tr><td><code>${esc(param.name)}</code>${param.required?`<small class="param-required">${text(locale,'必填','Required')}</small>`:''}</td><td><code>${esc(param.type)}</code>${param.enum?.length?`<div class="param-enum">${param.enum.map(value=>`<code>${esc(value)}</code>`).join(' ')}</div>`:''}${param.default!==undefined?`<small>${text(locale,'默认','Default')}: ${esc(JSON.stringify(param.default))}</small>`:''}</td><td>${esc(param.description[locale])}</td></tr>`).join('');
  return `<div class="tool-entry" id="tool-${tool.name}"><h3><a href="#tool-${tool.name}"><span class="tool-display-name">${esc(tool.displayName?.[locale] || tool.name)}</span><code>${tool.name}</code></a></h3><p>${esc(tool.description[locale])}</p><details><summary>${text(locale,'参数与返回值','Parameters & result')} <span>${tool.parameters.length} ${text(locale,'个参数','parameters')}</span></summary>${parameters?`<div class="table-scroll"><table><thead><tr><th>${text(locale,'参数','Parameter')}</th><th>${text(locale,'类型与可选值','Type & options')}</th><th>${text(locale,'说明','Description')}</th></tr></thead><tbody>${parameters}</tbody></table></div>`:`<p>${text(locale,'无业务参数；身份与会话由运行时注入。','No business parameters; identity and session are injected by the runtime.')}</p>`}<p class="tool-result"><strong>${text(locale,'返回：','Returns: ')}</strong>${esc(tool.result[locale])}</p>${tool.notes?.length?`<ul class="tool-notes">${tool.notes.map(note=>`<li>${esc(note[locale])}</li>`).join('')}</ul>`:''}</details></div>`;
}
function blockHtml(block, locale, index) {
  switch (block.type) {
    case 'paragraph': return `<p>${esc(block.text[locale])}</p>`;
    case 'note': return `<aside class="callout"><span class="callout-symbol">i</span><div><strong>${esc(block.title[locale])}</strong><p>${esc(block.text[locale])}</p></div></aside>`;
    case 'list': return `<ul class="prose-list">${block.items.map(item => `<li>${esc(item[locale])}</li>`).join('')}</ul>`;
    case 'code': return `<div class="code-block"><div class="code-toolbar"><span>${esc(block.language)}</span><button class="copy-code" data-copy="code-${index}" aria-label="${text(locale, '复制代码', 'Copy code')}">${icon('copy')}<span>${text(locale, '复制', 'Copy')}</span></button></div><pre tabindex="0"><code id="code-${index}">${esc(block.code)}</code></pre></div>`;
    case 'table': return `<div class="table-scroll" role="region" aria-label="${text(locale, '参考表格', 'Reference table')}" tabindex="0"><table><thead><tr>${block.headers.map(cell => `<th scope="col">${esc(cell[locale])}</th>`).join('')}</tr></thead><tbody>${block.rows.map(row => `<tr>${row.map(cell => `<td>${esc(cell[locale])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    case 'architecture': return diagram('architecture',locale);
    case 'diagram': return diagram(block.name,locale);
    case 'tools': return `<div class="tool-directory">${block.items.map(tool=>toolHtml(tool,locale)).join('')}</div>`;
    case 'endpoint': return `<div class="endpoint"><span class="method method-${esc(block.method.toLowerCase())}">${esc(block.method)}</span><code>${esc(block.path)}</code></div>`;
    case 'links': return `<ul class="source-links">${block.items.map(item => `<li><a href="${esc(item.href)}" target="_blank" rel="noreferrer">${esc(item.label[locale])}<span aria-hidden="true">↗</span></a></li>`).join('')}</ul>`;
    case 'cards': return `<div class="doc-cards">${block.items.map(item => `<a href="${path(locale, item.slug)}" class="doc-card"><span class="card-icon">${icon(item.icon)}</span><strong>${esc(item.title[locale])}</strong><p>${esc(item.text[locale])}</p>${icon('arrow')}</a>`).join('')}</div>`;
    default: throw new Error(`Unknown block ${block.type}`);
  }
}
function markdownBlock(block, locale) {
  if (block.type === 'diagram' || block.type === 'architecture') { const name=block.name || 'architecture'; return `![${diagramTitle(name,locale)}](${origin}/diagrams/${name}-${locale}.svg)`; }
  if (block.type === 'tools') return block.items.map(tool=>`### ${tool.displayName?.[locale] || tool.name} · ${tool.name}\n\n${tool.description[locale]}\n\n`+tool.parameters.map(param=>`- \`${param.name}\` (${param.type}${param.required?text(locale,', 必填',', required'):''}): ${param.description[locale]}${param.enum?.length?text(locale,' 可选值：',' Options: ')+param.enum.join(', '):''}${param.default!==undefined?text(locale,' 默认：',' Default: ')+JSON.stringify(param.default):''}`).join('\n')+`\n\n${text(locale,'返回：','Returns: ')}${tool.result[locale]}\n\n`+(tool.notes||[]).map(note=>'- '+note[locale]).join('\n')).join('\n\n');
  if (block.type === 'paragraph') return block.text[locale];
  if (block.type === 'note') return `> **${block.title[locale]}**\n> ${block.text[locale]}`;
  if (block.type === 'code') return `\`\`\`${block.language}\n${block.code}\n\`\`\``;
  if (block.type === 'list') return block.items.map(item => `- ${item[locale]}`).join('\n');
  if (block.type === 'table') return ['| ' + block.headers.map(h => h[locale]).join(' | ') + ' |', '| ' + block.headers.map(() => '---').join(' | ') + ' |', ...block.rows.map(row => '| ' + row.map(cell => cell[locale].replace(/\|/g, '\\|')).join(' | ') + ' |')].join('\n');
  if (block.type === 'endpoint') return `**${block.method}** \`${block.path}\``;
  if (block.type === 'links') return block.items.map(item => `- [${item.label[locale]}](${item.href})`).join('\n');
  if (block.type === 'cards') return block.items.map(item => `- [${item.title[locale]}](${origin}${path(locale, item.slug)}): ${item.text[locale]}`).join('\n');
  return '```text\nFrontend → Backend → Agent Runtime → PostgreSQL + pgvector\n              ↓ HTTP       ↓ MCP\n                  Graph Engine\n```';
}
function markdown(page, locale) {
  return `# ${page.title[locale]}\n\n${page.description[locale]}\n\n` + page.sections.map(section => `## ${section.title[locale]}\n\n${section.blocks.map(block => markdownBlock(block, locale)).join('\n\n')}`).join('\n\n') + '\n';
}
function html(page, locale, index) {
  const otherLocale = locale === 'zh' ? 'en' : 'zh';
  const is404 = index < 0;
  const title = `${page.title[locale]} · iGraph Docs`;
  const pagePath = path(locale, page.slug);
  const groupName = groups[page.group][locale === 'zh' ? 0 : 1];
  const navigation = Object.entries(groups).map(([group, label]) => `<div class="nav-group"><h2>${label[locale === 'zh' ? 0 : 1]}</h2>${pages.filter(item => item.group === group).map(item => `<a href="${path(locale, item.slug)}"${item.slug === page.slug ? ' class="active" aria-current="page"' : ''}>${esc(item.title[locale])}${item.slug === 'api' ? '<span class="nav-badge">API</span>' : ''}</a>`).join('')}</div>`).join('');
  const pagination = [{ item: pages[index - 1], side: 'previous', label: text(locale, '上一页', 'Previous') }, { item: pages[index + 1], side: 'next', label: text(locale, '下一页', 'Next') }].filter(x => x.item && !is404).map(({item,side,label}) => `<a class="page-${side}" href="${path(locale,item.slug)}"><small>${label}</small><strong>${esc(item.title[locale])} ${icon('arrow')}</strong></a>`).join('');
  return `<!doctype html>
<html lang="${locale === 'zh' ? 'zh-CN' : 'en'}" data-locale="${locale}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><meta name="description" content="${esc(page.description[locale])}"><meta name="theme-color" content="#ffffff"><meta name="color-scheme" content="light dark">${is404 ? '<meta name="robots" content="noindex">' : ''}<link id="site-favicon" rel="icon" href="/assets/igraph-logo-light.jpg" type="image/jpeg"><link rel="stylesheet" href="/assets/site.css"><script src="/assets/theme.js"></script><script defer src="/assets/site.js"></script>${!is404 && origin ? `<link rel="canonical" href="${origin}${pagePath}"><link rel="alternate" hreflang="zh-CN" href="${origin}${path('zh',page.slug)}"><link rel="alternate" hreflang="en" href="${origin}${path('en',page.slug)}">` : ''}<meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(page.description[locale])}"><meta property="og:type" content="article"></head>
<body><a class="skip-link" href="#content">${text(locale, '跳转到正文', 'Skip to content')}</a>
<header class="site-header"><div class="header-inner"><a class="brand" href="/${locale}/" aria-label="${text(locale, 'iGraph 首页', 'iGraph home')}"><span class="brand-logo" aria-hidden="true"><img class="brand-logo-light" src="/assets/igraph-logo-light.jpg" width="40" height="40" alt=""><img class="brand-logo-dark" src="/assets/igraph-logo-dark.jpg" width="40" height="40" alt=""></span><span class="brand-name">iGraph</span><span class="brand-divider"></span><span class="brand-docs">docs</span></a><button class="search-trigger" data-open-search>${icon('search')}<span>${text(locale, '搜索文档…', 'Search documentation…')}</span><kbd>Ctrl K</kbd></button><div class="header-actions"><a class="language-switch" href="${path(otherLocale, is404 ? 'overview' : page.slug)}" data-language="${otherLocale}" hreflang="${otherLocale}" aria-label="${text(locale, 'Switch to English', '切换到中文')}">${icon('globe')}<span>${text(locale, 'English', '中文')}</span></a><button class="theme-toggle icon-button" aria-label="${text(locale, '切换主题', 'Toggle theme')}" aria-pressed="false">${icon('sun')}${icon('moon')}</button><a class="header-api" href="/${locale}/api/">${text(locale, 'API 参考', 'API reference')}${icon('arrow')}</a></div></div><div class="subnav"><div><button class="mobile-menu" aria-expanded="false" aria-controls="sidebar">${icon('menu')}<span>${text(locale, '目录', 'Menu')}</span></button><a href="/${locale}/overview/"${['start','build','engine'].includes(page.group) ? ' class="selected"' : ''}>${text(locale, '开发指南', 'Guides')}</a><a href="/${locale}/api/"${page.group === 'reference' ? ' class="selected"' : ''}>${text(locale, '接口与参考', 'Reference')}</a><a href="/${locale}/deployment/"${page.group === 'ship' ? ' class="selected"' : ''}>${text(locale, '部署与更新', 'Deploy & updates')}</a></div></div></header>
<div class="page-layout"><button class="sidebar-backdrop" aria-label="${text(locale, '关闭目录', 'Close menu')}" hidden></button><aside class="sidebar" id="sidebar"><nav aria-label="${text(locale, '文档目录', 'Documentation navigation')}">${navigation}</nav></aside>
<main id="content" tabindex="-1"><div class="article-eyebrow"><span>${esc(groupName)}</span></div><div class="title-row"><h1>${esc(page.title[locale])}</h1></div><p class="page-description">${esc(page.description[locale])}</p><div class="article-meta"><span>${text(locale, '更新于', 'Updated')} <time datetime="${updated}">${updated}</time></span>${!is404 ? `<button class="copy-page" data-markdown="/markdown/${locale}/${page.slug}.md">${icon('copy')}<span>${text(locale, '复制页面', 'Copy page')}</span></button><a href="/markdown/${locale}/${page.slug}.md" download>${text(locale, '下载 Markdown', 'Download Markdown')}<span aria-hidden="true">↗</span></a>` : ''}</div>
<article>${page.sections.map((section, sectionIndex) => `<section class="doc-section" id="${section.id}"><h2><a class="heading-anchor" href="#${section.id}" aria-label="${text(locale, '章节链接：', 'Link to section: ')}${esc(section.title[locale])}">#</a>${esc(section.title[locale])}</h2>${section.blocks.map((block,blockIndex) => blockHtml(block,locale,`${sectionIndex}-${blockIndex}`)).join('')}</section>`).join('')}</article><nav class="pagination" aria-label="${text(locale, '相邻文档', 'Adjacent pages')}">${pagination}</nav><footer class="article-footer"><span>iGraph</span><a href="/${locale}/changelog/">${text(locale, '文档更新记录', 'Documentation changelog')}${icon('arrow')}</a></footer></main>
<aside class="toc"><span class="toc-title">${text(locale, '本页内容', 'On this page')}</span><nav aria-label="${text(locale, '本页内容', 'On this page')}">${page.sections.map((section,i) => `<a href="#${section.id}"${i===0?' class="active"':''}>${esc(section.title[locale])}</a>`).join('')}</nav><button class="back-top">↑ ${text(locale, '回到顶部', 'Back to top')}</button></aside></div>
<dialog class="search-dialog" aria-labelledby="search-label"><div class="search-input-row">${icon('search')}<label id="search-label" class="sr-only" for="doc-search">${text(locale, '搜索文档', 'Search documentation')}</label><input id="doc-search" type="search" placeholder="${text(locale, '搜索 API、构图、部署…', 'Search APIs, construction, deployment…')}" autocomplete="off"><button class="close-search" aria-label="${text(locale, '关闭搜索', 'Close search')}"><kbd>Esc</kbd></button></div><p class="search-summary" role="status" aria-live="polite"></p><div class="search-results"></div><div class="search-footer"><span>${text(locale, '↑ ↓ 选择 · Enter 打开', '↑ ↓ select · Enter open')}</span><span>${text(locale, '本地全文搜索', 'Local full-text search')}</span></div></dialog><div class="toast" role="status" aria-live="polite"></div></body></html>`;
}
async function save(file, content) { const target=join(out,file); await mkdir(dirname(target),{recursive:true}); await writeFile(target,content); }
for (const name of diagramNames) for (const locale of ['zh','en']) for (const dark of [false,true]) await save(`diagrams/${name}-${locale}${dark?'-dark':''}.svg`,renderDiagram(name,locale,dark));
for (const file of ['commerce.sql', 'commerce-options.json', 'commerce-result.json']) {
  await save(`examples/${file}`, await readFile(join(root, 'examples', file)));
}
const search=[];
for (const locale of ['zh','en']) {
  for (const [index,page] of pages.entries()) {
    await save(`${locale}/${page.slug}/index.html`, html(page,locale,index));
    await save(`markdown/${locale}/${page.slug}.md`, markdown(page,locale));
    for (const section of page.sections) {
      search.push({locale,title:page.title[locale],section:section.title[locale],href:path(locale,page.slug)+'#'+section.id,text:section.blocks.filter(block=>block.type!=='tools').map(block=>markdownBlock(block,locale)).join('\n')});
      for (const tool of toolsIn(section)) search.push({locale,title:page.title[locale],section:`${tool.displayName?.[locale] || tool.name} · ${tool.name}`,name:tool.name,kind:'tool',href:path(locale,page.slug)+'#tool-'+tool.name,text:markdownBlock({type:'tools',items:[tool]},locale)});
    }
  }
  const notFound={slug:'404',group:'start',title:{zh:'页面未找到',en:'Page not found'},description:{zh:'这个地址没有对应文档。可以返回概览，或搜索要查找的主题。',en:'There is no documentation at this address. Return to the overview or search for a topic.'},sections:[{id:'return',title:{zh:'继续阅读',en:'Keep exploring'},blocks:[{type:'cards',items:[{slug:'overview',icon:'book',title:{zh:'返回文档首页',en:'Documentation overview'},text:{zh:'浏览 iGraph 的全部技术文档。',en:'Explore the iGraph documentation.'}}]}]}]};
  await save(`${locale}/404.html`,html(notFound,locale,-1));
  if(locale==='en') await save('404.html',html(notFound,locale,-1));
}
await save('index.html',renderCover('en',{origin,rootEntry:true}));
for(const locale of ['zh','en']) await save(`${locale}/index.html`,renderCover(locale,{origin}));
await save('assets/point-cloud-fallback.svg',renderCloudFallback());
await save('search-index.json',JSON.stringify(search));
await save('llms.txt',`# iGraph\n\n> iGraph is a graph-driven data analysis agent (图驱动的数据分析智能体). Graph representations, queries, algorithms, and learning provide evidence for agent-orchestrated analysis and explanation. Skills organize workflows; self-evolving construction prepares and optimizes graph representations. Documentation only; no application API is hosted here.\n\n## English documentation\n\n${pages.map(page=>`- [${page.title.en}](${origin}/markdown/en/${page.slug}.md): ${page.description.en}`).join('\n')}\n\n## 中文文档\n\n${pages.map(page=>`- [${page.title.zh}](${origin}/markdown/zh/${page.slug}.md): ${page.description.zh}`).join('\n')}\n`);
await save('llms-full.txt',pages.map(page=>markdown(page,'en')).join('\n---\n\n'));
await save('robots.txt',`User-agent: *\nAllow: /\n${origin ? `Sitemap: ${origin}/sitemap.xml\n` : ''}`);
if(origin) await save('sitemap.xml',`<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${['zh','en'].map(locale=>`<url><loc>${origin}/${locale}/</loc></url>`).join('')}${['zh','en'].flatMap(locale=>pages.map(page=>`<url><loc>${origin}${path(locale,page.slug)}</loc><lastmod>${updated}</lastmod></url>`)).join('')}</urlset>`);
await mkdir(join(out,'assets'),{recursive:true});
for(const file of ['site.css','site.js','theme.js','entry.js','cover.css','cover.js','particles.js','cloud-geometry.js','igraph-logo-light.jpg','igraph-logo-dark.jpg']) await copyFile(join(root,'assets',file),join(out,'assets',file));
await save('_headers',`/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n  X-Frame-Options: DENY\n  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'none'\n  Permissions-Policy: camera=(), microphone=(), geolocation=()\n/assets/*\n  Cache-Control: public, max-age=0, must-revalidate\n/search-index.json\n  Cache-Control: public, max-age=0, must-revalidate\n`);
console.log(`Built ${pages.length * 2} documentation pages, 2 localized covers, root entry, Markdown exports, search, and AI-readable documentation.`);
