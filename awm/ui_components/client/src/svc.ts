/**
 * svc(name) — thin wrapper over the conventional /svc/<name>/ surface
 * every awm service exposes. Three call shapes covered:
 *
 *   svc('tts').fn('listEngines')           → POST /svc/tts/fn/listEngines
 *   svc('tts').session('call', { ... })    → POST /svc/tts/session/call
 *   svc('tts').ws(sessionId)               → wss://<host>/svc/tts/session/<id>
 *
 * `url(suffix)` is the escape hatch for anything that doesn't fit (e.g.
 * a service that exposes a non-standard path). Built on apiFetch so the
 * awm_session cookie and X-Awm-As header travel uniformly.
 */

import { apiFetch, type ApiFetchInit } from './auth';

export interface SvcClient {
  /** POST /svc/<name>/fn/<fn> with a JSON body. */
  fn<T = unknown>(fn: string, args?: unknown): Promise<T>;
  /** POST /svc/<name>/session/<verb> with a JSON body. */
  session<T = unknown>(verb: string, args?: unknown): Promise<T>;
  /** Open a WebSocket to /svc/<name>/session/<id> (https→wss). */
  ws(sessionId: string, protocols?: string | string[]): WebSocket;
  /** Build a path under /svc/<name>/. */
  url(suffix: string): string;
  /** Low-level: apiFetch scoped under /svc/<name>/. */
  request<T = unknown>(suffix: string, init?: ApiFetchInit): Promise<T>;
}

export function svc(name: string): SvcClient {
  const prefix = `/svc/${name}`;
  return {
    fn<T>(fn: string, args: unknown = {}): Promise<T> {
      return apiFetch<T>(`${prefix}/fn/${fn}`, { method: 'POST', body: args });
    },
    session<T>(verb: string, args: unknown = {}): Promise<T> {
      return apiFetch<T>(`${prefix}/session/${verb}`, { method: 'POST', body: args });
    },
    ws(sessionId: string, protocols?: string | string[]): WebSocket {
      const u = new URL(`${prefix}/session/${sessionId}`, window.location.href);
      u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
      return protocols !== undefined
        ? new WebSocket(u.toString(), protocols)
        : new WebSocket(u.toString());
    },
    url(suffix: string): string {
      return suffix.startsWith('/') ? `${prefix}${suffix}` : `${prefix}/${suffix}`;
    },
    request<T>(suffix: string, init: ApiFetchInit = {}): Promise<T> {
      const path = suffix.startsWith('/') ? `${prefix}${suffix}` : `${prefix}/${suffix}`;
      return apiFetch<T>(path, init);
    },
  };
}

/** Turn an absolute http(s):// URL or path into its ws(s):// equivalent. */
export function toWsUrl(pathOrUrl: string): string {
  const u = new URL(pathOrUrl, window.location.href);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}
