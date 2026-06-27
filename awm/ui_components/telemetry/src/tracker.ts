/**
 * Pure idempotency core for a telemetry stream — no I/O, no external value
 * imports, so it loads standalone under Node's test runner (and keeps the
 * dedupe guarantee independently testable from the WS/poll transport).
 *
 * A telemetry event carries both an ordering key (`seq`) and a stable dedupe key
 * (`id`); `StreamTracker` filters a batch to the events it hasn't seen and
 * advances the cursor, so the backfill/live overlap and a lagged reconnect never
 * surface a duplicate.
 */

/** One telemetry event. `seq` orders; `id` dedupes. Shape matches the server. */
export interface TelemetryEvent {
  /** Monotonic ordering key (the server's table rowid). */
  seq: number;
  /** Stable uuid — the dedupe key across the backfill/live overlap. */
  id: string;
  stream: string;
  kind: string;
  body: string;
  meta?: Record<string, unknown>;
  /** unix-ms integer. */
  ts: number;
  [k: string]: unknown;
}

/** Sync cursor. Omit both fields for full-from-start. */
export interface StreamCursor {
  /** Only events strictly after this seq. */
  after_seq?: number;
  /** Last seen id — the idempotency tiebreak/resume point. */
  after_id?: string;
}

/**
 * The idempotency core: filters a batch of events to those not seen before and
 * advances the cursor. Pure (no I/O) so it's the unit-tested guarantee behind
 * both the poll and the live path — feed it backfill AND live frames and it
 * never yields the same `id` twice.
 */
export class StreamTracker {
  private seen = new Set<string>();
  cursor: StreamCursor;

  constructor(initial: StreamCursor = {}) {
    this.cursor = { ...initial };
  }

  /** Return the unseen subset (in input order), advancing the cursor. */
  accept(events: TelemetryEvent[]): TelemetryEvent[] {
    const fresh: TelemetryEvent[] = [];
    for (const e of events) {
      if (!e || typeof e.id !== 'string' || this.seen.has(e.id)) continue;
      this.seen.add(e.id);
      fresh.push(e);
      if (this.cursor.after_seq === undefined || e.seq > this.cursor.after_seq) {
        this.cursor = { after_seq: e.seq, after_id: e.id };
      }
    }
    return fresh;
  }
}
