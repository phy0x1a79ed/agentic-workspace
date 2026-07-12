// Single root Vite config for the whole awm frontend tree.
//
// Every page builds with this one config; there is no per-page vite.config.ts.
// The build loop (scripts/build.sh) runs `vite build` with cwd = the page dir,
// so the page is the Vite project root (its index.html is the entry, its
// src/lib is $lib). Shared components are resolved by an `@awm/*` path alias —
// the bundler analogue of the Python dist-root glob (`from awm.x import …`):
// a component is just `ui_components/<name>/src`, imported by name with no
// per-package manifest. tsconfig.json carries the same `@awm/*` mapping for
// tsc/svelte-check, so the IDE and the bundler agree on one rule.
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Workspace root = this file's dir (always awm/), independent of which page is
// being built (that's process.cwd()).
const root = path.dirname(fileURLToPath(import.meta.url));
const components = path.join(root, 'ui_components');
const lib = path.resolve(process.cwd(), 'src/lib');

export default defineConfig({
  base: './',
  // cwd is the page dir (set by the build loop); make it the project root so
  // Vite picks up the page's index.html and resolves $lib page-locally.
  root: process.cwd(),
  resolve: {
    // Array form so we can order subpath-before-bare; first match wins.
    alias: [
      // `$lib/...` → `<page>/src/lib/...` (SvelteKit-style, page-local).
      { find: '$lib', replacement: lib },
      // `@awm/<name>/<sub>` → ui_components/<name>/src/<sub>  (subpath FIRST,
      // so `@awm/primitives/style.css` doesn't match the bare rule).
      { find: /^@awm\/([^/]+)\/(.+)$/, replacement: `${components}/$1/src/$2` },
      // `@awm/<name>` → ui_components/<name>/src/index.ts  (bare entry; the
      // target is explicit because alias targets don't auto-append extensions).
      { find: /^@awm\/([^/]+)$/, replacement: `${components}/$1/src/index.ts` },
    ],
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      treeshake: {
        // Treat component source (the barrel .ts and the .svelte modules) as
        // side-effect-free so a page only bundles the components it imports —
        // unused re-exports from a barrel prune away. This reproduces what
        // node_modules `sideEffects` gave the old per-package model, which
        // Vite honors only under node_modules; aliased first-party source
        // would otherwise be assumed side-effectful and bundle every component.
        // Query-bearing virtual CSS modules (…?…lang.css) and real .css files
        // keep side effects, so a USED component's compiled <style> and an
        // explicit `import '@awm/primitives/style.css'` always survive.
        moduleSideEffects: (id) => {
          const bare = id.split('?')[0];
          if (bare.includes('/ui_components/') &&
              (bare.endsWith('.ts') || bare.endsWith('.svelte'))) {
            return false;
          }
          return true;
        },
      },
    },
  },
  plugins: [svelte()],
});
