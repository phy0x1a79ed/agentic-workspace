import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({
      pages: '../build',
      assets: '../build',
      precompress: false,
      strict: false
    }),
    paths: {
      base: '/vertical-stripes/ptt'
    }
  }
};

export default config;
