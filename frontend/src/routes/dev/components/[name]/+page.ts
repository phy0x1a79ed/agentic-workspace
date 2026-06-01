import type { EntryGenerator } from './$types';

// Build-time enumeration of fixture slugs so SvelteKit prerenders one
// canonical .html per component. Mirrors the glob + slug derivation in
// $lib/dev/fixtures.ts — if you add a new fixture source there, add it
// here too.
const fixtureFiles = import.meta.glob('/src/lib/components/*.fixtures.ts');

export const prerender = true;

export const entries: EntryGenerator = () =>
  Object.keys(fixtureFiles).map((p) => {
    const file = p.split('/').pop() ?? p;
    const name = file.replace(/\.fixtures\.ts$/, '');
    const slug = name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
    return { name: slug };
  });
