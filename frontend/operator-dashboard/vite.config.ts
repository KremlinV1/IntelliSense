import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// The dashboard talks to the confirmation service at :8080.
// In dev we proxy both the REST endpoints and the WebSocket so the browser
// never sees a cross-origin request — cleaner than enabling CORS for every
// developer's machine.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
      '/ws': {
        target: 'ws://localhost:8080',
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
