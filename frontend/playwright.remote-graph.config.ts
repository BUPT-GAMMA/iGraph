import { defineConfig } from '@playwright/test';

if (!process.env.IGRAPH_E2E_BASE_URL) throw new Error('Set IGRAPH_E2E_BASE_URL to the actual Compose deployment. No fake backend is used.');
export default defineConfig({
  testDir: './integration', testMatch: 'remote-graph*.spec.ts', workers: 1, timeout: 420_000,
  reporter: 'line', outputDir: '../output/playwright/remote-graph-results',
  use: {
    baseURL: process.env.IGRAPH_E2E_BASE_URL,
    storageState: process.env.IGRAPH_E2E_STORAGE_STATE || undefined,
    // Disable automatic captures. Explicit screenshots are taken only with empty password fields or outside the form.
    trace: 'off', screenshot: 'off', video: 'off',
  },
});
