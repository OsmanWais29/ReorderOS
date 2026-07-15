import React, { createContext, useContext, useEffect, useState } from 'react';
import {
  loadToken,
  saveToken,
  clearToken,
  saveRefreshToken,
  clearRefreshToken,
} from './storage';
import { fetchMe, type AuthTokens, type MeResponse } from './api';
import { onTokenRefreshed, tryRefresh } from './session';
import { setActiveTenantId } from '../api/activeTenant';

type AuthState = {
  token: string | null;
  me: MeResponse | null;
  loading: boolean;
  signIn: (tokens: AuthTokens) => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthState>({
  token: null,
  me: null,
  loading: true,
  signIn: async () => {},
  signOut: async () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Background refreshes (silent 401 recovery in the API clients) push the
    // new access token back into React state here.
    onTokenRefreshed(setToken);
    loadToken().then(async (stored) => {
      // Boot: stale access token is NORMAL (5-min TTL) — refresh, then load.
      let usable = stored;
      if (usable) {
        try {
          await fetchMe(usable);
        } catch {
          usable = await tryRefresh();
        }
      } else {
        usable = await tryRefresh();
      }
      if (usable) {
        try {
          const profile = await fetchMe(usable);
          setToken(usable);
          setMe(profile);
          setActiveTenantId(profile.tenants[0]?.id ?? null);
        } catch {
          await clearToken();
        }
      }
      setLoading(false);
    });
    return () => onTokenRefreshed(null);
  }, []);

  const signIn = async (tokens: AuthTokens) => {
    await saveToken(tokens.accessToken);
    if (tokens.refreshToken) await saveRefreshToken(tokens.refreshToken);
    const profile = await fetchMe(tokens.accessToken);
    setToken(tokens.accessToken);
    setMe(profile);
    setActiveTenantId(profile.tenants[0]?.id ?? null);
  };

  const signOut = async () => {
    await clearToken();
    await clearRefreshToken();
    setToken(null);
    setMe(null);
    setActiveTenantId(null);
  };

  return (
    <AuthContext.Provider value={{ token, me, loading, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
