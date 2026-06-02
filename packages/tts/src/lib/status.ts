/**
 * Round-trip status of a single speak/replay call, owned by App.svelte
 * and rendered by TtsConfig's composer pill.
 *
 * `tick` on the `sending` variant is updated by App.svelte on a timer
 * so the elapsed counter stays live without the component itself
 * needing a setInterval.
 */
export type CallStatus =
  | { kind: 'idle' }
  | { kind: 'sending'; startedAt: number; tick: number }
  | { kind: 'done'; latencyMs: number }
  | { kind: 'cancelled' }
  | { kind: 'error'; message: string };
