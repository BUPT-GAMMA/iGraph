(() => {
  let theme;
  try { theme = localStorage.getItem('igraph-docs-theme'); } catch {}
  document.documentElement.dataset.theme = theme === 'dark' || (theme !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  const favicon = document.getElementById('site-favicon');
  if (favicon) favicon.href = `/assets/igraph-logo-${document.documentElement.dataset.theme}.jpg`;
})();
