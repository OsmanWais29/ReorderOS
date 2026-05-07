// PKCE helpers using the Web Crypto API (available in browsers and Expo web).

function base64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let str = '';
  bytes.forEach((b) => (str += String.fromCharCode(b)));
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

export async function generatePKCE(): Promise<{ verifier: string; challenge: string }> {
  const array = new Uint8Array(64);
  crypto.getRandomValues(array);
  const verifier = base64url(array.buffer);
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const challenge = base64url(hash);
  return { verifier, challenge };
}
