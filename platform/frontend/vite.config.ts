import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      // Proxy API calls to backend services during development
      '/auth': {
        target: 'http://localhost:8090',
        changeOrigin: true,
      },
      '/v1': {
        target: 'http://localhost:8580',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://localhost:8580',
        changeOrigin: true,
      },
    },
  },
});
