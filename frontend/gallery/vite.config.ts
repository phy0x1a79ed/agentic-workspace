import { defineConfig } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Standalone Vite build for the comp-primitives gallery. Independent of the
// main SvelteKit app: this produces a small static bundle that gets registered
// with the awm hub via `awm hub register --dir ../awm/galleries/primitives`.
//
// Output goes to `awm/galleries/primitives/` — NOT under `awm/static/`, because
// the SvelteKit adapter-static cleans `awm/static/` on every main build and
// would wipe the gallery bundle.
export default defineConfig({
  root: __dirname,
  base: './',
  plugins: [svelte({ preprocess: vitePreprocess() })],
  build: {
    outDir: resolve(__dirname, '../../awm/galleries/primitives'),
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(__dirname, 'index.html'),
    },
  },
});
