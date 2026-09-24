import { sampleCloud, projectCloudPoint } from '../assets/cloud-geometry.js';

const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
const arrow = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h15m-6-6 6 6-6 6"/></svg>';
const globe = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg>';

export function renderCover(locale, { origin = '', rootEntry = false } = {}) {
  const zh = locale === 'zh';
  const text = (cn, en) => zh ? cn : en;
  const other = zh ? 'en' : 'zh';
  const title = text('iGraph · 图驱动的数据分析智能体', 'iGraph · Graph-driven data analysis agent');
  const description = text('iGraph 是图驱动的数据分析智能体。以图组织实体与关系，通过图查询、算法和学习获取分析证据，再由智能体组织分析过程并解释结果。', 'iGraph is a graph-driven data analysis agent. Represent entities and relationships as a graph, obtain evidence through graph queries, algorithms, and learning, and let the agent organize the analysis and explain its results.');
  const chapters = [
    ['agent-runtime', '01', '分析智能体', 'Analysis agent', '拆解问题、调用工具、解释结果', 'Planning, tools, and interpretation'],
    ['graph-engine', '02', '图分析引擎', 'Graph Engine', '图查询、算法与机器学习', 'Graph queries, algorithms, and learning'],
    ['construction', '03', '自进化构图', 'Evolving construction', '为分析准备和优化图表示', 'Prepare graph representations for analysis'],
  ];
  return `<!doctype html>
<html lang="${zh ? 'zh-CN' : 'en'}" data-locale="${locale}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escape(title)}</title><meta name="description" content="${escape(description)}">
<meta name="theme-color" content="#f8fbff"><meta name="color-scheme" content="light dark">
<link id="site-favicon" rel="icon" href="/assets/igraph-logo-light.jpg" type="image/jpeg">
<link rel="stylesheet" href="/assets/cover.css"><script src="/assets/theme.js"></script>
${rootEntry ? '<script src="/assets/entry.js"></script>' : ''}
<script type="module" src="/assets/cover.js"></script>
${origin ? `<link rel="canonical" href="${origin}/${locale}/"><link rel="alternate" hreflang="zh-CN" href="${origin}/zh/"><link rel="alternate" hreflang="en" href="${origin}/en/"><link rel="alternate" hreflang="x-default" href="${origin}/">` : ''}
<meta property="og:title" content="${escape(title)}"><meta property="og:description" content="${escape(description)}"><meta property="og:type" content="website">
</head>
<body class="cover-page">
<div class="cover-backdrop" aria-hidden="true">
  <div class="cloud-stage" data-cloud-ready="false">
    <img class="cloud-fallback" src="/assets/point-cloud-fallback.svg" width="1440" height="1000" alt="">
    <canvas id="point-cloud" aria-hidden="true"></canvas>
  </div>
  <div class="cloud-veil"></div>
</div>
<a class="skip-link" href="#cover-content">${text('跳转到正文', 'Skip to content')}</a>
<header class="cover-header">
  <a class="cover-brand" href="/${locale}/" aria-label="${text('iGraph 首页', 'iGraph home')}"><span class="cover-logo" aria-hidden="true"><img class="logo-light" src="/assets/igraph-logo-light.jpg" width="44" height="44" alt=""><img class="logo-dark" src="/assets/igraph-logo-dark.jpg" width="44" height="44" alt=""></span><span>iGraph</span><small>Docs</small></a>
  <nav class="cover-nav" aria-label="${text('主导航', 'Main navigation')}"><a href="/${locale}/overview/">${text('技术文档', 'Documentation')}</a><a href="/${locale}/agent-runtime/">${text('分析机制', 'Agent workflow')}</a><a href="/${locale}/graph-tools/">${text('图工具', 'Graph tools')}</a><a href="/${locale}/experiments/">${text('实验结果', 'Results')}</a></nav>
  <div class="cover-preferences"><a class="cover-language" href="/${other}/" hreflang="${other}" aria-label="${zh ? 'Switch to English' : '切换到中文'}">${globe}<span>${zh ? 'EN' : '中文'}</span></a><button class="cover-theme" type="button" aria-label="${text('切换主题', 'Toggle theme')}" aria-pressed="false" hidden><svg class="theme-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></svg><svg class="theme-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z"/></svg></button></div>
</header>
<main id="cover-content" tabindex="-1">
  <section class="cover-hero" aria-labelledby="cover-title">
    <div class="hero-copy">
      <p class="hero-eyebrow"><span></span>${text('图驱动分析 · 技术文档', 'GRAPH-DRIVEN ANALYSIS · DOCUMENTATION')}</p>
      <h1 id="cover-title">iGraph<span class="title-period">.</span></h1>
      <h2>${text('图驱动的', 'Graph-driven')}<br><span>${text('数据分析智能体', 'data analysis agent')}</span></h2>
      <p class="hero-description">${text('从数据中的实体与关系出发，让智能体围绕分析问题组织图查询、算法与学习，将计算结果转化为有依据的分析与解释。', 'Start with the entities and relationships in your data. Let the agent organize graph queries, algorithms, and learning around your questions, then turn computed results into evidence-based analysis and explanations.')}</p>
      <div class="hero-actions"><a class="start-reading" href="/${locale}/overview/">${text('开始阅读', 'Start reading')}${arrow}</a><a class="read-report" href="/${locale}/graph-tools/">${text('探索分析工具', 'Explore graph tools')}${arrow}</a></div>
      <p class="hero-principles"><span>${text('组织数据', 'Graph representations')}</span><span>${text('开展分析', 'Analysis')}</span><span>${text('解释结果', 'Interpretation')}</span></p>
    </div>
  </section>
  <nav class="chapter-nav" aria-label="${text('探索技术章节', 'Explore technical chapters')}">${chapters.map(([slug,n,cn,en,cnDesc,enDesc])=>`<a href="/${locale}/${slug}/"><span class="chapter-number">${n}</span><span class="chapter-copy"><strong>${text(cn,en)}</strong><small>${text(cnDesc,enDesc)}</small></span>${arrow}</a>`).join('')}</nav>
</main>
<footer class="cover-footer"><span>iGraph <span class="footer-divider">/</span> ${text('技术文档', 'Documentation')}</span><a href="/${locale}/changelog/">${text('文档更新记录', 'Documentation updates')}${arrow}</a></footer>
</body></html>`;
}

export function renderCloudFallback() {
  const points = sampleCloud(3800).map(point => ({...point,...projectCloudPoint(point,1440,1000)}));
  points.sort((a,b) => a.depth-b.depth);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 1000">${points.map((p,i) => {
    const radius = Math.min(2.6,(.42+.34*p.weight+.72*p.depth)*p.perspective*1.25*1.35);
    const opacity = (.22+.72*p.depth*p.depth)*.94*(.75+.25*p.weight);
    return `<circle cx="${p.x.toFixed(2)}" cy="${p.y.toFixed(2)}" r="${radius.toFixed(2)}" fill="${i%7===0?'#188b9f':'#3166ba'}" opacity="${opacity.toFixed(2)}"/>`;
  }).join('')}</svg>`;
}
