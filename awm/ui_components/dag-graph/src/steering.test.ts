/**
 * deriveSteering unit tests — the chip-state derivation from the durable poll
 * bits. Runs under Node's built-in test runner with TS type-stripping
 * (`node --test src/steering.test.ts`); steering.ts has only a type-only import
 * so it loads standalone.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { deriveSteering, type SteerFields } from './steering.ts';

function chip(f: SteerFields) {
  return deriveSteering(f);
}

test('default (no bits) is viewing', () => {
  const c = chip({});
  assert.equal(c.state, 'viewing');
  assert.equal(c.label, 'viewing');
  assert.equal(c.tone, 'neutral');
  assert.equal(c.attached, false);
});

test('steer_requested while detached is wants-steering', () => {
  const c = chip({ steer_requested: true });
  assert.equal(c.state, 'wants_steering');
  assert.equal(c.label, 'wants steering');
  assert.equal(c.tone, 'warn');
  assert.equal(c.wantsSteering, true);
});

test('attached (no consent bits) is attached', () => {
  const c = chip({ attached: true });
  assert.equal(c.state, 'attached');
  assert.equal(c.attached, true);
  assert.equal(c.agentReady, false);
  assert.equal(c.userDone, false);
});

test('attached + agent-ready surfaces the sub-flag but stays attached', () => {
  const c = chip({ attached: true, steer_agent_ready: true });
  assert.equal(c.state, 'attached');
  assert.equal(c.agentReady, true);
  assert.equal(c.userDone, false);
});

test('attached + you-done surfaces the sub-flag but stays attached', () => {
  const c = chip({ attached: true, steer_user_done: true });
  assert.equal(c.state, 'attached');
  assert.equal(c.userDone, true);
  assert.equal(c.agentReady, false);
});

test('both consent bits set → detaching (handshake about to fire)', () => {
  const c = chip({ attached: true, steer_agent_ready: true, steer_user_done: true });
  assert.equal(c.state, 'detaching');
  assert.equal(c.label, 'detaching…');
  assert.equal(c.tone, 'ok');
  assert.equal(c.agentReady, true);
  assert.equal(c.userDone, true);
});

test('attached dominates a stale wants-steering flag', () => {
  // Attach clears wants-steering server-side, but a poll could momentarily carry
  // both — attached must win the headline.
  const c = chip({ attached: true, steer_requested: true });
  assert.equal(c.state, 'attached');
  assert.equal(c.wantsSteering, true);
});
