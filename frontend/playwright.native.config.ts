import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './integration',
  testMatch: 'native-recovery.spec.ts',
  timeout: 30_000,
  workers: 1,
  reporter: 'line',
  outputDir: '../output/playwright/native-results',
  use: { baseURL: 'http://127.0.0.1:15174', channel: 'chrome', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: `${process.env.IGRAPH_TEST_PYTHON || '../.venv/bin/python'} integration/native_backend.py`, port: 11119, reuseExistingServer: false },
    { command: 'pnpm exec vite --host 127.0.0.1 --port 15174', port: 15174, reuseExistingServer: false,
      env: { VITE_API_BASE: 'http://127.0.0.1:11119', VITE_SESSION_STATE_V2: 'true' } },
  ],
});
