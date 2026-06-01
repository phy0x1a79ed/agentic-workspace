// CSR-only bundle prerendered to real files at every route. Runtime state
// (focused room id, opened room id) lives in query params so the URL ↔
// file mapping stays 1:1 — `/focus?room=x` and `/focus?room=y` both
// resolve to the same on-disk `focus.html`.
export const ssr = false;
export const prerender = true;
export const trailingSlash = 'always';
