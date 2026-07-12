/**
 * StreamTracker unit tests — idempotent dedupe + cursor advance.
 *
 * Runs under Node's built-in test runner with TypeScript type-stripping
 * (`node --test src/tracker.test.ts`); `tracker.ts` has no value imports so it
 * loads standalone. Events mirror the wire shape the telemetry component
 * publishes (`{seq, id, stream, kind, body, meta, ts}`).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { StreamTracker, type TelemetryEvent } from './tracker.ts';

function ev(seq: number, id: string, body = ''): TelemetryEvent {
  return { seq, id, stream: 's', kind: 'k', body, meta: {}, ts: seq };
}

test('accept yields each event once and advances the cursor', () => {
  const t = new StreamTracker();
  const fresh = t.accept([ev(1, 'a'), ev(2, 'b'), ev(3, 'c')]);
  assert.deepEqual(fresh.map((e) => e.id), ['a', 'b', 'c']);
  assert.deepEqual(t.cursor, { after_seq: 3, after_id: 'c' });
});

test('the backfill/live overlap dedupes by id', () => {
  const t = new StreamTracker();
  // Backfill delivers a, b.
  t.accept([ev(1, 'a'), ev(2, 'b')]);
  // Live re-delivers b (overlap) then a genuinely new c.
  const fresh = t.accept([ev(2, 'b'), ev(3, 'c')]);
  assert.deepEqual(fresh.map((e) => e.id), ['c'], 'b was already seen');
  assert.deepEqual(t.cursor, { after_seq: 3, after_id: 'c' });
});

test('a lagged reconnect from the cursor replays without duplicates', () => {
  const t = new StreamTracker();
  t.accept([ev(1, 'a'), ev(2, 'b'), ev(3, 'c')]);
  // Reconnect: server replays the tail from after_seq=3 — but a slow path could
  // re-send c plus the missed d. c dedupes; only d is fresh.
  const fresh = t.accept([ev(3, 'c'), ev(4, 'd')]);
  assert.deepEqual(fresh.map((e) => e.id), ['d']);
  assert.deepEqual(t.cursor, { after_seq: 4, after_id: 'd' });
});

test('an initial cursor is honored and out-of-order seqs do not regress it', () => {
  const t = new StreamTracker({ after_seq: 10, after_id: 'x' });
  // A late/duplicate low-seq event is still deduped by id if seen; an unseen
  // low-seq event passes through but must not move the cursor backwards.
  const fresh = t.accept([ev(5, 'low'), ev(11, 'high')]);
  assert.deepEqual(fresh.map((e) => e.id), ['low', 'high']);
  assert.deepEqual(t.cursor, { after_seq: 11, after_id: 'high' }, 'cursor never regresses');
});

test('events without a string id are skipped', () => {
  const t = new StreamTracker();
  const bad = { seq: 1, stream: 's', kind: 'k', body: '', meta: {}, ts: 1 } as unknown as TelemetryEvent;
  assert.deepEqual(t.accept([bad]), []);
  assert.deepEqual(t.cursor, {});
});
