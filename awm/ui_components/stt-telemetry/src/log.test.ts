import { test } from 'node:test';
import assert from 'node:assert/strict';
import { appendTelemetry } from './log.ts';
import type { TelemetryEvent } from './types.ts';

// Minimal event builder. `t` just increments per call so deltas are non-zero.
let clock = 0;
function ev(event: string, extra: Partial<TelemetryEvent> = {}): TelemetryEvent {
  clock += 1;
  return { t: clock, dir: 'in', event, ...extra };
}

function feed(events: TelemetryEvent[], ...es: TelemetryEvent[]): TelemetryEvent[] {
  let acc = events;
  for (const e of es) acc = appendTelemetry(acc, e);
  return acc;
}

test('a mixed run of audio_chunk / vad_poll / partial_pass collapses to one quiet row', () => {
  let log: TelemetryEvent[] = [];
  log = feed(
    log,
    ev('audio_chunk', { dir: 'out', bytes: 1024 }),
    ev('vad_poll', { dir: 'backend' }),
    ev('audio_chunk', { dir: 'out', bytes: 2048 }),
    ev('partial_pass', { dir: 'backend' }),
    ev('audio_chunk', { dir: 'out', bytes: 1024 }),
    ev('vad_poll', { dir: 'backend' }),
  );

  assert.equal(log.length, 1, 'six heartbeats coalesce to a single row');
  const row = log[0];
  assert.equal(row.event, 'quiet');
  assert.equal(row.dir, 'quiet', 'neutral direction for a mixed run');
  assert.equal(row.count, 6, 'count tracks total merged events');
  assert.deepEqual(row.tally, { audio_chunk: 3, vad_poll: 2, partial_pass: 1 });
  assert.equal(row.bytes, 1024 + 2048 + 1024, 'only audio chunks contribute bytes');
  // Detail summarizes every present type, audio first with its kb.
  assert.match(row.detail ?? '', /audio ×3 \(4\.0kb\)/);
  assert.match(row.detail ?? '', /vad ×2/);
  assert.match(row.detail ?? '', /partial ×1/);
});

test('an important event breaks the run, splitting two quiet rows', () => {
  let log: TelemetryEvent[] = [];
  log = feed(
    log,
    ev('audio_chunk', { dir: 'out', bytes: 512 }),
    ev('vad_poll', { dir: 'backend' }),
    // important — renders verbatim, breaks the run
    ev('silence_cut', { dir: 'backend', detail: 'cut' }),
    ev('audio_chunk', { dir: 'out', bytes: 512 }),
    ev('vad_poll', { dir: 'backend' }),
  );

  assert.equal(log.length, 3, 'quiet · silence_cut · quiet');
  assert.equal(log[0].event, 'quiet');
  assert.equal(log[0].count, 2);
  assert.equal(log[1].event, 'silence_cut', 'important event verbatim, not coalesced');
  assert.equal(log[1].dir, 'backend');
  assert.equal(log[2].event, 'quiet');
  assert.equal(log[2].count, 2, 'a fresh quiet run starts after the important event');
});

test('cap drops the oldest rows', () => {
  let log: TelemetryEvent[] = [];
  // 5 important (non-coalescing) events with cap=3 keeps only the last 3.
  for (let i = 0; i < 5; i++) {
    log = appendTelemetry(log, ev('composer', { detail: `m${i}` }), 3);
  }
  assert.equal(log.length, 3);
  assert.deepEqual(log.map((r) => r.detail), ['m2', 'm3', 'm4']);
});
