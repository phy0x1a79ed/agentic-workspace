/**
 * Shared UI state — mobile sheet open-state, panel widths, WS connection
 * indicator. Runes-based so any component can `import { ui }` and react.
 */

import { coreStatus, ApiError } from '$lib/api/client';

export type WsKind = 'off' | 'on' | 'err';

class UiState {
  leftSheetOpen   = $state(false);
  rightSheetOpen  = $state(false);
  slashOpen       = $state(false);
  leaderBadge     = $state<string>('STANDBY');
  leaderActive    = $state(false);
  wsKind          = $state<WsKind>('off');
  // Round-trip time of the most recent /status probe in ms (null until first
  // sample). LiveIndicator renders this directly.
  pingMs          = $state<number | null>(null);
  // Scope identifier (e.g. "_vagrant/user-user-dev") of the user's own
  // vagrant manager. Returned by POST /vagrant/session; used to mark which
  // agent row in the details panel is "yours" and to target slash commands.
  managerScope    = $state<string | null>(null);

  // Adaptive ping loop. Cadence = max(2 * lastPing, 1000ms) so slow links
  // get more breathing room; errors back off to 2s.
  private _pingTimer: ReturnType<typeof setTimeout> | null = null;
  private _pingStopped = true;

  openLeft()  { this.leftSheetOpen  = true;  this.rightSheetOpen = false; }
  openRight() { this.rightSheetOpen = true;  this.leftSheetOpen  = false; }
  closeAll()  { this.leftSheetOpen  = false; this.rightSheetOpen = false; this.slashOpen = false; }

  startPing() {
    if (!this._pingStopped) return;
    this._pingStopped = false;
    void this._pingTick();
  }

  stopPing() {
    this._pingStopped = true;
    if (this._pingTimer) { clearTimeout(this._pingTimer); this._pingTimer = null; }
  }

  private async _pingTick() {
    if (this._pingStopped) return;
    const t0 = performance.now();
    let nextDelay = 1000;
    try {
      const r = await coreStatus();
      const rtt = Math.round(performance.now() - t0);
      this.pingMs = rtt;
      this.wsKind = 'on';
      if (r.leadership_state === 'ACTIVE') {
        this.leaderBadge = 'ACTIVE';
        this.leaderActive = true;
      } else {
        this.leaderBadge = `STANDBY → ${r.current_leader || '?'}`;
        this.leaderActive = false;
      }
      nextDelay = Math.max(2 * rtt, 1000);
    } catch (e) {
      this.pingMs = null;
      this.wsKind = e instanceof ApiError && e.status === 401 ? 'off' : 'err';
      nextDelay = 2000;
    }
    if (this._pingStopped) return;
    this._pingTimer = setTimeout(() => this._pingTick(), nextDelay);
  }
}

export const ui = new UiState();
