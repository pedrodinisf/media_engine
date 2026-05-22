import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// Vite + SvelteKit + Tailwind v4. The Tailwind plugin processes
// @import "tailwindcss" inside app.css and emits utility classes from
// the @theme block. No tailwind.config.js needed under v4.

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    // Same-origin in production (FastAPI mounts the build under /ui).
    // Dev mode proxies API calls to a running `med api start --port 8000`.
    proxy: {
      '/run': 'http://localhost:8000',
      '/pipelines': 'http://localhost:8000',
      '/jobs': 'http://localhost:8000',
      '/artifacts': 'http://localhost:8000',
      '/profiles': 'http://localhost:8000',
      '/operations': 'http://localhost:8000',
      '/backends': 'http://localhost:8000',
      '/tokens': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/ready': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/unit/setup.ts'],
    include: ['tests/unit/**/*.test.ts'],
  },
});
