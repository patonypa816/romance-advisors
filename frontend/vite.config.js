import { defineConfig } from 'vite';
export default defineConfig({
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: process.env.HELL_INC_BACKEND_URL || 'http://127.0.0.1:8080', changeOrigin: true } } },
});
