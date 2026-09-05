import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reportsDirectory: 'coverage',
      reporter: ['text', 'json-summary', 'lcov', 'cobertura', 'html'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.{test,spec}.{ts,tsx}', 'src/vite-env.d.ts'],
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:1109' },
  },
  build: { outDir: 'dist', emptyOutDir: true },
});
