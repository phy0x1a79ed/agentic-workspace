/** Fleet data calls — all served by the agents service (sole FleetView backend).
 *
 * The agents service absorbed the observe plane (roster + accounting + feed +
 * attention) from the retired notifications service, so roster reads, config,
 * attention acks, spawn (adhoc + scope), and dispose all resolve under
 * `/svc/agents`. Fleet config is a dedicated verb pair (`get_fleet_config` /
 * `save_fleet_config`) rather than the shared /config contract slot, which the
 * agents driver contract already owns.
 */
import { svc } from '@awm/client';
import type { FleetResponse } from './types';

const AGENTS = svc('agents');

export function fetchFleet(): Promise<FleetResponse> {
  return AGENTS.fn<FleetResponse>('list_fleet');
}

/** Persist a partial fleet-config patch (merged + validated by the contract). */
function saveFleetConfig(values: Record<string, unknown>): Promise<unknown> {
  return AGENTS.fn('save_fleet_config', { values });
}

/** Persist column visibility/order. */
export function saveColumns(
  column_order: string[],
  hidden_columns: string[],
): Promise<unknown> {
  return saveFleetConfig({ column_order, hidden_columns });
}

/** Persist last-used spawn defaults so the overlay prefills them next time. */
export function saveSpawnDefaults(defaults: Record<string, string>): Promise<unknown> {
  return saveFleetConfig({ spawn_defaults: defaults });
}

/** Persist the app-level desktop-notifications gate. */
export function saveNotificationsEnabled(enabled: boolean): Promise<unknown> {
  return saveFleetConfig({ notifications_enabled: enabled });
}

export interface SpawnResult {
  tmux_session: string;
  cwd: string;
  harness: string;
  model: string | null;
  effort: string | null;
  command: string;
  /** Present on scope spawns. */
  project?: string;
  scope?: string;
}

/** Adhoc spawn: a plain agent at a bare directory. */
export function spawnAgent(args: {
  cwd: string;
  harness: string;
  model?: string;
  effort?: string;
}): Promise<SpawnResult> {
  return AGENTS.fn<SpawnResult>('spawn', args);
}

/** Scope spawn: an agent in a scope's worktree (provisioned if absent). */
export function spawnScoped(args: {
  project: string;
  scope: string;
  harness: string;
  model?: string;
  effort?: string;
}): Promise<SpawnResult> {
  return AGENTS.fn<SpawnResult>('spawn_scoped', args);
}

export interface ScopeRow {
  project: string;
  scope: string;
  worktree: string;
  status: string | null;
  branch: string | null;
}

/** List awm scopes for the spawn picker. */
export function listScopes(project?: string): Promise<{ scopes: ScopeRow[] }> {
  return AGENTS.fn<{ scopes: ScopeRow[] }>('list_scopes', project ? { project } : {});
}

/** Dispose an attachable session (kill its tmux). */
export function killTmux(tmux_session: string): Promise<{ ok: boolean }> {
  return AGENTS.fn<{ ok: boolean }>('kill_tmux', { tmux_session });
}

/** Resolve/forget a non-attachable session's open attention items. */
export function forgetSession(session_id: string): Promise<unknown> {
  return AGENTS.fn('resolve', { session_id });
}

export function markSeen(id: string): Promise<unknown> {
  return AGENTS.fn('mark_seen', { id }).catch(() => undefined);
}
