'use strict';

(() => {
  const storageKey = 'workbench.theme';
  const root = document.documentElement;
  const colorSchemeMeta = document.querySelector('meta[name="color-scheme"]');
  const systemTheme = window.matchMedia?.('(prefers-color-scheme: dark)');
  const icons = {
    dark: '<path d="M20.5 15.7A8.5 8.5 0 0 1 8.3 3.5 8.5 8.5 0 1 0 20.5 15.7Z"/>',
    light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  };
  const labels = {
    'zh-HK': {dark:['夜間','切換至夜間模式'],light:['日間','切換至日間模式']},
    'zh-CN': {dark:['夜间','切换至夜间模式'],light:['日间','切换至日间模式']},
    en: {dark:['Dark','Switch to dark mode'],light:['Light','Switch to light mode']},
  };

  function savedTheme() {
    try {
      const value = localStorage.getItem(storageKey);
      return value === 'light' || value === 'dark' ? value : null;
    } catch {
      return null;
    }
  }

  let manualTheme = savedTheme();
  let currentTheme;

  function interfaceLocale() {
    const language = root.lang.toLowerCase();
    if (language === 'zh-cn' || language.startsWith('zh-hans')) return 'zh-CN';
    if (language === 'zh-hk' || language.startsWith('zh-hant')) return 'zh-HK';
    return 'en';
  }

  function updateButton() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
    const [label, description] = labels[interfaceLocale()][nextTheme];
    button.querySelector('.theme-toggle-icon').innerHTML = icons[nextTheme];
    button.querySelector('.theme-toggle-label').textContent = label;
    button.setAttribute('aria-label', description);
    button.title = description;
  }

  function applyTheme(theme) {
    currentTheme = theme;
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    if (colorSchemeMeta) colorSchemeMeta.content = theme;
    updateButton();
  }

  function preferredTheme() {
    return manualTheme || (systemTheme?.matches ? 'dark' : 'light');
  }

  // Run before stylesheets load so the first painted frame has the right theme.
  applyTheme(preferredTheme());

  if (systemTheme) {
    const onSystemChange = () => {
      if (!manualTheme) applyTheme(preferredTheme());
    };
    if (systemTheme.addEventListener) systemTheme.addEventListener('change', onSystemChange);
    else systemTheme.addListener?.(onSystemChange);
  }

  window.addEventListener('storage', event => {
    if (event.key !== storageKey) return;
    manualTheme = savedTheme();
    applyTheme(preferredTheme());
  });

  function connectButton() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    updateButton();
    button.addEventListener('click', () => {
      manualTheme = currentTheme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(storageKey, manualTheme); } catch { /* Theme still works for this page. */ }
      applyTheme(manualTheme);
    });
    new MutationObserver(updateButton).observe(root, {attributes:true,attributeFilter:['lang']});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', connectButton, {once:true});
  else connectButton();
})();
