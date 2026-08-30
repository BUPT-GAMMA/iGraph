import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
    plugins: [react()],
    test: {
        environment: 'jsdom',
        include: ['src/**/*.{test,spec}.{ts,tsx}'],
    },
    server: {
        port: 5173,
        proxy: { '/api': 'http://127.0.0.1:1109' },
    },
    build: { outDir: 'dist', emptyOutDir: true },
});
