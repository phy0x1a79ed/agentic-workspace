/**
 * Room WebSocket transport.
 *
 * Auth is cookie-based — no subprotocol bearer header. The path is
 *   wss://<host>/rooms/<room_id>/attach
 *
 * The WS is *pure UI transport*: opening one does NOT make the user a
 * participant of the room (server-side fix in awm/services/rooms.py removed
 * the subscribe-as-participant behaviour). Switching the focused room
 * should NOT tear down and reopen sockets — it's only a view change. So
 * the page holds a `RoomWsPool` that owns one socket per room currently
 * visible in the left rail. `attach(roomId)` is idempotent; `detach`
 * closes a single room's socket; `closeAll` runs on page teardown.
 *
 * Each socket carries its own backoff (1s → 15s cap) and event listeners.
 */

import { wsUrl } from './config';

export type RoomEvent =
  | { type: 'history'; posts: import('./client').Post[] }
  | { type: 'post'; post: import('./client').Post }
  | { type: 'participant_joined'; [k: string]: unknown }
  | { type: 'participant_left';   [k: string]: unknown }
  | { type: string; [k: string]: unknown };

export type WsState = 'idle' | 'connecting' | 'open' | 'closed' | 'error';
export type Listener = (ev: RoomEvent) => void;

class RoomSocket {
  state = $state<WsState>('idle');
  banner = $state<string>('');

  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private backoff = 1000;
  private wantConnected = true;

  constructor(private readonly roomId: string) {
    this._open();
  }

  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  close(): void {
    this.wantConnected = false;
    if (this.socket) {
      try { this.socket.close(); } catch { /* ignore */ }
      this.socket = null;
    }
    this.listeners.clear();
    this.state = 'idle';
  }

  private _open(): void {
    this.state = 'connecting';
    const url = wsUrl(`/rooms/${encodeURIComponent(this.roomId)}/attach`);
    const sock = new WebSocket(url);
    this.socket = sock;

    sock.onopen = () => {
      this.state = 'open';
      this.banner = `attached to ${this.roomId}`;
      this.backoff = 1000;
    };
    sock.onmessage = (e) => {
      let ev: RoomEvent;
      try { ev = JSON.parse(e.data); } catch { return; }
      for (const fn of this.listeners) {
        try { fn(ev); } catch (err) { console.error('[ws listener]', err); }
      }
    };
    sock.onclose = (ev) => {
      this.state = 'closed';
      this.banner = `disconnected (${ev.code})${this.wantConnected ? '; reconnecting…' : ''}`;
      if (this.wantConnected) {
        const delay = this.backoff;
        this.backoff = Math.min(this.backoff * 2, 15000);
        setTimeout(() => { if (this.wantConnected) this._open(); }, delay);
      }
    };
    sock.onerror = () => {
      this.state = 'error';
      this.banner = 'ws error';
    };
  }
}

export class RoomWsPool {
  private sockets = new Map<string, RoomSocket>();

  attach(roomId: string): void {
    if (this.sockets.has(roomId)) return;
    this.sockets.set(roomId, new RoomSocket(roomId));
  }

  detach(roomId: string): void {
    const s = this.sockets.get(roomId);
    if (!s) return;
    s.close();
    this.sockets.delete(roomId);
  }

  on(roomId: string, fn: Listener): () => void {
    this.attach(roomId);
    return this.sockets.get(roomId)!.on(fn);
  }

  bannerOf(roomId: string): string {
    return this.sockets.get(roomId)?.banner ?? '';
  }

  closeAll(): void {
    for (const s of this.sockets.values()) s.close();
    this.sockets.clear();
  }
}
