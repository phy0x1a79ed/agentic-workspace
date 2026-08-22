import type { TelemetryEvent } from './types';

const KB = 1024;

// The high-frequency heartbeats that carry no per-event signal: raw audio frames
// (~15×/sec while speaking), the VAD silence poll, and the cosmetic whisper
// re-pass. They flood the log and bury the interesting rows, so a *run* of them —
// even interleaved across the three types — collapses into one summary row.
// Everything else (partial-with-text, silence_cut, composer, whisper_repass,
// llm_refine/convo_cut, submit, status, context, ready, error, …) is "important":
// it renders verbatim and breaks the run.
const NOISE = new Set(['audio_chunk', 'vad_poll', 'partial_pass']);

// Short label for each noise type inside the merged summary.
const NOISE_LABEL: Record<string, string> = {
  audio_chunk: 'audio',
  vad_poll: 'vad',
  partial_pass: 'partial',
};

// Build the summary detail from a quiet run's per-type tallies, e.g.
// `audio ×62 (180.0kb) · vad ×24 · partial ×30`. Only audio carries bytes.
function quietDetail(tally: Record<string, number>, bytes: number): string {
  const parts: string[] = [];
  for (const ev of ['audio_chunk', 'vad_poll', 'partial_pass']) {
    const n = tally[ev];
    if (!n) continue;
    parts.push(
      ev === 'audio_chunk'
        ? `${NOISE_LABEL[ev]} ×${n} (${(bytes / KB).toFixed(1)}kb)`
        : `${NOISE_LABEL[ev]} ×${n}`,
    );
  }
  return parts.join(' · ');
}

// Push one telemetry event onto the log, coalescing runs of low-value heartbeat
// events and capping total length. Kept pure (no module state) so the panel stays
// presentational and the owner just does `events = appendTelemetry(events, e)`.
//
// A consecutive run of NOISE events (audio_chunk / vad_poll / partial_pass, in any
// interleaving) collapses into a single `quiet` row that tracks per-type counts
// and total audio bytes; the first *important* event breaks the run and the next
// noise event starts a fresh `quiet` row. Important events are appended verbatim.
// The `cap` keeps the array bounded (oldest dropped) so a long session can't grow
// it without limit.
export function appendTelemetry(
  events: TelemetryEvent[],
  e: TelemetryEvent,
  cap = 1000,
): TelemetryEvent[] {
  if (NOISE.has(e.event)) {
    const last = events[events.length - 1];
    const addBytes = e.event === 'audio_chunk' ? (e.bytes ?? 0) : 0;
    if (last && last.event === 'quiet') {
      // Merge into the open quiet run: bump this type's tally, sum audio bytes,
      // refresh `t` so the row's timestamp tracks the latest heartbeat. New array
      // identity (and a fresh tally object) so reactivity fires.
      const tally = { ...(last.tally ?? {}) };
      tally[e.event] = (tally[e.event] ?? 0) + 1;
      const bytes = (last.bytes ?? 0) + addBytes;
      const merged: TelemetryEvent = {
        ...last,
        t: e.t,
        count: (last.count ?? 0) + 1,
        bytes,
        tally,
        detail: quietDetail(tally, bytes),
      };
      const next = events.slice(0, -1);
      next.push(merged);
      return next;
    }
    // First event of a new quiet run. Neutral `dir` since the run mixes outbound
    // audio with backend polls/passes.
    const tally: Record<string, number> = { [e.event]: 1 };
    const first: TelemetryEvent = {
      t: e.t,
      dir: 'quiet',
      event: 'quiet',
      count: 1,
      bytes: addBytes,
      tally,
      detail: quietDetail(tally, addBytes),
    };
    return [...events, first].slice(-cap);
  }
  return [...events, e].slice(-cap);
}
