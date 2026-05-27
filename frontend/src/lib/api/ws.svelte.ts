/**
 * Room WebSocket attachment + event stream. Ported from awm/static/index.html
 * lines 1313–1352 plus the focus-tab variant at 1693+. Single class so both
 * routes (focus + room) share the same connect/reconnect/event-fanout logic.
 *
 * Auth is cookie-based — no subprotocol bearer header. The path is
 *   wss://<host>/rooms/<room_id>/attach
 *
 * Reconnect uses the same exponential backoff (1s → 15s cap) as legacy.
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

export class RoomWs {
  state = $state<WsState>('idle');
  banner = $state<string>('');

  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private backoff = 1000;
  private currentRoom: string | null = null;
  private wantConnected = false;

  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  connect(roomId: string): void {
    if (this.currentRoom === roomId && this.socket && this.socket.readyState === WebSocket.OPEN) return;
    this.close();
    this.currentRoom = roomId;
    this.wantConnected = true;
    this._open(roomId);
  }

  close(): void {
    this.wantConnected = false;
    if (this.socket) {
      try { this.socket.close(); } catch { /* ignore */ }
      this.socket = null;
    }
    this.state = 'idle';
    this.currentRoom = null;
  }

  private _open(roomId: string): void {
    this.state = 'connecting';
    const url = wsUrl(`/rooms/${encodeURIComponent(roomId)}/attach`);
    const sock = new WebSocket(url);
    this.socket = sock;

    sock.onopen = () => {
      this.state = 'open';
      this.banner = `attached to ${roomId}`;
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
      if (this.wantConnected && this.currentRoom === roomId) {
        const delay = this.backoff;
        this.backoff = Math.min(this.backoff * 2, 15000);
        setTimeout(() => {
          if (this.wantConnected && this.currentRoom === roomId) this._open(roomId);
        }, delay);
      }
    };
    sock.onerror = () => {
      this.state = 'error';
      this.banner = 'ws error';
    };
  }
}
