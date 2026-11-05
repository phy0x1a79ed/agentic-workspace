/**
 * Typed view of the trilium service's verb surface.
 *
 * Everything goes through `tool('trilium').call(...)` — the standard gateway
 * client — so the awm_session cookie and the caller identity header travel
 * uniformly, and the page never hand-rolls a fetch.
 *
 * There is one vault, shared by everyone who can sign in, so nothing here takes
 * a user: the verbs act on the vault and the session says who is asking.
 *
 * **Only the read verbs are here, and that is enforced elsewhere.** The service
 * refuses `start`, `stop`, `restart`, `restore`, `snapshot`, `export`, `logs`
 * and `init` for any caller arriving through an edge, because the vault is
 * shared: each of those is one person acting on everyone's work, and `restore`
 * discards it. They are operator verbs, run on the host. Leaving them out of
 * this module is the second line of that, not the first.
 */

import { tool } from '@awm/client';

const trilium = tool('trilium');

/** The vault's server. A caller arriving through the edge gets the readable
 *  half; pids, ports and absolute paths are for the console. */
export interface VaultState {
  running: boolean;
  /** Process alive is not the same fact as port bound; a start that failed to
   *  bind is running and useless. */
  listening: boolean;
  /** Whether the vault has a database yet. The service creates one the moment
   *  it sees the child listening without one, so `false` here means that has
   *  not happened yet — or failed, in which case `error` says so. */
  initialized: boolean | null;
  uptime_s: number | null;
  /** Trilium's own rolling copies, e.g. `backup-daily.db`. Overwritten on a
   *  schedule and pinned by nothing, so this is not the recovery answer. */
  backups: string[];
  /** Named snapshots pinned in the vault's scope. This is the recovery answer,
   *  and zero means there is none. */
  snapshots: number;
  error: string | null;
}

/** Which fork release is unpacked, and whether it is the one awm pins.
 *  Absent for a caller arriving through the edge. */
export interface SourceState {
  entry: string | null;
  installed: string | null;
  pinned: string | null;
  /** False when server.pin moved and install.sh has not run since. */
  current: boolean;
}

export interface Status {
  vault: VaultState;
  source?: SourceState;
}

/** One database copy. `snapshot` is named, pinned and durable; `rolling` is
 *  Trilium's own rotation and is overwritten without warning. */
export interface SnapshotInfo {
  name: string;
  file: string;
  kind: 'snapshot' | 'rolling';
  bytes: number;
  modified: string;
  restorable: boolean;
}

export const status = () => trilium.call<Status>('status');
export const snapshots = () =>
  trilium.call<{ snapshots: SnapshotInfo[] }>('snapshots');

/** Where the vault is served. A path, not a URL: it is the same origin as this
 *  page, behind the same session, so there is no host or port to get wrong. */
export const VAULT_PATH = '/trilium/';
