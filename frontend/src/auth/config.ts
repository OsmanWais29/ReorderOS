export const WORKOS_CLIENT_ID = 'client_01KQT0CRCZP8W8AMAYE5Y1F9SP';
export const WORKOS_AUTH_URL = 'https://api.workos.com/user_management/authorize';

// API base: EXPO_PUBLIC_API_BASE lets a dev/staging run point elsewhere without
// editing source (e.g. EXPO_PUBLIC_API_BASE=https://reorderos-staging-... npx expo
// start --web). Defaults to production.
export const API_BASE =
  process.env.EXPO_PUBLIC_API_BASE ?? 'https://reorderos-api-7d4et.ondigitalocean.app';

// Redirect URI WorkOS will send the user back to after login.
// Must be added in WorkOS Dashboard → Authentication → Redirects.
export const REDIRECT_URI = 'http://localhost:8081/auth/callback';
