(() => {
  let locale;
  try { locale = localStorage.getItem('igraph-docs-preferred-locale'); } catch {}
  if (locale !== 'zh' && locale !== 'en') locale = 'en';
  location.replace('/' + locale + '/' + location.hash);
})();
