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
import { TranscriptFold, parseHumanFrame } from './agent-acts.ts';

const AUTHOR = 'agent:dev';

/** A human-origin act as the agents service records it (record_in: direction in). */
function humanAct(
  body: string,
  ts: number,
  opts: { id?: string; kind?: string; injection?: boolean } = {},
): AgentAct {
  return {
    id: opts.id ?? `in-${ts}`,
    kind: opts.kind ?? 'message',
    body,
    meta: { direction: 'in', injection: !!opts.injection },
    ts,
  } as AgentAct;
}

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

test('parseHumanFrame splits the [from:…] frame into author + clean body', () => {
  assert.deepEqual(parseHumanFrame('[from:user:operator]\nhello there'), {
    author: 'user:operator',
    body: 'hello there',
  });
  assert.deepEqual(parseHumanFrame('[notify:user:op]\nstop'), {
    author: 'user:op',
    body: 'stop',
  });
  // Unframed body falls back to a generic human author, body untouched.
  assert.deepEqual(parseHumanFrame('raw'), { author: 'user:human', body: 'raw' });
});

test('a human-origin act renders as the human turn, not the agent voice', () => {
  const fold = new TranscriptFold(AUTHOR);
  const spoken = fold.push(humanAct('[from:user:operator]\nping', 100, { id: 'in1' }));

  assert.equal(fold.posts.length, 1);
  assert.equal(fold.posts[0].author, 'user:operator', 'authored by the human, not agent:dev');
  assert.equal(fold.posts[0].body, 'ping', 'frame stripped for display');
  assert.equal(fold.posts[0].kind, 'text');
  assert.equal(spoken, null, 'the human turn is never auto-spoken');
});

test('human turn interleaves with the agent reply by ts, deduped by id', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(humanAct('[from:user:op]\nquestion?', 100, { id: 'in1' }));
  fold.push(act('message', 'answer', 200, { id: 'm1', message_id: 'm1' }));
  fold.push(humanAct('[from:user:op]\nquestion?', 100, { id: 'in1' })); // backfill dup

  assert.deepEqual(fold.posts.map((p) => p.body), ['question?', 'answer']);
  assert.deepEqual(fold.posts.map((p) => p.author), ['user:op', AUTHOR]);
});

// ---------------------------------------------------------------------------
// Status lifecycle — user chip (sending→sent→received→failed) + tool chip
// ---------------------------------------------------------------------------

/** A tool act carrying the agentcore correlation data (id / tool_use_id). */
function toolUse(name: string, ts: number, useId: string): AgentAct {
  return {
    id: `tu-${useId}`, kind: 'tool_use', body: `[tool_use: ${name}]`,
    meta: { data: { name, id: useId } }, ts,
  } as AgentAct;
}
function toolResult(ts: number, useId: string, isError = false): AgentAct {
  return {
    id: `tr-${useId}`, kind: 'tool_result', body: 'output',
    meta: { data: { tool_use_id: useId, is_error: isError } }, ts,
  } as AgentAct;
}

test('optimistic chip goes sending → sent → received, one row throughout', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.pushOptimistic('cid-1', 'ping', 'user:op');
  assert.equal(fold.posts.length, 1);
  assert.equal(fold.posts[0].status, 'sending');
  assert.equal(fold.posts[0].id, 'cid-1');

  // The agents service records the human turn back under the SAME id (cid).
  fold.push(humanAct('[from:user:op]\nping', 1_000, { id: 'cid-1' }));
  assert.equal(fold.posts.length, 1, 'reconciles in place — no second row');
  assert.equal(fold.posts[0].status, 'sent');
  assert.equal(fold.posts[0].body, 'ping');

  // The agent's next act proves the model consumed the input.
  fold.push(act('message', 'pong', 2_000, { id: 'm1', message_id: 'm1' }));
  assert.equal(fold.posts.find((p) => p.id === 'cid-1')?.status, 'received');
});

test('a rejected enqueue marks the optimistic chip failed', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.pushOptimistic('cid-x', 'ping', 'user:op');
  fold.markStatus('cid-x', 'failed');
  assert.equal(fold.posts[0].status, 'failed');
});

test('reconcile advances even when the delivered ts predates the chip', () => {
  // Loopback clock skew: the server's delivered ts is earlier than the local
  // optimistic ts. The stale-drop must be bypassed for the human reconcile, or
  // the chip would strand at `sending`.
  const fold = new TranscriptFold(AUTHOR);
  fold.pushOptimistic('cid-2', 'ping', 'user:op');
  const staleTs = 1; // far older than Date.now() used by pushOptimistic
  fold.push(humanAct('[from:user:op]\nping', staleTs, { id: 'cid-2' }));
  assert.equal(fold.posts.length, 1);
  assert.equal(fold.posts[0].status, 'sent', 'reconcile is not dropped as stale');
});

test('backfill of a finished task derives received with no optimistic chip', () => {
  const fold = new TranscriptFold(AUTHOR);
  // Replayed straight from the transcript: human turn then a later agent act.
  fold.push(humanAct('[from:user:op]\nq', 100, { id: 'in1' }));
  fold.push(act('message', 'a', 200, { id: 'm1', message_id: 'm1' }));
  assert.equal(fold.posts.find((p) => p.id === 'in1')?.status, 'received');
});

test('a tool call flips running → done when its result lands', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(toolUse('Bash', 100, 'use-1'));
  assert.equal(fold.posts.find((p) => p.id === 'tu-use-1')?.status, 'running');
  fold.push(toolResult(150, 'use-1'));
  assert.equal(fold.posts.find((p) => p.id === 'tu-use-1')?.status, 'done');
});

test('a failed tool result flips its call to error', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(toolUse('Bash', 100, 'use-2'));
  fold.push(toolResult(150, 'use-2', true));
  assert.equal(fold.posts.find((p) => p.id === 'tu-use-2')?.status, 'error');
});

/** The transient turn-start "working" signal (status act, meta.phase=working). */
function workingAct(ts: number): AgentAct {
  return { id: `w-${ts}`, kind: 'status', body: '', meta: { phase: 'working' }, ts } as AgentAct;
}

test('working signal renders a skeleton row, cleared by the first reply', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(workingAct(100));
  assert.equal(fold.posts.length, 1, 'the skeleton shows while the agent acts');
  assert.equal(fold.posts[0].status, 'working');
  // The turn's first real content retires the skeleton in place of a reply row.
  const spoken = fold.push(act('message', 'hi', 200, { id: 'm1', message_id: 'm1' }));
  assert.equal(fold.posts.length, 1, 'skeleton replaced, not stacked');
  assert.equal(fold.posts[0].status ?? '', '', 'the reply is a normal row');
  assert.equal(fold.posts[0].body, 'hi');
  assert.ok(spoken, 'the reply still auto-speaks');
});

test('working skeleton is cleared by a tool call (a content-less turn)', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(workingAct(100));
  fold.push(toolUse('Bash', 150, 'use-w'));
  assert.equal(fold.posts.find((p) => p.status === 'working'), undefined,
    'a tool_use retires the skeleton');
  assert.equal(fold.posts.find((p) => p.id === 'tu-use-w')?.status, 'running');
});

test('working skeleton is cleared by the terminal result', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(workingAct(100));
  fold.push(act('result', 'done', 200, { id: 'r1' }));
  assert.equal(fold.posts.length, 0, 'result with no text still clears the skeleton');
});

test('a new working signal replaces the prior skeleton (one at most)', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(workingAct(100));
  fold.push(workingAct(200));
  const skeletons = fold.posts.filter((p) => p.status === 'working');
  assert.equal(skeletons.length, 1, 'never more than one skeleton row');
});

test('clearWorking drops a lingering skeleton (the lagged/reconnect path)', () => {
  const fold = new TranscriptFold(AUTHOR);
  fold.push(workingAct(100));
  assert.equal(fold.posts.length, 1);
  fold.clearWorking();
  assert.equal(fold.posts.length, 0);
});

test('the human turn does NOT clear the working skeleton (it precedes it)', () => {
  const fold = new TranscriptFold(AUTHOR);
  // Human turn lands, then the working signal for the turn it triggered.
  fold.push(humanAct('[from:user:op]\nq', 100, { id: 'in1' }));
  fold.push(workingAct(110));
  assert.ok(fold.posts.find((p) => p.status === 'working'), 'skeleton survives the human turn');
});
