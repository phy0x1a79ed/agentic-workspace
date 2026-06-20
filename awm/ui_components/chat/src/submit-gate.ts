/**
 * Send-once gate — the single seam every chat-send intent passes through.
 *
 * The voice/text composer (`@awm/stt-composer`) emits two distinct send
 * signals: `onsend` (the SEND button) and `onText` (a finalized voice
 * utterance). After the per-mode fix in `SttComposer` these are mutually
 * exclusive within a turn, so a turn posts exactly once. This gate is the
 * *defense-in-depth* on top of that: it collapses a duplicate emit of the
 * **same trimmed text within a short window** down to one, and drops empty
 * sends — so even if both signals fire for one utterance (ordering races,
 * a future composer change), the chat posts a single turn.
 *
 * It is deliberately framework-free (no Svelte, injectable clock) so it is
 * unit-testable exactly like `TranscriptFold` in `@awm/agent-chat`.
 */

export interface SubmitGateOpts {
  /** Collapse window for an identical repeat, in ms. Default 1000. */
  windowMs?: number;
  /** Monotonic clock; injected in tests. Defaults to performance.now / Date.now. */
  now?: () => number;
}

/**
 * Returns a gate function: feed it the raw composer text, it returns the
 * trimmed text to send, or `null` when the send should be suppressed (empty,
 * or an identical repeat inside the window). A legitimate repeat *after* the
 * window passes through — the gate only kills same-turn duplicates, never a
 * deliberate "say it again."
 */
export function makeSubmitGate(opts: SubmitGateOpts = {}): (raw: string) => string | null {
  const windowMs = opts.windowMs ?? 1000;
  const now =
    opts.now ??
    (typeof performance !== 'undefined' && typeof performance.now === 'function'
      ? () => performance.now()
      : () => Date.now());

  let lastText = '';
  let lastAt = -Infinity;

  return function gate(raw: string): string | null {
    const t = (raw ?? '').trim();
    if (!t) return null;
    const at = now();
    if (t === lastText && at - lastAt < windowMs) return null;
    lastText = t;
    lastAt = at;
    return t;
  };
}
