// Playwright web regression — the review-screen conversion flow (no Jest in
// this repo; this is the browser-level truth). Boots the REAL stack locally:
//   - backend uvicorn on :8123 against the local test DB (LOCAL_DEV_AUTH=true,
//     APP_ENV=local — the double-gated dev sign-in path; inert on staging/prod)
//   - expo web on :8124 pointed at that backend
// Never touches staging or production.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  use: {
    baseURL: 'http://localhost:8124',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command:
        'cd ../backend && .venv/bin/python -m uvicorn app.main:app --port 8123',
      url: 'http://localhost:8123/health/ready',
      reuseExistingServer: true,
      timeout: 60_000,
      env: {
        APP_ENV: 'local',
        LOCAL_DEV_AUTH: 'true',
        DATABASE_URL: 'postgresql+asyncpg://reorderos:reorderos@localhost:5433/reorderos',
        TOKEN_ENCRYPTION_KEY: 'RVpMWWJmY0ZQVWJlYWNoVGVzdE9ubHlLZXlfMTIzNDU2Nzg=',
        CORS_ORIGINS: '["http://localhost:8124"]',
      },
    },
    {
      command: 'npx expo start --port 8124',
      url: 'http://localhost:8124',
      reuseExistingServer: true,
      timeout: 240_000,
      env: { EXPO_PUBLIC_API_BASE: 'http://localhost:8123' },
    },
  ],
});
