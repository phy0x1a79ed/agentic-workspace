/**
 * Channel + agent client — the frontend's two-sided view of a live agent chat.
 *
 * Responsibilities are deliberately split (see the plan): the human side talks
 * to the *scope* (a scope IS the channel — messages only), and the live agent
 * side subscribes to the *agents* service transcript. The frontend never
 * subscribes to the scope channel; it only posts human messages there and reads
 * the human backlog. Everything the agent does (messages, partials, tool use,
 * results) streams from the agents service instead.
 *
 *   postToScope(project, scope, body)         → append a human message to the scope
 *   fetchScope(project, scope)                → backlog of human posts (one-shot)
 *   fetchAgentTranscript(scope, c?)           → connect-time act backfill (one-shot)
 *   subscribeAgent(scope, c?, cb)             → live act stream (backfill + push, deduped by id)
 *
 * Agent identity keys on the unit `scope` (the slug) ALONE — there is no
 * `project` in the agents-service wire surface; the scope-channel ops
 * (postToScope/fetchScope) keep their (project, scope) coordinate because a
 * scope IS a scopes-service repo, unchanged by the DAG decoupling.
 *
 * All of it rides `svc()` / `apiFetch`, so it speaks the same `/svc/<name>`
 * surface every other service call uses.
 *
 * Backend ops (over /svc/scopes/fn/* and /svc/agents/fn/*):
 *   scope_fetch(project, scope, kind?, limit?, order?, before_ts?)
 *   scope_post(project, scope, author, body, kind?, meta?, to_scope?)
 *   agent_subscribe(scope, after_ts?, after_id?, limit?)  → {acts, total}
 * NB: the /svc/<name>/fn/<fn> path dispatches on the *internal* op name, not
 * the MCP tool name — agent_subscribe's internal op is also `agent_subscribe`.
 *
 * The live agent stream is the agents service's `transcript` direct WS session
 * (POST /svc/agents/session/transcript {scope, after_id?, after_ts?}
 * → {ws_path}); the server replays the transcript from the cursor as a
 * `backfill` frame, then streams new acts live as `act` frames. Acts carry
 * their `agent_transcript` id (uuid) so the client de-dupes the backfill/live
 * overlap — the same dedupe key as agentcore's AgentEvent.id end-to-end.
 */

import { svc, toWsUrl } from './svc';
import { awmAs } from './auth';

/**
 * One post on a scope channel. Field names match the backend (`author`,
 * `body`, `ts`) and are structurally compatible with `@awm/tts-history`'s
 * `Post`, so fetched/streamed posts drop straight into `<TtsHistory>`.
 */
export interface ScopePost {
  id?: string;
  project?: string;
  scope?: string;
  /** 'agent:<scope>' | 'user:name' | 'system' (display form). */
  author: string;
  /** 'message' | 'journal' | 'system' | 'tool_use' | 'tool_result' | … */
  kind?: string;
  body: string;
  meta?: Record<string, unknown>;
  /** ISO timestamp. Always present on backend posts. */
  ts: string;
  to_scope?: string | string[];
  [k: string]: unknown;
}

/** True for posts authored by an agent (vs a human or the system). */
export function isAgentPost(p: { author?: string }): boolean {
  return (p.author ?? '').startsWith('agent:');
}

export interface FetchOpts {
  kind?: string;
  limit?: number;
  order?: 'asc' | 'desc';
  before_ts?: string;
}

/** Read the channel backlog. Oldest→newest by default (chat order). */
export async function fetchScope(
  project: string,
  scope: string,
  opts: FetchOpts = {},
): Promise<ScopePost[]> {
  const { posts } = await svc('scopes').fn<{ posts: ScopePost[]; total: number }>(
    'scope_fetch',
    { project, scope, order: 'asc', ...opts },
  );
  return posts ?? [];
}

export interface PostOpts {
  /** Defaults to the current operator identity (`awmAs()`, e.g. 'user:operator'). */
  author?: string;
  kind?: string;
  to_scope?: string;
  meta?: Record<string, unknown>;
}

/** Append a message to the channel. Returns the stored (normalized) post. */
export async function postToScope(
  project: string,
  scope: string,
  body: string,
  opts: PostOpts = {},
): Promise<ScopePost> {
  const { post } = await svc('scopes').fn<{ post: ScopePost }>('scope_post', {
    project,
    scope,
    author: opts.author ?? awmAs(),
    body,
    kind: opts.kind ?? 'message',
    ...(opts.to_scope ? { to_scope: opts.to_scope } : {}),
    ...(opts.meta ? { meta: opts.meta } : {}),
  });
  return post;
}

/**
 * Splice a human message into a RUNNING agent's stdin (the agents service
 * `enqueue_post` op → `enqueue_input`). Unlike `postToScope`, this does not go
 * through the scopes channel — it reaches the agent directly, which is the only
 * path that works for a task-bound PLACEMENT (a placement runs in a workspace
 * unit, not a scopes channel). The message rides the agent's next turn; the
 * reply streams back through its transcript. Returns whether it was enqueued
 * (false when no live session holds the `scope`).
 */
export async function enqueueAgentPost(
  scope: string,
  body: string,
  author?: string,
): Promise<boolean> {
  const { enqueued } = await svc('agents').fn<{ enqueued: boolean }>('enqueue_post', {
    scope,
    author: author ?? awmAs(),
    body,
  });
  return !!enqueued;
}

// ---------------------------------------------------------------------------
// Agent transcript — backfill (one-shot) + live subscribe (WS)
// ---------------------------------------------------------------------------

/**
 * A normalized act from the agents service transcript. Mirrors
 * `agent_transcript` rows / agentcore's `AgentEvent`: `id` is the uuid dedupe
 * key, `kind` classifies the act, `body` is the rendered text, `meta` carries
 * the source event, and `ts` is a unix-ms integer.
 */
export interface AgentAct {
  /** agent_transcript uuid — the dedupe key across backfill/live overlap. */
  id: string;
  /** 'message' | 'partial' | 'tool_use' | 'tool_result' | 'status' | 'result' | 'error' */
  kind: string;
  body: string;
  meta?: Record<string, unknown>;
  /** unix-ms integer. */
  ts: number;
  [k: string]: unknown;
}

/** Cursor for catch-up reads / reconnects. Omit both for full-from-start. */
export interface AgentCursor {
  /** ISO timestamp — only acts strictly after this. */
  after_ts?: string;
  /** agent_transcript uuid — tiebreak/resume point. */
  after_id?: string;
}

/**
 * One-shot connect-time backfill / catch-up read of an agent's acts. Returns
 * acts after the (after_ts, after_id) cursor, ordered by (ts, id) — the same
 * shape and cursor as the live session's opening `backfill` frame, as a plain
 * call. Useful for a non-live snapshot or to seed history before opening the
 * stream; the live `subscribeAgent` already replays from the cursor on open,
 * so a surface that subscribes immediately does not need this.
 */
export async function fetchAgentTranscript(
  scope: string,
  cursor: AgentCursor = {},
  limit?: number,
): Promise<AgentAct[]> {
  const { acts } = await svc('agents').fn<{ acts: AgentAct[]; total: number }>(
    'agent_subscribe',
    {
      scope,
      ...(cursor.after_ts ? { after_ts: cursor.after_ts } : {}),
      ...(cursor.after_id ? { after_id: cursor.after_id } : {}),
      ...(limit !== undefined ? { limit } : {}),
    },
  );
  return acts ?? [];
}

/** Frames the agents `transcript` WS session pushes (server → client). */
export type AgentStreamEvent =
  | { type: 'backfill'; acts: AgentAct[] }
  | { type: 'act'; act: AgentAct }
  | { type: 'lagged' }
  | { type: 'error'; message: string };

/** A handle to a live agent subscription. Call `close()` on unmount. */
export interface AgentSubscription {
  close(): void;
}

/**
 * Live-attach to an agent's act stream. Opens the agents service's
 * `transcript` direct session (POST /svc/agents/session/transcript, then the
 * returned WS): the first frame is `backfill` (the transcript from the cursor),
 * then `act` frames stream in as the agent works. The client is read-only —
 * it sends nothing on the socket.
 *
 * `onEvent` is invoked per parsed frame. The caller is responsible for
 * de-duping by `act.id` (the backfill/live overlap is intentional) and for
 * reconnecting on a `lagged` frame with its last seen `after_id` (the server
 * closes the socket after `lagged`).
 *
 * Allocation is async (POST to open the session, then connect the WS), so the
 * socket isn't live the instant this returns — frames arrive via the callback.
 * `close()` is safe to call before the socket finishes opening.
 */
export function subscribeAgent(
  scope: string,
  cursor: AgentCursor,
  onEvent: (ev: AgentStreamEvent) => void,
): AgentSubscription {
  let ws: WebSocket | null = null;
  let closed = false;

  void (async () => {
    try {
      const { ws_path } = await svc('agents').session<{ ws_path: string }>('transcript', {
        scope,
        ...(cursor.after_ts ? { after_ts: cursor.after_ts } : {}),
        ...(cursor.after_id ? { after_id: cursor.after_id } : {}),
      });
      if (closed) return;
      ws = new WebSocket(toWsUrl(ws_path));
      ws.addEventListener('message', (ev) => {
        if (typeof ev.data !== 'string') return;
        let parsed: AgentStreamEvent | null = null;
        try {
          parsed = JSON.parse(ev.data) as AgentStreamEvent;
        } catch {
          return;
        }
        if (parsed) onEvent(parsed);
      });
      ws.addEventListener('error', () => {
        onEvent({ type: 'error', message: 'agent transcript ws error' });
      });
    } catch (err) {
      onEvent({ type: 'error', message: (err as Error).message });
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

// ---------------------------------------------------------------------------
// Interactive tmux terminal (claude-tmux harness)
// ---------------------------------------------------------------------------

/** A handle to a live interactive terminal. */
export interface TerminalSession {
  /** Send keystrokes to the agent's pane (relayed as a binary frame). */
  send(data: string): void;
  /** Tell the server to resize the PTY/tmux client (JSON control frame). */
  resize(cols: number, rows: number): void;
  /** Detach (closes the WS; the agent's tmux session keeps running). */
  close(): void;
}

/** Callbacks for {@link openTerminal}. */
export interface TerminalHandlers {
  /** Raw terminal output bytes (a binary frame) — feed straight to xterm. */
  onData: (bytes: Uint8Array) => void;
  /** A JSON error control frame (e.g. non-tmux agent / dead session). */
  onError?: (message: string) => void;
  /** The socket closed (detach / server gone). */
  onClose?: () => void;
}

/**
 * Open the agents service's `terminal` direct session for a claude-tmux agent:
 * POST /svc/agents/session/terminal {scope, cols?, rows?}, then connect
 * the returned WS as a bidirectional byte relay of `tmux attach`.
 *
 * Wire protocol (mirrors the backend `terminal_session`): binary frames are raw
 * terminal bytes (output via `onData`, keystrokes via `send`); text frames are
 * JSON control (`resize` upstream; `error` downstream). Allocation is async, so
 * the socket isn't live the instant this returns. `close()` is safe to call
 * before it finishes opening — it detaches, never killing the agent.
 */
export function openTerminal(
  scope: string,
  opts: { cols?: number; rows?: number },
  handlers: TerminalHandlers,
): TerminalSession {
  let ws: WebSocket | null = null;
  let closed = false;
  const enc = new TextEncoder();

  void (async () => {
    try {
      const { ws_path } = await svc('agents').session<{ ws_path: string }>('terminal', {
        scope,
        ...(opts.cols ? { cols: opts.cols } : {}),
        ...(opts.rows ? { rows: opts.rows } : {}),
      });
      if (closed) return;
      ws = new WebSocket(toWsUrl(ws_path));
      ws.binaryType = 'arraybuffer';
      ws.addEventListener('message', (ev) => {
        if (typeof ev.data === 'string') {
          // Text frame = JSON control. Today that's only an error frame.
          try {
            const msg = JSON.parse(ev.data) as { type?: string; message?: string };
            if (msg?.type === 'error') handlers.onError?.(msg.message ?? 'terminal error');
          } catch {
            /* ignore non-JSON text */
          }
          return;
        }
        handlers.onData(new Uint8Array(ev.data as ArrayBuffer));
      });
      ws.addEventListener('error', () => handlers.onError?.('terminal ws error'));
      ws.addEventListener('close', () => handlers.onClose?.());
    } catch (err) {
      handlers.onError?.((err as Error).message);
    }
  })();

  return {
    send(data: string): void {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(enc.encode(data));
      } catch {
        /* ignore */
      }
    },
    resize(cols: number, rows: number): void {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      } catch {
        /* ignore */
      }
    },
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
