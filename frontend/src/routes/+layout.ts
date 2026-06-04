// CSR-only bundle prerendered to real files at every route. Runtime state
// (focused room id, opened room id) lives in query params so the URL ↔
// file mapping stays 1:1 — `/focus?room=x` and `/focus?room=y` both
// resolve to the same on-disk `focus/index.html`. `trailingSlash: 'always'`
// makes SvelteKit emit each route as `<route>/index.html`, which is what
// the hub's directory-index resolution serves verbatim.
export const ssr = false;
export const prerender = true;
export const trailingSlash = 'always';
