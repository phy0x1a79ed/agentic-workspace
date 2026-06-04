// Singleton pub/sub bus. One instance per JS realm (browser tab); all
// imports of `bus` resolve to the same module record. Channels are
// plain strings; payloads are unknown — consumers narrow with a type
// guard at the subscribe site. No wildcards, no priorities, no
// middleware: keep this tiny and predictable.

type Handler = (payload: unknown) => void;

interface ChannelState {
  handlers: Set<Handler>;
  // Most-recently-published payload, kept iff anyone has subscribed
  // with replayLast: true on this channel since last publish — cheap
  // bounded memory (one slot per channel).
  last: { value: unknown } | null;
}

const channels = new Map<string, ChannelState>();

function getOrCreate(channel: string): ChannelState {
  let state = channels.get(channel);
  if (state === undefined) {
    state = { handlers: new Set(), last: null };
    channels.set(channel, state);
  }
  return state;
}

export interface PublishOptions {
  /** Deliver synchronously inline. Default false → queueMicrotask, so the
   *  publisher's stack unwinds before handlers run (avoids re-entrancy
   *  surprises when a handler publishes back). */
  sync?: boolean;
}

export interface SubscribeOptions {
  /** If a payload has been published on this channel since process
   *  start, replay it to the new subscriber on the next microtask.
   *  Useful for late-mounting UI that should reflect current state. */
  replayLast?: boolean;
}

function deliver(state: ChannelState, payload: unknown, sync: boolean): void {
  const handlers = Array.from(state.handlers);
  if (sync) {
    for (const h of handlers) {
      try { h(payload); } catch (err) { reportError(err); }
    }
  } else {
    queueMicrotask(() => {
      for (const h of handlers) {
        try { h(payload); } catch (err) { reportError(err); }
      }
    });
  }
}

function reportError(err: unknown): void {
  // Don't let a single faulty handler take down the bus or the
  // microtask. Log and move on; the publisher already returned.
  if (typeof console !== "undefined" && console.error) {
    console.error("[bus] handler threw:", err);
  }
}

export const bus = {
  publish(channel: string, payload: unknown, opts: PublishOptions = {}): void {
    const state = getOrCreate(channel);
    state.last = { value: payload };
    deliver(state, payload, opts.sync === true);
  },
  subscribe(channel: string, handler: Handler, opts: SubscribeOptions = {}): () => void {
    const state = getOrCreate(channel);
    state.handlers.add(handler);
    if (opts.replayLast === true && state.last !== null) {
      const snapshot = state.last.value;
      queueMicrotask(() => {
        // Re-check membership: caller may have unsubscribed before the
        // microtask drains. Don't deliver to an already-detached handler.
        if (state.handlers.has(handler)) {
          try { handler(snapshot); } catch (err) { reportError(err); }
        }
      });
    }
    return () => { state.handlers.delete(handler); };
  },
  /** Test-only: clear all channels + cached last-payloads. Production
   *  code should never call this. */
  reset(): void {
    channels.clear();
  },
};

export type Bus = typeof bus;


// ---------------------------------------------------------------------------
// Operator identity
//
// Stripes get caller identity for free: the hub injects X-Awm-As on every
// proxied request from the awm_as cookie, so backends already know who's
// calling. Frontends still need to *display* the operator (and don't have
// cookie access since awm_as is HttpOnly), so we expose a cached fetch
// to /auth/whoami here. One round-trip per page load, regardless of how
// many call sites ask. Lives alongside the bus because every stripe
// already imports from @awm/bus — one well-known module for cross-cutting
// concerns beats a separate @awm/identity package.
// ---------------------------------------------------------------------------

let operatorCache: Promise<string> | null = null;

export function getOperator(): Promise<string> {
  if (operatorCache !== null) return operatorCache;
  operatorCache = fetch("/auth/whoami", { credentials: "include" })
    .then((r) => r.ok ? r.json() : { awm_user: "operator" })
    .then((j) => (j && typeof j.awm_user === "string") ? j.awm_user : "operator")
    .catch(() => "operator");
  return operatorCache;
}

/** Test-only: drop the cached whoami fetch. */
export function _resetOperatorCache(): void {
  operatorCache = null;
}
