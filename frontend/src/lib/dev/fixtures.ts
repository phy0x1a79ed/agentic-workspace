// Shared fixture discovery — used by /dev routes and the vitest runner.
//
// Convention (see infra-dev-components/.awm/context.md):
//   `<Name>.fixtures.ts` lives next to `<Name>.svelte` and exports:
//     - `default`: Record<variantName, props>
//     - `component`: the Svelte component to mount
//
// Components register themselves by dropping a sibling `.fixtures.ts` file.
// No central registry; no per-component test boilerplate.

export interface FixtureModule {
  /** Variant-name → props map. */
  default: Record<string, Record<string, unknown>>;
  /** The Svelte component to mount. */
  component: unknown;
}

export interface FixtureEntry {
  /** Absolute glob path (e.g. /src/lib/components/StatusTag.fixtures.ts). */
  path: string;
  /** Filename without extension (e.g. StatusTag). */
  name: string;
  /** Route slug (e.g. status-tag). */
  slug: string;
  module: FixtureModule;
}

const modules = import.meta.glob<FixtureModule>(
  '/src/lib/components/*.fixtures.ts',
  { eager: true },
);

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
