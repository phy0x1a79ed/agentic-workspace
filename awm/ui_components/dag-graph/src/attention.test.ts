/**
 * deriveAttention unit tests — the attention strip's two-group derivation off
 * the same poll. Runs under Node's built-in test runner with TS type-stripping
 * (`node --test src/attention.test.ts`); attention.ts has only type-only imports
 * so it loads standalone.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { DagTask } from '@awm/client';
import { deriveAttention } from './attention.ts';

function task(id: string, extra: Partial<DagTask> = {}): DagTask {
  return {
    task_id: id, goal: id, title: '', tags: [], state: 'ready', is_root: false,
    mode: null, workspace_slug: null, agent_ref: null,
    created_at: 0, updated_at: 0, ...extra,
  };
}

// A fixed "now" (ms) so ages are deterministic; tasks stamp updated_at in
// unix SECONDS, so 1_000_000 s == 1_000_000_000 ms.
const NOW = 2_000_000_000; // ms

test('empty plan yields two empty groups', () => {
  const a = deriveAttention([], NOW);
  assert.deepEqual(a.wants, []);
  assert.deepEqual(a.broken, []);
});

test('wants group: steer_requested and attached; not the root', () => {
  const a = deriveAttention([
    task('root', { is_root: true, attached: true }),
    task('req', { steer_requested: true }),
    task('att', { attached: true }),
    task('idle'),
  ], NOW);
  assert.deepEqual(a.wants.map((e) => e.taskId).sort(), ['att', 'req']);
  assert.equal(a.broken.length, 0);
});

test('broken group: failed / abandoned only', () => {
  const a = deriveAttention([
    task('f', { state: 'failed' }),
    task('ab', { state: 'abandoned' }),
    task('ok', { state: 'completed' }),
    task('run', { state: 'active' }),
  ], NOW);
  assert.deepEqual(a.broken.map((e) => e.taskId).sort(), ['ab', 'f']);
  assert.equal(a.wants.length, 0);
});

test('a broken node is only broken, never also wants (terminal is not steering)', () => {
  const a = deriveAttention([
    task('x', { state: 'failed', steer_requested: true, attached: true }),
  ], NOW);
  assert.deepEqual(a.broken.map((e) => e.taskId), ['x']);
  assert.equal(a.wants.length, 0);
});

test('entries carry age (nowMs − updated_at·1000), oldest first', () => {
  const a = deriveAttention([
    task('new', { steer_requested: true, updated_at: 1_999_999_000 }), // 1s old
    task('old', { steer_requested: true, updated_at: 1_000_000_000 }), // ~1e9s old
  ], NOW);
  assert.deepEqual(a.wants.map((e) => e.taskId), ['old', 'new'], 'oldest leads');
  assert.equal(a.wants[1].ageMs, NOW - 1_999_999_000 * 1000);
});

test('label falls back goal → short id', () => {
  const a = deriveAttention([
    task('titled', { steer_requested: true, title: 'My Title', goal: 'g' }),
    task('goaled', { steer_requested: true, title: '', goal: 'a goal' }),
    task('abcdefgh1234', { steer_requested: true, title: '', goal: '' }),
  ], NOW);
  const byId = new Map(a.wants.map((e) => [e.taskId, e.label]));
  assert.equal(byId.get('titled'), 'My Title');
  assert.equal(byId.get('goaled'), 'a goal');
  assert.equal(byId.get('abcdefgh1234'), 'abcdefgh…');
});
