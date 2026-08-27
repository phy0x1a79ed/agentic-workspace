/**
 * Typed view of the trilium service's verb surface.
 *
 * Everything goes through `svc('trilium').fn(...)` — the standard gateway
 * client — so the awm_session cookie and the caller identity header travel
 * uniformly, and the page never hand-rolls a fetch.
 *
 * Note where this page is served from: the *gateway* front (:12100), while each
 * person's Trilium lives on its own front in the 12501 band. That is
 * deliberate — this page still loads and still reports when someone's front is
 * down, which is exactly when someone is looking at it.
 */

import { svc } from '@awm/client';

const trilium = svc('trilium');

/** One person's server. Reported per user because "the service is up" and
 *  "this person can reach their notes" are different facts. */
export interface InstanceState {
  user: string;
  running: boolean;
  pid: number | null;
  exit_code: number | null;
  port: number;
  /** Process alive is not the same fact as port bound; a start that failed to
   *  bind is running and useless. */
  listening: boolean;
  uptime_s: number | null;
  scope: string;
  data_dir: string;
  /** Rolling database copies present, e.g. `backup-daily.db`. Empty means this
   *  person has nothing to recover from yet. */
  backups: string[];
  log: string;
  error: string | null;
}

export interface FrontState {
  user: string;
  listener_port: number;
  upstream: string;
  tls: boolean;
  san: string | null;
  serving: boolean;
  error: string | null;
  url: string;
}

/** Which bundle is being served, and whether it matches the source on disk. */
export interface SourceState {
  entry: string | null;
  /** `fork` when this node builds what it serves, `tarball` when it serves the
   *  published build. A serving-only node reports `tarball` and no revision. */
  source: 'fork' | 'tarball' | null;
  fork_dir: string;
  branch?: string | null;
  describe?: string | null;
  head?: string | null;
  dirty?: boolean | null;
  built_head?: string | null;
  /** False when the bundle on disk was built from a different commit, or from
   *  a clean tree that has since been edited. */
  built_current?: boolean | null;
}

export interface Status {
  instances: InstanceState[];
  fronts: FrontState[];
  source: SourceState;
}

export interface UserInfo {
  user: string;
  slot: number;
  front_port: number;
  upstream_port: number;
  scope: string;
}

export const status = () => trilium.fn<Status>('status');
export const users = () =>
  trilium.fn<{ users: UserInfo[]; userdata_dir: string; max_users: number }>('users');
export const start = (user?: string) => trilium.fn<unknown>('start', { user });
export const stop = (user: string) => trilium.fn<unknown>('stop', { user });
export const restart = (user: string) => trilium.fn<unknown>('restart', { user });
export const logs = (user: string, tail = 200) =>
  trilium.fn<{ user: string; tail: number; log: string }>('logs', { user, tail });
