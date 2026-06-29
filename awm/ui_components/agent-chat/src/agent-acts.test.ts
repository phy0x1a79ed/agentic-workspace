/**
 * TranscriptFold unit tests — idempotent, time-ordered streaming dedupe.
 *
 * Runs under Node's built-in test runner with TypeScript type-stripping
 * (`node --test src/agent-acts.test.ts`); `agent-acts.ts` has only type-only
 * imports so it loads standalone. The acts here mirror the wire shape the
 * agents service publishes (`{id, kind, body, meta, ts}`).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { AgentAct } from '@awm/client';
import { TranscriptFold } from './agent-acts.ts';

const AUTHOR = 'agent:dev';

/** A streamed act with `message_id` in meta (the coalesce key). */
function act(
  kind: string,
  body: string,
  ts: number,
  opts: { id?: string; message_id?: string } = {},
): AgentAct {
  const meta = opts.message_id ? { message_id: opts.message_id } : {};
  return { id: opts.id ?? `${kind}-${ts}`, kind, body, meta, ts } as AgentAct;
}

test('a streamed reply folds to one growing-then-settled row', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(act('partial', 'An', 100, { id: 'p1', message_id: 'm1' }));
  fold.push(act('partial', 'Anytime', 101, { id: 'p2', message_id: 'm1' }));
  const spoken = fold.push(act('message', 'Anytime! 👋', 102, { id: 'm1', message_id: 'm1' }));

  assert.equal(fold.posts.length, 1, 'one chat row, no fragments');
  assert.equal(fold.posts[0].body, 'Anytime! 👋', 'settles on final text');
  assert.ok(spoken, 'the finalized message is returned for auto-speak');
  assert.equal(spoken?.body, 'Anytime! 👋');
});

test('partials never trigger speech', () => {
  const fold = new TranscriptFold(AUTHOR);
  assert.equal(fold.push(act('partial', 'An', 100, { id: 'p1', message_id: 'm1' })), null);
});

test('shuffled / duplicated / stale updates yield a single stable row', () => {
  const fold = new TranscriptFold(AUTHOR);
  // Final arrives first, then earlier partials (out of order), plus a duplicate.
  fold.push(act('message', 'Hello world', 200, { id: 'm1', message_id: 'm1' }));
  fold.push(act('partial', 'Hel', 100, { id: 'p1', message_id: 'm1' })); // stale
  fold.push(act('partial', 'Hello', 150, { id: 'p2', message_id: 'm1' })); // stale
  fold.push(act('message', 'Hello world', 200, { id: 'm1', message_id: 'm1' })); // duplicate

  assert.equal(fold.posts.length, 1);
  assert.equal(fold.posts[0].body, 'Hello world', 'stale updates never regress the row');
});

test('result is hidden (no duplicate final line)', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(act('message', 'done', 100, { id: 'm1', message_id: 'm1' }));
  fold.push(act('result', 'done', 101, { id: 'r1' }));

  assert.equal(fold.posts.length, 1, 'the result echo does not render a second row');
  assert.equal(fold.posts[0].body, 'done');
});

test('distinct messages slot in by time', () => {
  const fold = new TranscriptFold(AUTHOR);
  // Fold a later message first, then an earlier one — render order is by ts.
  fold.push(act('message', 'second', 200, { id: 'm2', message_id: 'm2' }));
  fold.push(act('message', 'first', 100, { id: 'm1', message_id: 'm1' }));

  assert.deepEqual(fold.posts.map((p) => p.body), ['first', 'second']);
});

test('reconnect (backfill + live overlap) is idempotent', () => {
  const fold = new TranscriptFold(AUTHOR);
  // Live stream.
  fold.push(act('partial', 'Hi', 100, { id: 'p1', message_id: 'm1' }));
  fold.push(act('message', 'Hi there', 101, { id: 'm1', message_id: 'm1' }));
  const before = fold.posts.map((p) => ({ ...p }));

  // Reconnect replays the finalized row from backfill (same id + ts).
  fold.push(act('message', 'Hi there', 101, { id: 'm1', message_id: 'm1' }));

  assert.equal(fold.posts.length, 1);
  assert.deepEqual(fold.posts.map((p) => p.body), before.map((p) => p.body));
});

test('tool acts stay their own rows, keyed by act id', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(act('tool_use', '[tool_use: Bash]', 100, { id: 't1', message_id: 'm1' }));
  fold.push(act('tool_use', '[tool_use: Bash]', 100, { id: 't1', message_id: 'm1' })); // dup
  fold.push(act('message', 'result text', 101, { id: 'm1', message_id: 'm1' }));

  assert.equal(fold.posts.length, 2, 'tool row + message row, dup deduped');
  assert.equal(fold.posts[0].kind, 'tool_use');
});

test('cursor tracks the most recent act, including hidden ones', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(act('message', 'hi', 100, { id: 'm1', message_id: 'm1' }));
  fold.push(act('status', '', 105, { id: 's1' }));

  assert.equal(fold.lastId, 's1');
  assert.equal(fold.lastTs, new Date(105).toISOString());
});
