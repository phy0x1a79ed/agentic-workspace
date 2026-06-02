import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Relative base so the same bundle works at any hub-assigned prefix
// (per AGENTS.md § "Authoring a frontend stripe").
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
  },
});
