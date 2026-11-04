// Minimal Svelte config consumed ONLY by `svelte-check` (see the `typecheck`
// npm script). It exists at the workspace root so svelte-check loads this file
// instead of probing for a Vite config: vite 5.x lacks the `resolveConfig`
// export that @sveltejs/load-config expects, so that probe throws
// "vite.resolveConfig is not a function". The build uses vite.config.ts.
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

export default {
  preprocess: vitePreprocess(),
};
