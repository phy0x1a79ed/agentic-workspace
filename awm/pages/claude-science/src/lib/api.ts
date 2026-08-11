/**
 * Typed view of the claude-science service's verb surface.
 *
 * Everything goes through `svc('claude-science').fn(...)` — the standard
 * gateway client — so the awm_session cookie and the caller identity header
 * travel uniformly, and the page never hand-rolls a fetch.
 *
 * Note where this page is served from: the *gateway* front (:12100), while the
 * workbench itself lives on :12201. That is deliberate — it means this page
 * still loads and still reports when the workbench fronts are down, which is
 * exactly when someone is looking at it.
 */

import { svc } from '@awm/client';

const cs = svc('claude-science');

export interface DaemonState {
  running: boolean;
  pid: number | null;
  version: string | null;
  port: number | null;
  sandbox_port: number | null;
  /** True if the daemon was already up when the service arrived. */
  adopted: boolean | null;
  restarts_by_us: number;
  supervised_since: number | null;
  error?: string | null;
}

export interface InstallStep {
  path: string;
  installed?: boolean;
  provisioned?: boolean;
  running?: boolean;
}

export interface FrontState {
  listener_port: number;
  upstream: string;
  tls: boolean;
  san: string[] | null;
  serving: boolean;
  error: string | null;
}

export interface BridgeState {
  listening_port: number | null;
  mounted: boolean;
  prefix: string;
  allowlist: Record<string, string[]>;
  tool_count: number;
  error: string | null;
}

export interface Status {
  daemon: DaemonState;
  install: { binary: InstallStep; data_dir: InstallStep; daemon: InstallStep };
  fronts: Record<string, FrontState>;
  mcp_bridge: BridgeState;
  origins: string[];
}

export const status = () => cs.fn<Status>('status');
export const start = () => cs.fn<DaemonState>('start');
export const stop = () => cs.fn<DaemonState>('stop');
export const restart = () => cs.fn<DaemonState>('restart');
export const logs = (tail = 200) => cs.fn<{ tail: number; log: string }>('logs', { tail });
export const signinUrl = () => cs.fn<{ url: string }>('signin_url').then((r) => r.url);
export const updateCheck = () =>
  cs.fn<{ installed_version: string | null; output: string }>('update_check');
