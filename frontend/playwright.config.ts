import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: 'line',
  outputDir: '../output/playwright/test-results',
  use: {
    baseURL: 'http://127.0.0.1:15173',
    channel: 'chrome',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: 'node e2e/fake-backend.mjs',
      port: 11109,
      reuseExistingServer: false,
    },
    {
      command: 'pnpm exec vite --host 127.0.0.1 --port 15173',
      port: 15173,
      reuseExistingServer: false,
      env: { VITE_API_BASE: 'http://127.0.0.1:11109', VITE_SESSION_STATE_V2: 'true' },
    },
  ],
});
