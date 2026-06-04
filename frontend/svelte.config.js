import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({
      pages: '../awm/static',
      assets: '../awm/static',
      precompress: false,
      strict: false
    }),
    paths: {
      base: '/ui'
    },
    prerender: {
      // dev/components/[name] enumerates fixtures via entries(); a scope
      // with zero fixtures emits zero pages for that route, which would
      // otherwise be treated as an "unseen route" error.
      handleUnseenRoutes: 'ignore'
    }
  }
};

export default config;
