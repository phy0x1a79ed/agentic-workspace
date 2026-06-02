/**
 * Tiny fetch wrappers used by ChatInput / SlashPicker. Relative URLs throughout
 * so the same code works under the legacy monolith origin and under any stripe
 * prefix (the hub serves /rooms/* at the same origin as every stripe bundle).
 *
 * `X-Awm-As` is set explicitly from the `awm_as` cookie. Through the hub the
 * hub overwrites the header from the same cookie source, so the value lands
 * either way; for direct dev-server hits (Vite at :12103) this is the only
 * thing setting it.
 */

import type { Post, SlashCatalog, AgentSlashResult } from './types';

let _awmAsCache = '';
export function awmAs(): string {
  if (_awmAsCache) return _awmAsCache;
  if (typeof document === 'undefined') return 'user:operator';
  const m = document.cookie.match(/(?:^|; )awm_as=([^;]*)/);
  _awmAsCache = 'user:' + (m ? decodeURIComponent(m[1]) : 'operator');
  return _awmAsCache;
}

export class ChatApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function _api<T>(
  method: 'GET' | 'POST',
  path: string,
  body?: unknown,
): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers: { 'X-Awm-As': awmAs() },
  };
  if (body !== undefined) {
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  const text = await r.text();
  let json: unknown = null;
  if (text) {
    try { json = JSON.parse(text); } catch { /* non-JSON */ }
  }
  if (!r.ok) {
    const detail = (json as { detail?: unknown })?.detail ?? text;
    throw new ChatApiError(
      `${method} ${path} → ${r.status}: ${String(typeof detail === 'string' ? detail : JSON.stringify(detail)).slice(0, 400)}`,
      r.status,
      detail,
    );
  }
  return json as T;
}

export const fetchSlashCommands = (roomId: string, scope: string) =>
  _api<SlashCatalog>(
    'GET',
    `/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(scope)}/slash-commands`,
  );

export const postText = (roomId: string, body: string, to_scope?: string | null) =>
  _api<Post>(
    'POST',
    `/rooms/${encodeURIComponent(roomId)}/posts`,
    to_scope ? { body, to_scope } : { body },
  );

export const runAgentSlash = (roomId: string, scope: string, cmd: string) =>
  _api<AgentSlashResult>(
    'POST',
    `/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(scope)}/slash`,
    { cmd },
  );
