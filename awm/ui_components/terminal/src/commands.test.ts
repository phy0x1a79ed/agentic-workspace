import { test } from 'node:test';
import assert from 'node:assert/strict';
import { COMMAND_GRID } from './commands.ts';
import { voiceAvailable } from './voice.ts';

test('command grid maps labels to the expected control bytes', () => {
  const by = Object.fromEntries(COMMAND_GRID.map((c) => [c.label, c.bytes]));
  assert.equal(by['Esc'], '\x1b');
  assert.equal(by['Ctrl-C'], '\x03');
  assert.equal(by['Enter'], '\r');
  assert.equal(by['⇧Tab'], '\x1b[Z');
  assert.equal(by['↑'], '\x1b[A');
  assert.equal(by['↓'], '\x1b[B');
  assert.equal(by['/compact'], '/compact\r');
  assert.equal(by['/clear'], '/clear\r');
});

test('destructive commands are flagged danger', () => {
  const danger = new Set(COMMAND_GRID.filter((c) => c.danger).map((c) => c.label));
  assert.ok(danger.has('Ctrl-C'));
  assert.ok(danger.has('/clear'));
});

test('voiceAvailable is false without a browser window (node)', () => {
  assert.equal(voiceAvailable(), false);
});
