import { defineConfig } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';

// Stripe build for @awm/primitives. Output goes to ./dist, which is committed
// and served verbatim by the hub at the /primitives prefix (kind=stripe,
// frontend-only). base='./' keeps asset URLs relative so the bundle works at
// any prefix in case it ever gets remounted.
export default defineConfig({
  base: './',
  plugins: [svelte({ preprocess: vitePreprocess() })],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
