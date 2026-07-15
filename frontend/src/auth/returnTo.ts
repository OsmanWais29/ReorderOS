// Post-login return path. The WorkOS redirect round-trip reloads the web app,
// so the intended route must survive in localStorage (web) with an in-memory
// fallback for native. Consumed exactly once by the auth callback.

import { Platform } from 'react-native';

const KEY = 'auth_return_to';
let mem: string | null = null;

export const saveReturnTo = (path: string): void => {
  mem = path;
  if (Platform.OS === 'web') {
    try {
      window.localStorage.setItem(KEY, path);
    } catch {
      // storage unavailable (private mode) — in-memory fallback stands
    }
  }
};

export const consumeReturnTo = (): string | null => {
  let v = mem;
  mem = null;
  if (Platform.OS === 'web') {
    try {
      v = window.localStorage.getItem(KEY) ?? v;
      window.localStorage.removeItem(KEY);
    } catch {
      // ignore
    }
  }
  return v;
};
