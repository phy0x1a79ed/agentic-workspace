import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { fileURLToPath, URL } from 'node:url';

// Relative base so the same bundle works at any hub-assigned prefix
// (per AGENTS.md § "Authoring a frontend stripe").
export default defineConfig({
  plugins: [svelte()],
  base: './',
  resolve: {
    alias: {
      $lib: fileURLToPath(new URL('./src/lib', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
  },
});
