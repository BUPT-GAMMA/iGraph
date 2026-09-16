import { createPointCloud } from './particles.js';

const root = document.documentElement;
const locale = root.dataset.locale;
const text = (zh, en) => locale === 'zh' ? zh : en;
const themeButton = document.querySelector('.cover-theme');
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let explicitTheme = false;
try { explicitTheme = ['light','dark'].includes(localStorage.getItem('igraph-docs-theme')); } catch {}
document.querySelector('.cover-language').addEventListener('click', event => {
  const target = event.currentTarget;
  target.href = new URL(target.getAttribute('href'), location.origin).pathname + location.hash;
  try { localStorage.setItem('igraph-docs-preferred-locale', target.getAttribute('hreflang')); } catch {}
});
function syncTheme() {
  const dark = root.dataset.theme === 'dark';
  themeButton.setAttribute('aria-pressed', String(dark));
  themeButton.setAttribute('aria-label', dark ? text('切换到浅色模式', 'Switch to light mode') : text('切换到深色模式', 'Switch to dark mode'));
  document.querySelector('meta[name="theme-color"]').content = dark ? '#0c1420' : '#f8fbff';
  document.getElementById('site-favicon').href = `/assets/igraph-logo-${dark ? 'dark' : 'light'}.jpg`;
}
syncTheme();
themeButton.hidden = false;
themeButton.addEventListener('click', () => {
  explicitTheme = true;
  root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('igraph-docs-theme', root.dataset.theme); } catch {}
  syncTheme();
});
systemTheme.addEventListener('change', event => { if(!explicitTheme) { root.dataset.theme = event.matches ? 'dark' : 'light'; syncTheme(); } });

const stage = document.querySelector('.cloud-stage');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
let cloud;
try {
  cloud = createPointCloud(document.getElementById('point-cloud'), {
    onReady: () => { stage.dataset.cloudReady = 'true'; },
  });
} catch { stage.dataset.cloudReady = 'false'; }
if(cloud) {
  const setMotion = () => {
    cloud.setPaused(reducedMotion.matches);
  };
  reducedMotion.addEventListener('change', setMotion);
  setMotion();
  // A restored history entry keeps its canvas instance; a discarded page releases it.
  window.addEventListener('pagehide', event => { if(!event.persisted) cloud.destroy(); });
}
