/**
 * Shared UI state — mobile sheet open-state, panel widths, WS connection
 * indicator. Runes-based so any component can `import { ui }` and react.
 */

export type WsKind = 'off' | 'on' | 'err';

class UiState {
  leftSheetOpen   = $state(false);
  rightSheetOpen  = $state(false);
  slashOpen       = $state(false);
  leaderBadge     = $state<string>('STANDBY');
  leaderActive    = $state(false);
  wsKind          = $state<WsKind>('off');
  // Scope identifier (e.g. "_vagrant/user-user-dev") of the user's own
  // vagrant manager. Returned by POST /vagrant/session; used to mark which
  // agent row in the details panel is "yours" and to target slash commands.
  managerScope    = $state<string | null>(null);

  openLeft()  { this.leftSheetOpen  = true;  this.rightSheetOpen = false; }
  openRight() { this.rightSheetOpen = true;  this.leftSheetOpen  = false; }
  closeAll()  { this.leftSheetOpen  = false; this.rightSheetOpen = false; this.slashOpen = false; }
}

export const ui = new UiState();
