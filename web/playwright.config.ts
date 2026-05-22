import { defineConfig, devices } from '@playwright/test';

// Playwright e2e config. Commit 39 ships one smoke test that asserts
// the SvelteKit dev server boots and renders the shell. Real flows
// (upload → job → catalog) come online in commits 41-44 once the API
// surface they exercise lands.
//
// In CI we run against the built `media_engine/web/dist/` served by
// FastAPI; locally we run against `vite dev` so iteration is fast.

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: process.env.MEDIA_ENGINE_WEB_E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: process.env.MEDIA_ENGINE_WEB_E2E_BASE_URL
    ? undefined
    : {
        command: 'pnpm dev --port 5173',
        port: 5173,
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
