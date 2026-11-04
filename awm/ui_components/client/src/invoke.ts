/**
 * tool(name) and openSession() — a page's two doors into the gateway.
 *
 *   tool('mic').call('status')                → POST /invoke {name:'mic', args:{verb:'status', args:{}}}
 *   openSession('mic', 'stream', { ... })     → wss://<host>/hub/session/mic/stream, init as frame 1
 *
 * A verb's reply is `{result: "<string>"}`, the string being the verb's JSON
 * result; `call` parses it and falls back to the raw string. A refusal is a
 * non-2xx `{detail}`, thrown as an HttpError (an AuthError on 401/403) whose
 * message is the detail. Built on apiFetch so the awm_session cookie and
 * X-Awm-As header travel uniformly.
 */

import { apiFetch, HttpError } from './auth';

export interface CallOpts {
  /** Run the verb on another node. */
  peer?: string;
}

export interface ToolClient {
  /** Run one verb of the tool. */
  call<T = unknown>(verb: string, args?: Record<string, unknown>, opts?: CallOpts): Promise<T>;
}

export async function invoke<T = unknown>(
  name: string,
  verb: string,
  args: Record<string, unknown> = {},
  opts: CallOpts = {},
): Promise<T> {
  const envelope: Record<string, unknown> = { verb, args };
  if (opts.peer) envelope.peer = opts.peer;
  let reply: { result?: unknown };
  try {
    reply = await apiFetch<{ result?: unknown }>('/invoke', {
      method: 'POST',
      body: { name, args: envelope },
    });
  } catch (e) {
    if (e instanceof HttpError) e.message = `${name}.${verb}: ${detailOf(e.body) || e.message}`;
    throw e;
  }
  const result = reply?.result;
  if (typeof result !== 'string') return result as T;
  try {
    return JSON.parse(result) as T;
  } catch {
    return result as unknown as T;
  }
}

export function tool(name: string): ToolClient {
  return {
    call<T>(verb: string, args: Record<string, unknown> = {}, opts: CallOpts = {}): Promise<T> {
      return invoke<T>(name, verb, args, opts);
    },
  };
}

export interface SessionOpts extends CallOpts {
  protocols?: string | string[];
}

/**
 * Open a direct session on a service. The init object goes out as the first
 * text frame; every frame after that is the service's. A refusal closes the
 * socket with code 1011 and the reason.
 */
export function openSession(
  service: string,
  kind: string,
  init: Record<string, unknown> = {},
  opts: SessionOpts = {},
): WebSocket {
  const path = `/hub/session/${encodeURIComponent(service)}/${encodeURIComponent(kind)}`;
  const u = new URL(toWsUrl(path));
  if (opts.peer) u.searchParams.set('peer', opts.peer);
  const ws = opts.protocols !== undefined
    ? new WebSocket(u.toString(), opts.protocols)
    : new WebSocket(u.toString());
  // Registered before any caller handler, so init is always the first frame.
  ws.addEventListener('open', () => ws.send(JSON.stringify(init)));
  return ws;
}

/** Turn an absolute http(s):// URL or path into its ws(s):// equivalent. */
export function toWsUrl(pathOrUrl: string): string {
  const u = new URL(pathOrUrl, window.location.href);
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}

function detailOf(body: string): string {
  try {
    const d = (JSON.parse(body) as { detail?: unknown }).detail;
    if (d === undefined) return '';
    return typeof d === 'string' ? d : JSON.stringify(d);
  } catch {
    return body.slice(0, 240);
  }
}
