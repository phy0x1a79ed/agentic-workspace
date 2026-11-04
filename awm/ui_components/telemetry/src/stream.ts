/**
 * @awm/telemetry — the client half of the reusable telemetry substrate.
 *
 * A telemetry *stream* is an ordered, idempotent sequence of events a service
 * publishes (see the Python `awm.telemetry` component). A third-party client
 * syncs it two ways, both carrying `(seq, id)` so they dedupe:
 *
 *   pollStream(tool, verb, stream, cursor)         → one-shot catch-up read (events after cursor)
 *   subscribeStream(service, kind, init, onFrame)  → live WS frames (backfill + push), raw
 *   syncStream({...})                          → managed: dedup by id + advance cursor + reconnect
 *
 * The cursor is `{ after_seq, after_id }` — `after_seq` is the monotonic order
 * key the server filters on; `after_id` is the idempotency tiebreak the client
 * dedupes on. `StreamTracker` is the pure idempotency core (no I/O), so the
 * backfill/live overlap and a lagged reconnect never surface a duplicate.
 *
 * Rides `tool()` / `openSession()` from `@awm/client`: a verb over
 * `POST /invoke` and a direct session at `/hub/session/<service>/<kind>`.
 */

import { openSession, tool } from '@awm/client';

import { StreamTracker, type StreamCursor, type TelemetryEvent } from './tracker';

export { StreamTracker, type StreamCursor, type TelemetryEvent };

/** Frames the telemetry WS session pushes (server → client). */
export type StreamFrame =
  | { type: 'backfill'; events: TelemetryEvent[] }
  | { type: 'event'; event: TelemetryEvent }
  | { type: 'lagged' }
  | { type: 'error'; message: string };

/** A handle to a live subscription / sync. Call `close()` on unmount. */
export interface StreamSubscription {
  close(): void;
}

/** Build the `{stream, after_seq?, after_id?}` init/args from a cursor. */
function cursorArgs(stream: string, cursor: StreamCursor): Record<string, unknown> {
  return {
    stream,
    ...(cursor.after_seq !== undefined ? { after_seq: cursor.after_seq } : {}),
    ...(cursor.after_id !== undefined ? { after_id: cursor.after_id } : {}),
  };
}

/**
 * Direct poll sync: events on `stream` after `cursor`, via the tool's poll verb
 * (→ `{ events }`). The verb is the service's — a service that adopts
 * telemetry exposes one returning `{ events: tel.read_after(stream, after_seq) }`.
 */
export async function pollStream(
  toolName: string,
  verb: string,
  stream: string,
  cursor: StreamCursor = {},
  limit?: number,
): Promise<TelemetryEvent[]> {
  const { events } = await tool(toolName).call<{ events: TelemetryEvent[] }>(verb, {
    ...cursorArgs(stream, cursor),
    ...(limit !== undefined ? { limit } : {}),
  });
  return events ?? [];
}

/**
 * Live-subscribe to a stream's raw frames. Opens the service's direct session
 * with the cursor as its init: the first frame back is `backfill` (from the
 * init cursor), then `event` frames stream live. Sends nothing after the init.
 *
 * `onFrame` gets each parsed frame verbatim; the caller dedupes by `event.id`
 * and reconnects on `lagged`. For dedup + reconnect handled for you, use
 * `syncStream`.
 */
export function subscribeStream(
  service: string,
  sessionKind: string,
  init: { stream: string } & StreamCursor,
  onFrame: (frame: StreamFrame) => void,
): StreamSubscription {
  let closed = false;
  const ws = openSession(service, sessionKind, cursorArgs(init.stream, init));
  ws.addEventListener('message', (ev) => {
    if (typeof ev.data !== 'string') return;
    let parsed: StreamFrame | null = null;
    try {
      parsed = JSON.parse(ev.data) as StreamFrame;
    } catch {
      return;
    }
    if (parsed) onFrame(parsed);
  });
  ws.addEventListener('error', () => {
    onFrame({ type: 'error', message: 'telemetry ws error' });
  });
  ws.addEventListener('close', (ev) => {
    if (!closed && ev.code === 1011) onFrame({ type: 'error', message: ev.reason || 'session refused' });
  });

  return {
    close(): void {
      if (closed) return;
      closed = true;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    },
  };
}

export interface SyncOptions {
  /** The service that serves the live session. */
  service: string;
  /** The stream key to sync. */
  stream: string;
  /** Direct session kind for the live WS (default `'telemetry'`). */
  sessionKind?: string;
  /** Where to resume from (default: full-from-start). */
  initial?: StreamCursor;
  /** Called with each batch of NEW (deduped, in-order) events. */
  onEvents: (events: TelemetryEvent[]) => void;
  /** Optional error sink (transport errors; the sync keeps trying). */
  onError?: (message: string) => void;
}

/**
 * Managed live sync: subscribe → dedup by id → advance cursor → reconnect on
 * `lagged` from the last cursor (the backfill replays the gap). The caller only
 * ever sees fresh events, exactly once, in order — the idempotency the substrate
 * promises, handled end-to-end. `close()` stops it for good.
 */
export function syncStream(opts: SyncOptions): StreamSubscription {
  const sessionKind = opts.sessionKind ?? 'telemetry';
  const tracker = new StreamTracker(opts.initial);
  let sub: StreamSubscription | null = null;
  let closed = false;

  const connect = (): void => {
    if (closed) return;
    sub = subscribeStream(
      opts.service,
      sessionKind,
      { stream: opts.stream, ...tracker.cursor },
      (frame) => {
        if (closed) return;
        if (frame.type === 'backfill') {
          const fresh = tracker.accept(frame.events);
          if (fresh.length) opts.onEvents(fresh);
        } else if (frame.type === 'event') {
          const fresh = tracker.accept([frame.event]);
          if (fresh.length) opts.onEvents(fresh);
        } else if (frame.type === 'lagged') {
          // The server closed after lagging us; reconnect from our cursor and
          // the backfill replays whatever we missed (deduped on the way in).
          try {
            sub?.close();
          } catch {
            /* ignore */
          }
          connect();
        } else if (frame.type === 'error') {
          opts.onError?.(frame.message);
        }
      },
    );
  };

  connect();

  return {
    close(): void {
      if (closed) return;
      closed = true;
      try {
        sub?.close();
      } catch {
        /* ignore */
      }
    },
  };
}
