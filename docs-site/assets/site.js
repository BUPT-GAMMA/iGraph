(() => {
  const locale = document.documentElement.dataset.locale;
  const msg = (zh, en) => locale === 'zh' ? zh : en;
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const themeButton = $('.theme-toggle');
  const dialog = $('.search-dialog');
  const searchInput = $('#doc-search');
  const results = $('.search-results');
  let searchIndex;
  let selected = -1;
  let toastTimer;
  let searchRequest;
  let searchOpener;

  function toast(message) {
    const element = $('.toast');
    element.textContent = message;
    element.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => element.classList.remove('visible'), 2600);
  }
  function syncTheme() {
    const dark = document.documentElement.dataset.theme === 'dark';
    themeButton.setAttribute('aria-pressed', String(dark));
    themeButton.setAttribute('aria-label', dark ? msg('切换到浅色模式', 'Switch to light mode') : msg('切换到深色模式', 'Switch to dark mode'));
    document.querySelector('meta[name="theme-color"]').content = dark ? '#10151e' : '#ffffff';
    document.getElementById('site-favicon').href = `/assets/igraph-logo-${dark ? 'dark' : 'light'}.jpg`;
  }
  syncTheme();
  themeButton.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('igraph-docs-theme', theme); } catch {}
    syncTheme();
  });
  $('.language-switch').addEventListener('click', event => {
    const target = event.currentTarget;
    target.href = new URL(target.getAttribute('href'), location.origin).pathname + location.hash;
    try { localStorage.setItem('igraph-docs-preferred-locale', target.dataset.language); } catch {}
  });

  async function copy(value) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(value);
    } catch {
      const field = document.createElement('textarea');
      field.className = 'sr-only';
      field.value = value;
      field.setAttribute('readonly', '');
      document.body.append(field);
      field.select();
      const success = document.execCommand('copy');
      field.remove();
      if (!success) throw new Error('Copy failed');
    }
  }
  $$('.copy-code, .copy-page').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      let value;
      if (button.dataset.copy) value = document.getElementById(button.dataset.copy).textContent;
      else {
        const response = await fetch(button.dataset.markdown);
        if (!response.ok) throw new Error('Markdown unavailable');
        value = await response.text();
      }
      await copy(value);
      toast(msg('已复制到剪贴板', 'Copied to clipboard'));
    } catch { toast(msg('复制失败，请选择文本复制或下载 Markdown', 'Copy failed. Select the text or download Markdown.')); }
    finally { button.disabled = false; button.focus(); }
  }));

  const menuButton = $('.mobile-menu');
  const backdrop = $('.sidebar-backdrop');
  function setMenu(open) {
    document.body.classList.toggle('menu-open', open);
    menuButton.setAttribute('aria-expanded', String(open));
    backdrop.hidden = !open;
    $('main').inert = open;
    if (open) $('#sidebar a').focus();
    else menuButton.focus();
  }
  menuButton.addEventListener('click', () => setMenu(!document.body.classList.contains('menu-open')));
  backdrop.addEventListener('click', () => setMenu(false));
  const mobile = matchMedia('(max-width: 760px)');
  mobile.addEventListener('change', () => { if (!mobile.matches && document.body.classList.contains('menu-open')) setMenu(false); });

  function selectResult(next) {
    const items = [...results.querySelectorAll('a')];
    selected = items.length ? (next + items.length) % items.length : -1;
    items.forEach((item, index) => item.classList.toggle('selected', index === selected));
    if (selected >= 0) items[selected].scrollIntoView({ block: 'nearest' });
  }
  function renderSearch() {
    if (!searchIndex) return;
    const query = searchInput.value.trim().toLocaleLowerCase();
    const words = query.split(/\s+/).filter(Boolean);
    const candidates = searchIndex.filter(item => item.locale === locale).map(item => {
      const title = `${item.title} ${item.section}`.toLocaleLowerCase();
      const body = item.text.toLocaleLowerCase();
      const matches = words.every(word => title.includes(word) || body.includes(word));
      const score = (query && (item.name || item.section).toLocaleLowerCase() === query ? 40 : 0) + words.reduce((value, word) => value + (title.includes(word) ? 8 : 0) + (body.includes(word) ? 1 : 0), 0);
      return { ...item, matches, score };
    }).filter(item => !query || item.matches).sort((a,b) => b.score - a.score);
    const items = candidates.slice(0, query ? 12 : 6);
    results.replaceChildren();
    $('.search-summary').textContent = query
      ? candidates.length ? msg(`找到 ${candidates.length} 条结果`, `${candidates.length} results`) : msg('未找到结果。试试“构图”“API”或“部署”。', 'No results. Try “construction”, “API”, or “deploy”.')
      : msg('开始输入以搜索全部中文文档', 'Type to search all English documentation');
    for (const item of items) {
      const link = document.createElement('a');
      link.className = 'search-result';
      link.href = item.href;
      const title = document.createElement('strong');
      title.textContent = item.section;
      const page = document.createElement('small');
      page.textContent = item.title;
      const preview = document.createElement('p');
      const plain = item.text.replace(/[`#>|]/g, '').replace(/\s+/g, ' ').trim();
      const first = words.length ? plain.toLocaleLowerCase().indexOf(words[0]) : 0;
      const start = Math.max(0, first - 35);
      preview.textContent = (start ? '…' : '') + plain.slice(start, start + 155) + (plain.length > start + 155 ? '…' : '');
      link.append(page, title, preview);
      link.addEventListener('mouseenter', () => selectResult(items.indexOf(item)));
      link.addEventListener('click', () => {
        dialog.close();
        if (new URL(link.href).pathname === location.pathname) requestAnimationFrame(revealTool);
      });
      results.append(link);
    }
    selectResult(0);
  }
  async function openSearch() {
    if (document.body.classList.contains('menu-open')) setMenu(false);
    if (!dialog.open) { searchOpener = document.activeElement; dialog.showModal(); }
    searchInput.focus();
    if (searchIndex) return renderSearch();
    $('.search-summary').textContent = msg('正在加载搜索索引…', 'Loading the search index…');
    try {
      searchRequest ||= fetch('/search-index.json').then(response => {
        if (!response.ok) throw new Error('Search index unavailable');
        return response.json();
      });
      searchIndex = await searchRequest;
      renderSearch();
    } catch {
      searchRequest = undefined;
      $('.search-summary').textContent = msg('搜索索引加载失败。关闭后重新打开可重试。', 'Could not load search. Close and reopen to retry.');
    }
  }
  $$('[data-open-search]').forEach(button => button.addEventListener('click', openSearch));
  $('.close-search').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => searchOpener?.focus());
  dialog.addEventListener('click', event => {
    const box = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) dialog.close();
  });
  searchInput.addEventListener('input', renderSearch);
  searchInput.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); selectResult(selected + (event.key === 'ArrowDown' ? 1 : -1)); }
    if (event.key === 'Enter' && selected >= 0) { event.preventDefault(); results.querySelectorAll('a')[selected]?.click(); }
  });
  document.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    const editable = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName) || event.target.isContentEditable;
    if (((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') || (event.key === '/' && !editable && !event.ctrlKey && !event.metaKey)) {
      event.preventDefault();
      openSearch();
    }
    if (event.key === 'Escape' && document.body.classList.contains('menu-open') && !dialog.open) setMenu(false);
    if (event.key === 'Tab' && document.body.classList.contains('menu-open') && !dialog.open) {
      const links = [...$('#sidebar').querySelectorAll('a')];
      const first = links[0], last = links.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); menuButton.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); menuButton.focus(); }
      else if (document.activeElement === menuButton) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
    }
  });

  const sections = $$('.doc-section');
  const tocLinks = $$('.toc nav a');
  let scrollPending = false;
  function updateToc() {
    const current = sections.filter(section => section.getBoundingClientRect().top <= 175).at(-1) || sections[0];
    tocLinks.forEach(link => {
      const active = link.hash === '#' + current?.id;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    scrollPending = false;
  }
  window.addEventListener('scroll', () => { if (!scrollPending) { scrollPending = true; requestAnimationFrame(updateToc); } }, { passive: true });
  updateToc();
  function revealTool() {
    const target = document.getElementById(location.hash.slice(1));
    if (target?.classList.contains('tool-entry')) {
      target.querySelector('details').open = true;
      target.scrollIntoView({ block: 'start' });
    }
  }
  revealTool();
  window.addEventListener('hashchange', revealTool);
  $('.back-top').addEventListener('click', () => window.scrollTo({ top: 0, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }));
})();
