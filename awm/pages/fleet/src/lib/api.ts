/** Fleet data calls — notifications (roster + config) and agents (spawn/dispose). */
import { svc, saveConfigContract } from '@awm/client';
import type { FleetResponse } from './types';

const NOTIF = svc('notifications');
const AGENTS = svc('agents');

export function fetchFleet(): Promise<FleetResponse> {
  return NOTIF.fn<FleetResponse>('list_fleet');
}

/** Persist column visibility/order (partial patch merged by the contract). */
export function saveColumns(
  column_order: string[],
  hidden_columns: string[],
): Promise<unknown> {
  return saveConfigContract('notifications', { column_order, hidden_columns });
}

/** Persist last-used spawn defaults so the overlay prefills them next time. */
export function saveSpawnDefaults(defaults: Record<string, string>): Promise<unknown> {
  return saveConfigContract('notifications', { spawn_defaults: defaults });
}

/** Persist the app-level desktop-notifications gate. */
export function saveNotificationsEnabled(enabled: boolean): Promise<unknown> {
  return saveConfigContract('notifications', { notifications_enabled: enabled });
}

export interface SpawnResult {
  tmux_session: string;
  cwd: string;
  harness: string;
  model: string | null;
  effort: string | null;
  command: string;
}

export function spawnAgent(args: {
  cwd: string;
  harness: string;
  model?: string;
  effort?: string;
}): Promise<SpawnResult> {
  return AGENTS.fn<SpawnResult>('spawn', args);
}

/** Dispose an attachable session (kill its tmux). */
export function killTmux(tmux_session: string): Promise<{ ok: boolean }> {
  return AGENTS.fn<{ ok: boolean }>('kill_tmux', { tmux_session });
}

/** Resolve/forget a non-attachable session's open attention items. */
export function forgetSession(session_id: string): Promise<unknown> {
  return NOTIF.fn('resolve', { session_id });
}

export function markSeen(id: string): Promise<unknown> {
  return NOTIF.fn('mark_seen', { id }).catch(() => undefined);
}
