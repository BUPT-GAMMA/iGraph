import { defineConfig } from '@playwright/test';

if (!process.env.IGRAPH_E2E_BASE_URL) throw new Error('Set IGRAPH_E2E_BASE_URL to the actual Compose deployment. No fake backend is used.');
export default defineConfig({
  testDir: './integration', testMatch: 'remote-graph.spec.ts', workers: 1, timeout: 240_000,
  reporter: 'line', outputDir: '../output/playwright/remote-graph-results',
  use: {
    baseURL: process.env.IGRAPH_E2E_BASE_URL,
    storageState: process.env.IGRAPH_E2E_STORAGE_STATE || undefined,
    // Credential fields and authenticated traffic must never enter trace/video/screenshots.
    trace: 'off', screenshot: 'off', video: 'off',
  },
});
