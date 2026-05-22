import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// SvelteKit config. Phase 6 ships a single-page app served by FastAPI
// StaticFiles under /ui. adapter-static with a fallback turns the build
// into a plain dist tree that the Python wheel ships and uvicorn mounts.
//
// `paths.base = '/ui'` makes every link + asset path relative to /ui,
// matching the mount point. Prerendering is disabled — every route
// resolves through the bundled JS (no SSR HTML files generated).

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({
      pages: '../media_engine/web/dist',
      assets: '../media_engine/web/dist',
      fallback: 'index.html',
      precompress: false,
      strict: false,
    }),
    paths: {
      base: '/ui',
      relative: false,
    },
    prerender: {
      entries: [],
    },
  },
};

export default config;
