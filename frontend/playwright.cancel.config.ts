import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './integration', testMatch: 'cancellation.spec.ts', timeout: 40_000, workers: 1,
  reporter: 'line', outputDir: '../output/playwright/cancellation-results',
  use: { baseURL: 'http://127.0.0.1:15175', channel: 'chrome', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: `${process.env.IGRAPH_TEST_PYTHON || '../.venv/bin/python'} integration/cancellation_backend.py`, port: 11129, reuseExistingServer: false },
    { command: 'pnpm exec vite --host 127.0.0.1 --port 15175', port: 15175, reuseExistingServer: false,
      env: { VITE_API_BASE: 'http://127.0.0.1:11129', VITE_SESSION_STATE_V2: 'true' } },
  ],
});
