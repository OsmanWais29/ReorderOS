// Language context — global EN/FR toggle persisted to AsyncStorage.
//
// Usage:
//   const { lang, setLang, t } = useLang();
//   <Text>{t.getStarted}</Text>

import React, { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from 'react';
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as Localization from 'expo-localization';
import { STRINGS, type Lang } from './strings';

const STORAGE_KEY = '@reorderos/lang';

type Ctx = {
  lang: Lang;
  setLang: (l: Lang) => void;
  toggle: () => void;
  // Indexed by Lang (not pinned to 'en'): STRINGS[lang] is STRINGS['en'] | STRINGS['fr'],
  // whose per-key value type is a union of the EN/FR string literals → `string` to consumers.
  // Pinning to 'en' made the 'fr' literals unassignable (TS2322). Per-key completeness across
  // locales is enforced separately by the i18n completeness check, not by this type.
  t: (typeof STRINGS)[Lang];
};

const LangContext = createContext<Ctx | null>(null);

function detectInitial(): Lang {
  // Quebec / Canadian French defaults to French; otherwise English.
  const locale = Localization.getLocales()[0]?.languageCode?.toLowerCase();
  return locale === 'fr' ? 'fr' : 'en';
}

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectInitial);
  const [hydrated, setHydrated] = useState(false);

  // Hydrate from storage on mount.
  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY)
      .then(stored => {
        if (stored === 'en' || stored === 'fr') setLangState(stored);
      })
      .finally(() => setHydrated(true));
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    AsyncStorage.setItem(STORAGE_KEY, l).catch(() => {});
  }, []);

  const toggle = useCallback(() => {
    setLang(lang === 'en' ? 'fr' : 'en');
  }, [lang, setLang]);

  // Don't render until hydrated to avoid a flash of EN content for FR users.
  if (!hydrated) return null;

  const value: Ctx = { lang, setLang, toggle, t: STRINGS[lang] };
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useLang(): Ctx {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error('useLang must be used inside <LangProvider>');
  return ctx;
}
