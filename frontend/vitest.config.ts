import { defineConfig } from 'vitest/config';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { sveltekit } from '@sveltejs/kit/vite';

// SvelteKit's vite plugin resolves `$lib`, `$app`, and the alias map
// configured in svelte.config.js. Without it, `import.meta.glob('/src/lib/...')`
// works but `$app/paths` imports inside components blow up at test load.
export default defineConfig({
  plugins: [sveltekit()],
  test: {
    environment: 'jsdom',
    globals: false,
    include: ['src/**/*.test.ts'],
    setupFiles: [],
  },
  resolve: {
    conditions: ['browser'],
  },
});
