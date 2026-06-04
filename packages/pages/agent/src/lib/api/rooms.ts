/**
 * Room bootstrap + WS attach for the agent convo page.
 *
 * Find-or-create a room for a {project, scope} pair, post text into it,
 * and stream incoming posts back through a callback.
 *
 * `awmAs()` reads the same awm_as cookie the hub stamps on bootstrap —
 * its value (e.g. "operator") is sent as `X-Awm-As: user:operator` so
 * the backend attributes posts to the right operator identity.
 */

import type { Post } from '@awm/tts-history';

export type { Post };

let _awmAsCache = '';

export function awmAs(): string {
  if (_awmAsCache) return _awmAsCache;
  const m = document.cookie.match(/(?:^|; )awm_as=([^;]*)/);
  _awmAsCache = 'user:' + (m ? decodeURIComponent(m[1]) : 'operator');
  return _awmAsCache;
}

async function _fetch<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers: { 'X-Awm-As': awmAs() },
  };
  if (body !== undefined) {
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  const text = await r.text();
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status}: ${text.slice(0, 240)}`);
  return (text ? JSON.parse(text) : null) as T;
}

interface RoomInfo {
  id: string;
  topic?: string | null;
  status: string;
  [k: string]: unknown;
}

interface RoomListResponse { rooms: RoomInfo[]; total: number }

/**
 * Find the most recent active room with `scopeIdent` (e.g. "awm/infra-service-hub")
 * as a participant; if none exists, create one. Idempotently re-invites the
 * scope so an agent session that died after a hub restart wakes back up.
 */
export async function ensureRoom(scopeIdent: string): Promise<string> {
  const listed = await _fetch<RoomListResponse>(
    'GET',
    `/rooms?status=active&participating_scope=${encodeURIComponent(scopeIdent)}`,
  );
  if (listed.rooms.length > 0) {
    const roomId = listed.rooms[listed.rooms.length - 1].id;
    try {
      await _fetch('POST', `/rooms/${encodeURIComponent(roomId)}/invite`, { scope: scopeIdent });
    } catch { /* already-present or already-running — both fine */ }
    return roomId;
  }
  const created = await _fetch<RoomInfo>('POST', '/rooms', {
    scopes: [scopeIdent],
    close_on_exit: false,
  });
  return created.id;
}

/** Post a text message into a room. `to` optionally narrows to one scope. */
export async function postText(roomId: string, body: string, to?: string): Promise<void> {
  const payload: Record<string, unknown> = { body, kind: 'text' };
  if (to) payload.to_scope = to;
  await _fetch('POST', `/rooms/${encodeURIComponent(roomId)}/posts`, payload);
}

export type RoomEvent =
  | { type: 'history'; posts: Post[] }
  | { type: 'post'; post: Post }
  | { type: 'participant_joined'; participant: Record<string, unknown> }
  | { type: 'participant_left'; participant: Record<string, unknown> }
  | { type: 'room_closed'; ts: string }
  | { type: 'lagged' }
  | { type: 'error'; message: string }
  | { type: 'pong' };

/**
 * Open the per-room WS at /rooms/{id}/attach. Initial frame is `history`
 * with the backlog; `post` frames stream in live. Close on unmount.
 */
export class RoomAttach {
  private ws: WebSocket;
  private closed = false;

  constructor(roomId: string, onEvent: (ev: RoomEvent) => void) {
    const url = new URL(`/rooms/${encodeURIComponent(roomId)}/attach`, window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(url.toString());
    this.ws.addEventListener('message', (ev) => {
      if (typeof ev.data !== 'string') return;
      let parsed: RoomEvent | null = null;
      try { parsed = JSON.parse(ev.data) as RoomEvent; } catch { return; }
      onEvent(parsed);
    });
    this.ws.addEventListener('close', () => { this.closed = true; });
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    try { this.ws.close(); } catch { /* ignore */ }
  }
}
