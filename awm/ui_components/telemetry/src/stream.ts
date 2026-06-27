/**
 * @awm/telemetry — the client half of the reusable telemetry substrate.
 *
 * A telemetry *stream* is an ordered, idempotent sequence of events a service
 * publishes (see the Python `awm.telemetry` component). A third-party client
 * syncs it two ways, both carrying `(seq, id)` so they dedupe:
 *
 *   pollStream(svc, op, stream, cursor)        → one-shot catch-up read (events after cursor)
 *   subscribeStream(svc, kind, init, onFrame)  → live WS frames (backfill + push), raw
 *   syncStream({...})                          → managed: dedup by id + advance cursor + reconnect
 *
 * The cursor is `{ after_seq, after_id }` — `after_seq` is the monotonic order
 * key the server filters on; `after_id` is the idempotency tiebreak the client
 * dedupes on. `StreamTracker` is the pure idempotency core (no I/O), so the
 * backfill/live overlap and a lagged reconnect never surface a duplicate.
 *
 * Rides `svc()` / `toWsUrl()` from `@awm/client`, so it speaks the same
 * `/svc/<name>/fn/<op>` + `/svc/<name>/session/<kind>` surface as every other
 * service call.
 */

import { svc, toWsUrl } from '@awm/client';

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
 * Direct poll sync: events on `stream` after `cursor`, via the service's poll op
 * (`POST /svc/<svcName>/fn/<op>` → `{ events }`). The op name is the service's —
 * a service that adopts telemetry exposes one returning
 * `{ events: tel.read_after(stream, after_seq) }`.
 */
export async function pollStream(
  svcName: string,
  op: string,
  stream: string,
  cursor: StreamCursor = {},
  limit?: number,
): Promise<TelemetryEvent[]> {
  const { events } = await svc(svcName).fn<{ events: TelemetryEvent[] }>(op, {
    ...cursorArgs(stream, cursor),
    ...(limit !== undefined ? { limit } : {}),
  });
  return events ?? [];
}

/**
 * Live-subscribe to a stream's raw frames. Opens the service's direct WS
 * session (`POST /svc/<svcName>/session/<sessionKind>` → `{ ws_path }`, then the
 * WS): the first frame is `backfill` (from the init cursor), then `event` frames
 * stream live. Read-only — sends nothing on the socket.
 *
 * `onFrame` gets each parsed frame verbatim; the caller dedupes by `event.id`
 * and reconnects on `lagged`. For dedup + reconnect handled for you, use
 * `syncStream`.
 */
export function subscribeStream(
  svcName: string,
  sessionKind: string,
  init: { stream: string } & StreamCursor,
  onFrame: (frame: StreamFrame) => void,
): StreamSubscription {
  let ws: WebSocket | null = null;
  let closed = false;

  void (async () => {
    try {
      const { ws_path } = await svc(svcName).session<{ ws_path: string }>(
        sessionKind,
        cursorArgs(init.stream, init),
      );
      if (closed) return;
      ws = new WebSocket(toWsUrl(ws_path));
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
    } catch (err) {
      onFrame({ type: 'error', message: (err as Error).message });
    }
  })();

  return {
    close(): void {
      if (closed) return;
      closed = true;
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    },
  };
}

export interface SyncOptions {
  /** Service name (the `/svc/<name>` prefix). */
  svcName: string;
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
      opts.svcName,
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
