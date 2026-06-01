import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// HMR proxy target: by default, the shared dev hub at :7821 (the
// projects/awm/dev sandbox). Override via AWM_API_TARGET if you've
// registered PTT against a different hub (e.g. a per-scope :7851).
const API_TARGET = process.env.AWM_API_TARGET || 'https://127.0.0.1:7821';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: '0.0.0.0',
    port: 12153,
    strictPort: true,
    // Allow Vite to read packages/ptt/components/ (one level above the
    // SvelteKit project root) — needed by the fixtures glob.
    fs: { allow: ['..'] },
    proxy: {
      '/ptt/_api': { target: API_TARGET, changeOrigin: true, secure: false, ws: true }
    }
  },
  preview: { host: '0.0.0.0', port: 12153 }
});
