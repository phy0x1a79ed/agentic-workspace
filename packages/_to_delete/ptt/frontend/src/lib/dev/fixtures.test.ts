// Generic crash-on-mount runner. Every <Name>.fixtures.ts under
// packages/ptt/components/ is discovered via import.meta.glob; each declared
// variant is mounted in jsdom. A component that throws on mount fails its
// variant's test — no per-component test file required.

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { fixtureEntries } from './fixtures';

describe('fixture discovery', () => {
  it('discovers fixture files via import.meta.glob', () => {
    expect(Array.isArray(fixtureEntries)).toBe(true);
  });
});

for (const entry of fixtureEntries) {
  describe(entry.path, () => {
    const variants = Object.entries(entry.module.default ?? {});
    if (variants.length === 0) {
      it.skip('has no variants', () => {});
      return;
    }
    for (const [variantName, props] of variants) {
      it(`mounts ${variantName} without throwing`, () => {
        expect(() =>
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          render(entry.module.component as any, { props: props as any }),
        ).not.toThrow();
      });
    }
  });
}
