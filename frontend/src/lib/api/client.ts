/**
 * Thin HTTP wrapper around the awm backend. Behaviour ported verbatim from
 * the legacy SPA's `api()` helper (awm/static/index.html lines 850–877):
 *
 *  - bearer lives in an HttpOnly cookie, so we just `credentials: 'include'`
 *  - X-Awm-As header carries the "user:<name>" identity (read from the
 *    `awm_as` cookie at boot)
 *  - 401 → redirect to /ui/login.html (cookie missing/expired)
 *  - response is buffered to text once; JSON parsed from the buffer so
 *    error paths don't double-consume the stream
 */

function readCookie(name: string): string {
  if (typeof document === 'undefined') return '';
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

let _as = '';
export function awmAs(): string {
  if (!_as) _as = 'user:' + (readCookie('awm_as') || 'operator');
  return _as;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

export async function api<T = unknown>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown
): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers: { 'X-Awm-As': awmAs() }
  };
  if (body !== undefined) {
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) {
    // In Vite dev (12103) we don't have a session cookie — surface the 401 to
    // callers so they can show an error rather than bouncing to login.html
    // (which lives only on the prod 12101 origin). In prod, follow the legacy
    // behaviour: cookie missing/expired → re-login via Discord or CLI.
    if (typeof location !== 'undefined' && !import.meta.env?.DEV) {
      location.replace('/ui/login.html');
    }
    throw new ApiError('unauthorized', 401, null);
  }
  const text = await r.text();
  let json: unknown = null;
  if (text) {
    try { json = JSON.parse(text); } catch { /* non-JSON */ }
  }
  if (!r.ok) {
    const detail = (json as { detail?: unknown })?.detail ?? text;
    throw new ApiError(
      `${method} ${path} → ${r.status}: ${String(typeof detail === 'string' ? detail : JSON.stringify(detail)).slice(0, 400)}`,
      r.status,
      detail
    );
  }
  return json as T;
}

/* ─── Typed endpoints ────────────────────────────────────────────────── */

// Backend-derived types come from `generated.ts` (regenerate with
// `npm run gen-types`). The migration is intentionally narrow: types that
// match the generated schema 1:1 are re-exported as aliases below; types
// that diverge (Room reshape, voice-engine dict responses, untyped peer/scope
// endpoints) stay hand-written until the backend tightens its response_model
// declarations. Engine CONFIG_SCHEMA blobs are explicitly out of this seam —
// the spec's `dict[str, Any]` becomes `unknown` and is not useful here.
import type { components } from './generated';

export type VagrantSessionResponse = components['schemas']['VagrantSessionResponse'];

export interface Peer {
  peer_id: string;
  friendly_name?: string;
  ssh_alias?: string;
  remote_port?: number | string;
  last_seen?: string;
}
export interface PeersResponse { peers: Peer[] }
export interface PingResponse { ok: boolean; rtt_ms?: number; error?: string }

export interface Project {
  name: string;
  scope_counts: { active: number; completed: number; deleted: number };
}
export interface ProjectsResponse { projects: Project[] }

export interface Scope {
  project: string;
  scope: string;
  branch: string;
  status: string;
  worktree: string;
}
export interface ScopesResponse { scopes: Scope[] }

export interface CoreStatus {
  leadership_state?: 'ACTIVE' | 'STANDBY' | string;
  current_leader?: string;
  [k: string]: unknown;
}

export interface Room {
  id: string;
  topic?: string;
  status: string;
  host_peer_id?: string;
  created_at?: string;
  [k: string]: unknown;
}
export interface RoomsResponse { rooms: Room[] }
export interface RoomsSearchResponse extends RoomsResponse { degraded?: { peer_id: string }[] }

export interface RoomParticipant {
  identifier: string;
  kind: 'subscriber' | 'shadow_peer' | 'agent' | string;
  joined_at?: string;
  left_at?: string | null;
}
export interface RoomDetail extends Room { participants: RoomParticipant[] }

export interface LiveAgent {
  pid?: number | string;
  status?: string;
  agent_cli?: string;
  permission_mode?: string;
  model?: string;
  effort?: string;
  started_at?: string;
  exited_at?: string | null;
  exit_code?: number | null;
}
export interface RoomAgent {
  scope: string;
  kind: string;
  live?: LiveAgent | null;
}
export interface RoomAgentsResponse { agents: RoomAgent[] }

export interface Post {
  id?: string;
  ts: string;
  /** Author scope identifier — backend field name is `author`. */
  author: string;
  /** Recipient — single scope or list. Backend may omit on broadcast. */
  to_scope?: string | string[];
  /** Post text — backend field name is `body`. */
  body: string;
  kind?: string;
  [k: string]: unknown;
}
export interface RoomHistoryResponse { posts: Post[] }

export interface SlashCommandInfo {
  name: string;          // leading slash, e.g. "/restart"
  args: string;          // display string, e.g. "[mode]"
  description: string;
}
export interface SlashCommandsResponse {
  server: SlashCommandInfo[];
  claude: string[];      // bare claude command names (no leading slash)
}

export interface AgentSlashResponse { handled: boolean; result: string }

export interface EngineSpec {
  schema: { properties?: Record<string, unknown>; [k: string]: unknown };
  defaults: Record<string, unknown>;
}
export type EnginesByKind = Record<string, Record<string, EngineSpec>>;

export interface LoadedEngine {
  id: string;
  params: Record<string, unknown>;
  instance_id: string;
}
export type LoadedEnginesByKind = Record<string, LoadedEngine | null>;

/* ─── Convenience helpers ────────────────────────────────────────────── */

export const peerInfo      = ()                          => api<Peer>('GET', '/peer');
export const listPeers     = ()                          => api<PeersResponse>('GET', '/peers');
export const pingPeer      = (id: string)                => api<PingResponse>('GET', `/peers/${encodeURIComponent(id)}/ping`);
export const listProjects  = ()                          => api<ProjectsResponse>('GET', '/projects');
export const listScopes    = (status = 'active')         => api<ScopesResponse>('GET', `/scopes?status=${encodeURIComponent(status)}`);
export const coreStatus    = ()                          => api<CoreStatus>('GET', '/status');

export const listRooms = (status = 'active', peer = '') => {
  const p = new URLSearchParams({ status });
  if (peer) p.set('peer', peer);
  return api<RoomsResponse>('GET', `/rooms?${p}`);
};
export const searchRooms = (q: string, peer = '') => {
  const p = new URLSearchParams({ q });
  if (peer) p.set('peer', peer);
  return api<RoomsSearchResponse>('GET', `/rooms/search?${p}`);
};
export const createRoom = (body: { topic?: string | null; scopes?: string[]; prompts?: Record<string, string>; close_on_exit?: boolean }) =>
  api<{ id: string }>('POST', '/rooms', body);
export const getRoom        = (id: string) => api<RoomDetail>('GET', `/rooms/${encodeURIComponent(id)}`);
export const getRoomAgents  = (id: string) => api<RoomAgentsResponse>('GET', `/rooms/${encodeURIComponent(id)}/agents`);
export const getRoomHistory = (id: string, params: Record<string, string> = {}) =>
  api<RoomHistoryResponse>('GET', `/rooms/${encodeURIComponent(id)}/history?${new URLSearchParams(params)}`);
export const postToRoom = (id: string, body: { body: string; to_scope?: string | null }) =>
  api<Post>('POST', `/rooms/${encodeURIComponent(id)}/posts`, body);
export const closeRoom   = (id: string, kill_agents = false) =>
  api<{ ok: true }>('POST', `/rooms/${encodeURIComponent(id)}/close`, { kill_agents });
export const archiveRoom = (id: string)               => api<{ ok: true }>('POST', `/rooms/${encodeURIComponent(id)}/archive`);
export const inviteToRoom = (id: string, scope: string) =>
  api<{ ok: true }>('POST', `/rooms/${encodeURIComponent(id)}/invite`, { scope });
export const runAgentSlash = (id: string, scope: string, cmd: string) =>
  api<AgentSlashResponse>('POST', `/rooms/${encodeURIComponent(id)}/agents/${encodeURIComponent(scope)}/slash`, { cmd });
export const ensureVagrantSession = () =>
  api<VagrantSessionResponse>('POST', '/vagrant/session');

export const listVoiceEngines = () =>
  api<EnginesByKind>('GET', '/voice/engines');
export const getLoadedEngines = () =>
  api<LoadedEnginesByKind>('GET', '/voice/engines/loaded');
export const loadEngine = (kind: string, id: string, params: Record<string, unknown> = {}) =>
  api<LoadedEngine>('POST', `/voice/engines/${encodeURIComponent(kind)}/load`, { id, params });
export const unloadEngine = (kind: string) =>
  api<{ unloaded: boolean }>('POST', `/voice/engines/${encodeURIComponent(kind)}/unload`);

/** Persisted global engine selection: ``{stt: {engine_id, params} | null, tts: ...}``. */
export interface GlobalEngineSelection { engine_id: string; params: Record<string, unknown> }
export type GlobalEnginesByKind = Record<string, GlobalEngineSelection | null>;
export const getGlobalEngines = () =>
  api<GlobalEnginesByKind>('GET', '/voice/engines/global');
export const putGlobalEngine = (kind: string, engine_id: string, params: Record<string, unknown> = {}) =>
  api<GlobalEnginesByKind>('PUT', `/voice/engines/${encodeURIComponent(kind)}/global`, { engine_id, params });
/** Fetch synth audio blob (audio/wav). 409 = no tts engine loaded. */
export async function ttsSpeakBlob(text: string): Promise<Blob> {
  const r = await fetch('/voice/tts/speak', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-Awm-As': awmAs() },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) {
    let detail = '';
    try { detail = await r.text(); } catch { /* ignore */ }
    throw new ApiError(`tts ${r.status}${detail ? ': ' + detail.slice(0, 200) : ''}`, r.status, detail);
  }
  return r.blob();
}
export const getSlashCommands = (id: string, scope: string) =>
  api<SlashCommandsResponse>('GET', `/rooms/${encodeURIComponent(id)}/agents/${encodeURIComponent(scope)}/slash-commands`);
