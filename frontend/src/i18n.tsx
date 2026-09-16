import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

export type Language = 'en' | 'zh';
export type Translate = (english: string, chinese: string) => string;

export const LANGUAGE_STORAGE_KEY = 'igraph-language';

export function resolveLanguage(saved: string | null): Language {
  if (saved === 'zh' || saved === 'en') return saved;
  return 'en';
}

function browserLanguage(): Language {
  try {
    return resolveLanguage(window.localStorage.getItem(LANGUAGE_STORAGE_KEY));
  } catch {
    return resolveLanguage(null);
  }
}

let activeLanguage: Language = browserLanguage();

export function currentLanguage() {
  return activeLanguage;
}

export function tr(english: string, chinese: string) {
  return activeLanguage === 'zh' ? chinese : english;
}

type I18nContextValue = {
  language: Language;
  setLanguage: (language: Language) => void;
  toggleLanguage: () => void;
  t: Translate;
};

const fallbackValue: I18nContextValue = {
  language: activeLanguage,
  setLanguage: language => { activeLanguage = language; },
  toggleLanguage: () => { activeLanguage = activeLanguage === 'en' ? 'zh' : 'en'; },
  t: tr,
};

const I18nContext = createContext<I18nContextValue>(fallbackValue);

export function I18nProvider({ children, initialLanguage }: { children: ReactNode; initialLanguage?: Language }) {
  const [language, setLanguageState] = useState<Language>(() => initialLanguage || browserLanguage());
  activeLanguage = language;

  const setLanguage = useCallback((nextLanguage: Language) => {
    activeLanguage = nextLanguage;
    setLanguageState(nextLanguage);
    try { window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLanguage); } catch { /* Storage is optional. */ }
  }, []);

  const toggleLanguage = useCallback(() => {
    setLanguage(activeLanguage === 'en' ? 'zh' : 'en');
  }, [setLanguage]);

  const t = useCallback<Translate>((english, chinese) => language === 'zh' ? chinese : english, [language]);

  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    document.title = t('iGraph · Graph Intelligence Workspace', 'iGraph · 图分析工作台');
  }, [language, t]);

  useEffect(() => {
    function syncLanguage(event: StorageEvent) {
      if (event.key !== LANGUAGE_STORAGE_KEY || (event.newValue !== 'en' && event.newValue !== 'zh')) return;
      activeLanguage = event.newValue;
      setLanguageState(event.newValue);
    }
    window.addEventListener('storage', syncLanguage);
    return () => window.removeEventListener('storage', syncLanguage);
  }, []);

  const value = useMemo<I18nContextValue>(() => ({ language, setLanguage, toggleLanguage, t }), [language, setLanguage, t, toggleLanguage]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}
