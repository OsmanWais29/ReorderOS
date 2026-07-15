// Silent session refresh — the fix for the 5-minute WorkOS access token.
//
// Access tokens are short-lived by design; the session lives in the ROTATED
// refresh token. Any API client that gets a 401 calls tryRefresh(): a
// single-flight (concurrent 401s share one refresh — WorkOS refresh tokens are
// single-use, so parallel refreshes would kill the session), which posts the
// stored refresh token to /auth/refresh, persists the new pair, notifies
// AuthContext, and returns the fresh access token for a one-shot retry.
// Returns null when the session is truly over → caller surfaces sign-in.

import { API_BASE } from './config';
import {
  clearRefreshToken,
  loadRefreshToken,
  saveRefreshToken,
  saveToken,
} from './storage';

type TokenListener = (accessToken: string) => void;
let listener: TokenListener | null = null;

/** AuthContext registers here so a background refresh updates React state. */
export function onTokenRefreshed(fn: TokenListener | null): void {
  listener = fn;
}

let inflight: Promise<string | null> | null = null;

async function doRefresh(): Promise<string | null> {
  const refreshToken = await loadRefreshToken();
  if (!refreshToken) return null;
  try {
    const res = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      // Session expired/revoked for real — clear so we don't loop.
      await clearRefreshToken();
      return null;
    }
    const data = (await res.json()) as { access_token: string; refresh_token: string | null };
    await saveToken(data.access_token);
    if (data.refresh_token) await saveRefreshToken(data.refresh_token);
    listener?.(data.access_token);
    return data.access_token;
  } catch {
    // Network hiccup: keep the refresh token, report no new access token.
    return null;
  }
}

/** Single-flight refresh; concurrent callers await the same attempt. */
export function tryRefresh(): Promise<string | null> {
  inflight ??= doRefresh().finally(() => {
    inflight = null;
  });
  return inflight;
}
