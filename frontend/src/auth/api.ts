import { API_BASE, REDIRECT_URI } from './config';

/** Access + rotated refresh pair — store BOTH (WorkOS refresh is single-use). */
export type AuthTokens = { accessToken: string; refreshToken: string | null };

export async function signInWithPassword(email: string, password: string): Promise<AuthTokens> {
  const res = await fetch(`${API_BASE}/api/v1/auth/sign-in`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? 'Sign in failed');
  }
  const data = await res.json();
  return { accessToken: data.access_token as string, refreshToken: data.refresh_token ?? null };
}

export type MeResponse = {
  user: {
    id: string;
    workos_id: string;
    email: string;
    email_verified: boolean;
    first_name: string | null;
    last_name: string | null;
  };
  tenants: Array<{ id: string; slug: string; name: string; plan: string }>;
  active_tenant_id: string | null;
};

export async function exchangeCode(code: string, codeVerifier?: string): Promise<AuthTokens> {
  const res = await fetch(`${API_BASE}/api/v1/auth/exchange`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, code_verifier: codeVerifier ?? null, redirect_uri: REDIRECT_URI }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? 'Token exchange failed');
  }
  const data = await res.json();
  return { accessToken: data.access_token as string, refreshToken: data.refresh_token ?? null };
}

export async function fetchMe(token: string): Promise<MeResponse> {
  const res = await fetch(`${API_BASE}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error('Failed to fetch user');
  return res.json() as Promise<MeResponse>;
}

export async function registerTenant(
  token: string,
  slug: string,
  name: string,
): Promise<{ tenant: { id: string; slug: string; name: string } }> {
  const res = await fetch(`${API_BASE}/api/v1/auth/register-tenant`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ slug, name }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail ?? 'Registration failed');
  }
  return res.json();
}
