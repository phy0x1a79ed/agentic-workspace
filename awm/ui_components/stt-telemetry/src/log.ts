import type { TelemetryEvent } from './types';

const KB = 1024;

function chunkDetail(count: number, bytes: number): string {
  // ×42 (43.0kb) — count of coalesced chunks + their total size.
  return `×${count} (${(bytes / KB).toFixed(1)}kb)`;
}

// Push one telemetry event onto the log, coalescing audio-chunk bursts and
// capping total length. Kept pure (no module state) so the panel stays
// presentational and the owner just does `events = appendTelemetry(events, e)`.
//
// `audio_chunk` fires ~15×/sec while speaking — flooding the log and burying the
// interesting events. So a run of consecutive `audio_chunk` events collapses into
// a single in-place row that accumulates `count` and `bytes`; the first non-chunk
// event breaks the run and the next chunk starts a fresh burst. Every other event
// is appended verbatim. The `cap` keeps the array bounded (oldest dropped) so a
// long session can't grow it without limit.
export function appendTelemetry(
  events: TelemetryEvent[],
  e: TelemetryEvent,
  cap = 1000,
): TelemetryEvent[] {
  if (e.event === 'audio_chunk') {
    const last = events[events.length - 1];
    if (last && last.event === 'audio_chunk') {
      const count = (last.count ?? 1) + 1;
      const bytes = (last.bytes ?? 0) + (e.bytes ?? 0);
      // Replace the tail row in place (new array identity for reactivity), and
      // refresh `t` so the burst's timestamp tracks the latest chunk.
      const merged: TelemetryEvent = {
        ...last,
        t: e.t,
        count,
        bytes,
        detail: chunkDetail(count, bytes),
      };
      const next = events.slice(0, -1);
      next.push(merged);
      return next;
    }
    // First chunk of a new burst.
    const bytes = e.bytes ?? 0;
    const first: TelemetryEvent = {
      ...e,
      count: 1,
      bytes,
      detail: chunkDetail(1, bytes),
    };
    return [...events, first].slice(-cap);
  }
  return [...events, e].slice(-cap);
}
