/**
 * Room bootstrap + WS attach against awm's /rooms surface.
 *
 * Find-or-create a room for a {project, scope} pair, post text into it, and
 * stream incoming posts back through a callback. Lifted out of the agent page
 * so any page (chat, agent, …) shares one room client instead of copy-pasting
 * the find-or-create dance.
 */

import { apiFetch } from './auth';
import { toWsUrl } from './svc';

/**
 * One room transcript post. Mirrors awm's /rooms post shape and is
 * structurally compatible with `@awm/tts-history`'s `Post`, so the same
 * objects can be handed straight to `<TtsHistory>` without conversion.
 */
export interface RoomPost {
  id?: string;
  ts: string;
  /** Author identifier — e.g. `user:operator`, `agent:project/scope`. */
  author: string;
  /** Recipient — single scope or list. Backend may omit on broadcast. */
  to_scope?: string | string[];
  body: string;
  kind?: string;
  [k: string]: unknown;
}

interface RoomInfo {
  id: string;
  topic?: string | null;
  status: string;
  [k: string]: unknown;
}

interface RoomListResponse { rooms: RoomInfo[]; total: number }

/**
 * Find the most recent active room with `scopeIdent` (e.g. "awm/agent-harness")
 * as a participant; if none exists, create one. Idempotently re-invites the
 * scope so an agent session that died after a hub restart wakes back up.
 */
export async function ensureRoom(scopeIdent: string): Promise<string> {
  const listed = await apiFetch<RoomListResponse>(
    `/rooms/search?status=active&participating_scope=${encodeURIComponent(scopeIdent)}`,
  );
  if (listed.rooms.length > 0) {
    const roomId = listed.rooms[listed.rooms.length - 1].id;
    try {
      await apiFetch(`/rooms/${encodeURIComponent(roomId)}/invite`, {
        method: 'POST',
        body: { scope: scopeIdent },
      });
    } catch { /* already-present or already-running — both fine */ }
    return roomId;
  }
  const created = await apiFetch<RoomInfo>('/rooms', {
    method: 'POST',
    body: { scopes: [scopeIdent], close_on_exit: false },
  });
  return created.id;
}

/** Post a text message into a room. `to` optionally narrows to one scope. */
export async function postText(roomId: string, body: string, to?: string): Promise<void> {
  const payload: Record<string, unknown> = { body, kind: 'text' };
  if (to) payload.to_scope = to;
  await apiFetch(`/rooms/${encodeURIComponent(roomId)}/posts`, {
    method: 'POST',
    body: payload,
  });
}

export type RoomEvent =
  | { type: 'history'; posts: RoomPost[] }
  | { type: 'post'; post: RoomPost }
  | { type: 'participant_joined'; participant: Record<string, unknown> }
  | { type: 'participant_left'; participant: Record<string, unknown> }
  | { type: 'room_closed'; ts: string }
  | { type: 'lagged' }
  | { type: 'error'; message: string }
  | { type: 'pong' };

/**
 * Open the per-room WS at /rooms/{id}/attach. Initial frame is `history` with
 * the backlog; `post` frames stream in live. Call `close()` on unmount.
 */
export class RoomAttach {
  private ws: WebSocket;
  private closed = false;

  constructor(roomId: string, onEvent: (ev: RoomEvent) => void) {
    this.ws = new WebSocket(toWsUrl(`/rooms/${encodeURIComponent(roomId)}/attach`));
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
