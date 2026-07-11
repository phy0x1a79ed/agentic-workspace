/**
 * The attention board is subsumed by /ui/fleet — fleet's "needs you" section IS
 * the board and keeps the same desktop-push behavior. This page now redirects
 * there. App.svelte is retained (for reference / rollback) but no longer mounted.
 */
const here = window.location.pathname;
const dest = here.replace(/notifications(\/?)(?:index\.html)?$/, 'fleet$1');
window.location.replace(
  (dest === here ? '../fleet/' : dest) + window.location.search + window.location.hash,
);
