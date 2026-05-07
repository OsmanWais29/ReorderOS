import React, { createContext, useContext, useEffect, useState } from 'react';
import { loadToken, saveToken, clearToken } from './storage';
import { fetchMe, type MeResponse } from './api';

type AuthState = {
  token: string | null;
  me: MeResponse | null;
  loading: boolean;
  signIn: (token: string) => Promise<void>;
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
    loadToken().then(async (stored) => {
      if (stored) {
        try {
          const profile = await fetchMe(stored);
          setToken(stored);
          setMe(profile);
        } catch {
          await clearToken();
        }
      }
      setLoading(false);
    });
  }, []);

  const signIn = async (newToken: string) => {
    await saveToken(newToken);
    const profile = await fetchMe(newToken);
    setToken(newToken);
    setMe(profile);
  };

  const signOut = async () => {
    await clearToken();
    setToken(null);
    setMe(null);
  };

  return (
    <AuthContext.Provider value={{ token, me, loading, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
