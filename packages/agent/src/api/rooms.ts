/**
 * Agent-stripe room bootstrap + WS attach. Reuses @awm/chat-primitives's
 * fetch helpers for posts and slash; adds room create/find + WS open.
 */

import { awmAs, postText, runAgentSlash } from '@awm/chat-primitives';
import type { Post } from '@awm/chat-primitives';

export { awmAs, postText, runAgentSlash };
export type { Post };

interface RoomInfo {
  id: string;
  topic?: string | null;
  status: string;
  [k: string]: unknown;
}

interface RoomListResponse {
  rooms: RoomInfo[];
  total: number;
}

interface CreateRoomResponse extends RoomInfo {}

interface LiveAgent {
  model?: string | null;
  permission_mode?: string | null;
  status?: string | null;
  [k: string]: unknown;
}

interface RoomAgent {
  scope: string;
  identifier: string;
  live?: LiveAgent | null;
}

interface RoomAgentsResponse {
  agents: RoomAgent[];
}

/** Return the LiveAgent for the named scope in the room, or null if none. */
export async function getRoomAgent(roomId: string, scopeIdent: string): Promise<LiveAgent | null> {
  const r = await _fetch<RoomAgentsResponse>('GET', `/rooms/${encodeURIComponent(roomId)}/agents`);
  for (const a of r.agents ?? []) {
    if (a.identifier === scopeIdent || a.scope === scopeIdent) return a.live ?? null;
  }
  return null;
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

/**
 * Find the most recent active room that has ``scopeIdent`` as a
 * participant; if none, create one with that scope as the only seed.
 * For existing rooms, also re-invite the scope so a dead agent session
 * (e.g. after an awm-exposed restart that didn't cleanly resume) wakes
 * back up — invite is idempotent and triggers ``_ensure_local_session``.
 * scopeIdent is the awm "project/scope" form, e.g. "awm/test-awm-agent".
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
    } catch {
      // Already-present participant or already-running session — both fine.
    }
    return roomId;
  }
  const created = await _fetch<CreateRoomResponse>('POST', '/rooms', {
    scopes: [scopeIdent],
    close_on_exit: false,
  });
  return created.id;
}

/** Outbound WS frame shapes. */
type Outbound =
  | { type: 'ping' }
  | { type: 'post'; body: string; kind?: string; to?: string };

/** Inbound WS frame shapes, narrowed to the ones the agent stripe cares about. */
export type RoomEvent =
  | { type: 'history'; posts: Post[] }
  | { type: 'post'; post: Post }
  | { type: 'participant_joined'; participant: Record<string, unknown> }
  | { type: 'participant_left'; participant: Record<string, unknown> }
  | { type: 'room_closed'; ts: string }
  | { type: 'upstream_disconnected'; peer: string; reason: string }
  | { type: 'lagged' }
  | { type: 'error'; message: string }
  | { type: 'pong' };

export class RoomAttach {
  private ws: WebSocket;
  private closed = false;
  private onEvent: (ev: RoomEvent) => void;

  constructor(roomId: string, onEvent: (ev: RoomEvent) => void) {
    this.onEvent = onEvent;
    const url = new URL(`/rooms/${encodeURIComponent(roomId)}/attach`, window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(url.toString());
    this.ws.addEventListener('message', (ev) => {
      if (typeof ev.data !== 'string') return;
      let parsed: RoomEvent | null = null;
      try { parsed = JSON.parse(ev.data) as RoomEvent; } catch { return; }
      this.onEvent(parsed);
    });
    this.ws.addEventListener('close', () => { this.closed = true; });
    this.ws.addEventListener('error', () => { /* surfaced via close */ });
  }

  send(msg: Outbound): void {
    if (this.closed) return;
    this.ws.send(JSON.stringify(msg));
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    try { this.ws.close(); } catch { /* ignore */ }
  }
}
