// SPA shell — disable SSR; SvelteKit runs purely client-side, adapter-static
// emits a single index.html fallback.
export const ssr = false;
export const prerender = false;
export const trailingSlash = 'ignore';
