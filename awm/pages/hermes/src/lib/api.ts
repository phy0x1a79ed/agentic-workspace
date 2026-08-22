/**
 * Typed view of the hermes service's verb surface.
 *
 * Everything goes through `svc('hermes').fn(...)` — the standard gateway client
 * — so the awm_session cookie and the caller identity header travel uniformly,
 * and the page never hand-rolls a fetch.
 *
 * Note where this page is served from: the *gateway* edge (:12100), while the
 * dashboard GUI lives on its own TLS front (:12401). That is deliberate — it
 * means this page still loads and still reports when the front is down, which
 * is exactly when someone is looking at it.
 */

import { svc, HttpError } from '@awm/client';

const hermes = svc('hermes');

/** The transient user-manager unit the dashboard runs in. `available: false`
 *  means this node has no user manager, so the dashboard will NOT outlive
 *  `systemctl restart awm` — a fact worth surfacing before the next deploy. */
export interface UnitState {
  available: boolean;
  unit: string | null;
  active_state?: string | null;
  sub_state?: string | null;
  main_pid?: number | null;
}

export interface DashboardState {
  listening: boolean;
  port: number;
  pids: number[];
  pid: number | null;
  home: string;
  bin: string;
  installed: boolean;
  web_dist: boolean;
  user_unit: UnitState;
  /** null until this service has decided; true = it was already serving. */
  adopted: boolean | null;
  restarts_by_us: number;
  supervised_since: number | null;
}

/** Hermes' own opinion of itself. Absent when the probe could not reach it,
 *  in which case `error` says why — the two are never both meaningful. */
export interface HealthState {
  serving: boolean;
  error?: string;
  api_status?: {
    version?: string;
    overall?: string;
    active_sessions?: number;
    gateway_mode?: string;
    components?: Record<string, { status?: string; state?: string }>;
  };
}

export interface FrontState {
  listener_port: number;
  upstream: string;
  tls: boolean;
  san: string | null;
  serving: boolean;
  error: string | null;
  url: string;
}

export interface ModelState {
  default?: string;
  provider?: string;
  base_url?: string;
  error?: string;
}

export interface Status {
  dashboard: DashboardState;
  health: HealthState;
  front: FrontState;
  landing_url: string;
  model: ModelState;
}

/** True when the gateway has no such service running. The service is
 *  profile-gated (it only bootstraps where AWM_PROFILES names `hermes`) while
 *  this page is discovered on disk unconditionally, so "no service here" is a
 *  normal state to render, not an error to swallow. */
export function isServiceAbsent(e: unknown): boolean {
  return e instanceof HttpError && e.status === 404;
}

export const status = () => hermes.fn<Status>('status');
export const start = () => hermes.fn<DashboardState>('start');
export const stop = () => hermes.fn<DashboardState>('stop');
export const restart = () => hermes.fn<DashboardState>('restart');
export const logs = (tail = 200) =>
  hermes.fn<{ source: string; log: string }>('logs', { tail });
export const url = () =>
  hermes.fn<{ url: string; landing: string; loopback: string; serving: boolean }>('url');
