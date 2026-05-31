import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const API_TARGET = process.env.AWM_API_TARGET || 'https://127.0.0.1:12101';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: '0.0.0.0',
    port: 12103,
    strictPort: true,
    // Allow Vite to read files from the scope-owned tree at the worktree
    // root (e.g. ../ptt/components/*) — needed by the fixtures glob.
    fs: { allow: ['..'] },
    proxy: {
      // Backend endpoints — anything that isn't a SvelteKit route or asset.
      '/auth':     { target: API_TARGET, changeOrigin: true, secure: false },
      '/invoke':   { target: API_TARGET, changeOrigin: true, secure: false },
      '/peer':     { target: API_TARGET, changeOrigin: true, secure: false },
      '/peers':    { target: API_TARGET, changeOrigin: true, secure: false },
      '/projects': { target: API_TARGET, changeOrigin: true, secure: false },
      '/scopes':   { target: API_TARGET, changeOrigin: true, secure: false },
      '/status':   { target: API_TARGET, changeOrigin: true, secure: false },
      '/rooms':    { target: API_TARGET, changeOrigin: true, secure: false },
      '/vagrant':  { target: API_TARGET, changeOrigin: true, secure: false },
      '/voice':    { target: API_TARGET, changeOrigin: true, secure: false, ws: true },
      '/ptt':      { target: API_TARGET, changeOrigin: true, secure: false, ws: true },
      '/ws':       { target: API_TARGET, changeOrigin: true, secure: false, ws: true }
    }
  },
  preview: { host: '0.0.0.0', port: 12103 }
});
