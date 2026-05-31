// Generic crash-on-mount runner.
//
// Every `<Name>.fixtures.ts` under src/lib/components/ is discovered via
// import.meta.glob; each declared variant is mounted in jsdom. A component
// that throws on mount fails its variant's test — no per-component test
// file required. Adding a new fixture needs zero changes here.

import { describe, it, expect } from 'vitest';
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import { render } from '@testing-library/svelte';
import { fixtureEntries } from './fixtures';

// Always emit at least one suite so vitest doesn't fail on "no test suite found"
// when zero fixtures exist yet (i.e. before any comp-* scope ships one).
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
