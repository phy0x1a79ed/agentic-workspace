/**
 * makeSubmitGate unit tests — the send-once collapse.
 *
 * Runs under Node's built-in runner with TS type-stripping
 * (`node --test src/submit-gate.test.ts`); `submit-gate.ts` is framework-free.
 * The clock is injected so the window is deterministic (no real timers).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { makeSubmitGate } from './submit-gate.ts';

/** A controllable clock. */
function clock(start = 0) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

test('an identical repeat within the window is suppressed (the double-send)', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  // The classic double: voice utterance auto-fires, then SEND fires the same text.
  assert.equal(gate('hello there'), 'hello there', 'first send passes');
  assert.equal(gate('hello there'), null, 'same text immediately after is dropped');
});

test('an identical repeat AFTER the window passes through', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  assert.equal(gate('yes'), 'yes');
  c.advance(1500);
  assert.equal(gate('yes'), 'yes', 'a deliberate repeat later is not a duplicate');
});

test('different text back-to-back both pass', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  assert.equal(gate('first'), 'first');
  assert.equal(gate('second'), 'second');
});

test('empty / whitespace-only sends are dropped and do not arm the gate', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  assert.equal(gate(''), null);
  assert.equal(gate('   '), null);
  // An empty send must not have recorded state that suppresses the next real one.
  assert.equal(gate('real'), 'real');
});

test('returns trimmed text (so the caller posts the clean turn)', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  assert.equal(gate('  padded  '), 'padded');
  // The trimmed form is what's compared for the dedupe, too.
  assert.equal(gate('padded'), null, 'trim-equal repeat within window is a duplicate');
});

test('the window is measured edge-to-edge, not cumulative', () => {
  const c = clock();
  const gate = makeSubmitGate({ windowMs: 1000, now: c.now });
  assert.equal(gate('x'), 'x');
  c.advance(999);
  assert.equal(gate('x'), null, 'just inside the window → dropped');
  c.advance(2); // now 1001 past the first, but the gate compares to last accepted
  assert.equal(gate('x'), 'x', 'past the window from the last accepted → passes');
});
