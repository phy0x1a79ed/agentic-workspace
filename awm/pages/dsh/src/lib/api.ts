/**
 * Typed view of the dsh service's verb surface.
 *
 * Everything goes through `svc('dsh').fn(...)` — the standard gateway client —
 * so the awm_session cookie and the caller identity header travel uniformly,
 * and the page never hand-rolls a fetch.
 *
 * Note where this page is served from: the *gateway* front (:12100), while the
 * harness GUI lives on :12301. That is deliberate — it means this page still
 * loads and still reports when the harness front is down, which is exactly when
 * someone is looking at it.
 */

import { svc } from '@awm/client';

const dsh = svc('dsh');

/** The model route's three failure modes. All present as "nothing answers" in
 *  the GUI, so they are reported apart. */
export interface RouteState {
  settings: string;
  provider_declared: boolean;
  /** `<provider>/<model>` the harness will use for a new session. */
  default_model: string | null;
  /** False when the selection still points at the profile's DeepSeek-direct
   *  default, whose credential this workspace does not hold. */
  default_model_ours: boolean;
  key_env: string;
  key_available: boolean;
  key_source: string;
}

export interface HarnessState {
  installed: boolean;
  version: string | null;
  running: boolean;
  pid: number | null;
  exit_code: number | null;
  port: number;
  /** Process alive is not the same fact as port bound; a start that failed to
   *  bind is running and useless. */
  listening: boolean;
  uptime_s: number | null;
  home: string;
  runtime: string;
  workdir: string;
  node_bin: string | null;
  model_route: RouteState;
  error: string | null;
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

export interface Status {
  harness: HarnessState;
  front: FrontState;
}

export const status = () => dsh.fn<Status>('status');
export const start = () => dsh.fn<HarnessState>('start');
export const stop = () => dsh.fn<HarnessState>('stop');
export const restart = () => dsh.fn<HarnessState>('restart');
export const logs = (tail = 200) => dsh.fn<{ tail: number; log: string }>('logs', { tail });
export const url = () => dsh.fn<{ url: string }>('url').then((r) => r.url);
