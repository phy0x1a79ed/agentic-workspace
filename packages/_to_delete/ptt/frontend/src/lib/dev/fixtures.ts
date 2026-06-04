// Shared fixture discovery — used by /dev routes and the vitest runner.
//
// Convention: `<Name>.fixtures.ts` lives next to `<Name>.svelte` and exports:
//   - default: Record<variantName, props>
//   - component: the Svelte component to mount
//
// Glob path resolves from packages/ptt/frontend/ up 1 → packages/ptt/ →
// down 1 → components/. The dev surface adds/removes fixtures purely by
// adding/removing files; no central registry.

export interface FixtureModule {
  default: Record<string, Record<string, unknown>>;
  component: unknown;
}

export interface FixtureEntry {
  path: string;
  name: string;
  slug: string;
  module: FixtureModule;
}

const modules = {
  ...import.meta.glob<FixtureModule>('/../components/*.fixtures.ts', { eager: true }),
  ...import.meta.glob<FixtureModule>('/src/lib/primitives/*.fixtures.ts', { eager: true }),
};

function deriveName(path: string): string {
  const file = path.split('/').pop() ?? path;
  return file.replace(/\.fixtures\.ts$/, '');
}

function deriveSlug(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase();
}

export const fixtureEntries: FixtureEntry[] = Object.entries(modules)
  .map(([path, mod]) => {
    const name = deriveName(path);
    return { path, name, slug: deriveSlug(name), module: mod };
  })
  .sort((a, b) => a.slug.localeCompare(b.slug));

export function findBySlug(slug: string): FixtureEntry | undefined {
  return fixtureEntries.find((e) => e.slug === slug);
}
