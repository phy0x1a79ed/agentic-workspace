// Shared Vite config base for every generated page in packages/pages/*.
// Per-page vite.config.ts is a one-liner re-export of this file; authors
// never edit either. See awm/services/packages/gen.py for the generator.
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';

export default defineConfig({
  base: './',
  resolve: {
    // SvelteKit-style alias: `$lib/...` resolves to `<page>/src/lib/...`.
    // Vite runs with cwd at the page dir, so `src/lib` is unambiguous.
    alias: {
      $lib: path.resolve(process.cwd(), 'src/lib'),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  plugins: [svelte()],
});
