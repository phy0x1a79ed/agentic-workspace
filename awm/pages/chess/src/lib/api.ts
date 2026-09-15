// Client for the rlm-chess realm: verbs over /svc/rlm-chess/fn, events over the
// `chess` emitter, and the view state kept in the URL query.

import { svc, toWsUrl } from '@awm/client';

const realm = svc('rlm-chess');

export type Color = 'white' | 'black';

export interface Awaiting {
  slot: number;
  color: Color;
  seat: string;
  external: boolean;
  game_id: string;
  ply: number;
  since: number;
}

export interface ChessEvent {
  session_id: string;
  kind: string;
  seq: number | null;
  data: any;
}

const CODES = [
  'busy', 'not_found', 'invalid_params', 'exists', 'no_game', 'already_initialized',
  'bot_failed', 'unknown_method', 'internal', 'not_your_turn', 'stale', 'not_running',
  'start_failed',
];
const CODE_RE = new RegExp(`\\b(${CODES.join('|')}): ([\\s\\S]*)$`);

export class RealmError extends Error {
  constructor(public code: string, message: string) {
    super(message);
  }
}

export async function call<T = any>(verb: string, args: Record<string, unknown> = {}): Promise<T> {
  try {
    return await realm.fn<T>(verb, args);
  } catch (err: any) {
    let text = String(err?.message ?? err);
    try {
      const body = JSON.parse(err?.body ?? '');
      if (body?.error) text = String(body.error);
    } catch { /* not a JSON error body */ }
    const match = CODE_RE.exec(text);
    throw new RealmError(match ? match[1] : 'error', match ? match[2] : text);
  }
}

/** Follow the `chess` emitter until the returned function is called. Reconnects on drop. */
export function subscribe(onEvent: (e: ChessEvent) => void,
                          onStatus?: (live: boolean) => void): () => void {
  let closed = false;
  let sock: WebSocket | null = null;
  const open = () => {
    const s = new WebSocket(toWsUrl(realm.url('/emit/chess')));
    sock = s;
    s.onopen = () => onStatus?.(true);
    s.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      try {
        const e = JSON.parse(ev.data);
        if (e && typeof e.kind === 'string') onEvent(e);
      } catch { /* ignore a malformed frame */ }
    };
    s.onclose = () => {
      onStatus?.(false);
      if (!closed) setTimeout(open, 1000);
    };
    s.onerror = () => s.close();
  };
  open();
  return () => {
    closed = true;
    sock?.close();
  };
}

export type View =
  | { screen: 'lobby' }
  | { screen: 'live'; sid: string }
  | { screen: 'replay'; save: string | null; session: string | null; game: string | null };

export function readView(): View {
  const q = new URLSearchParams(window.location.search);
  const sid = q.get('sid');
  if (sid) return { screen: 'live', sid };
  const replay = q.get('replay');
  if (replay) {
    const session = replay.startsWith('session:') ? replay.slice('session:'.length) : null;
    return { screen: 'replay', save: session ? null : replay, session, game: q.get('game') };
  }
  return { screen: 'lobby' };
}

export function go(params: Record<string, string | null | undefined> = {}): void {
  const url = new URL(window.location.href);
  url.search = '';
  for (const [k, v] of Object.entries(params)) if (v) url.searchParams.set(k, v);
  window.history.pushState(null, '', url);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export const REASONS: Record<string, string> = {
  checkmate: 'checkmate',
  stalemate: 'stalemate',
  insufficient_material: 'insufficient material',
  seventyfive_moves: '75-move rule',
  fivefold_repetition: 'fivefold repetition',
  max_plies: 'move cap reached',
  resignation: 'resignation',
  illegal_move: 'illegal-move forfeit',
  timeout: 'timeout',
  disconnect: 'bot disconnected',
  protocol_error: 'bot protocol error',
  stopped: 'session stopped',
};

export const isExternal = (name: string | null | undefined) => !!name && name.startsWith('@');

export function randomColor(): Color {
  return Math.random() < 0.5 ? 'white' : 'black';
}
