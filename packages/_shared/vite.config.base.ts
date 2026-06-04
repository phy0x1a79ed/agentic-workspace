// Shared Vite config base for every generated page in packages/pages/*.
// Per-page vite.config.ts is a one-liner re-export of this file; authors
// never edit either. See awm/services/packages/gen.py for the generator.
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  plugins: [svelte()],
});
