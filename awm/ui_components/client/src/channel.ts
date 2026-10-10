/**
 * Scope channel client.
 *
 *   postToScope(project, scope, body)         -> append a human message to the scope
 *   fetchScope(project, scope)                -> backlog of posts (one-shot)
 *
 * Everything rides `svc()` / `apiFetch`, the same `/svc/<name>` surface every
 * other service call uses.
 *
 * Backend ops (over /svc/scopes/fn/*):
 *   scope_fetch(project, scope, kind?, limit?, order?, before_ts?)
 *   scope_post(project, scope, author, body, kind?, meta?, to_scope?)
 */

import { svc } from './svc';
import { awmAs } from './auth';

/**
 * One post on a scope channel. Field names match the backend (`author`,
 * `body`, `ts`) and are structurally compatible with `@awm/tts-history`'s
 * `Post`, so fetched/streamed posts drop straight into `<TtsHistory>`.
 */
export interface ScopePost {
  id?: string;
  project?: string;
  scope?: string;
  /** 'agent:<scope>' | 'user:name' | 'system' (display form). */
  author: string;
  /** 'message' | 'journal' | 'system' | 'tool_use' | 'tool_result' | … */
  kind?: string;
  body: string;
  meta?: Record<string, unknown>;
  /** ISO timestamp. Always present on backend posts. */
  ts: string;
  to_scope?: string | string[];
  [k: string]: unknown;
}

/** True for posts authored by an agent (vs a human or the system). */
export function isAgentPost(p: { author?: string }): boolean {
  return (p.author ?? '').startsWith('agent:');
}

export interface FetchOpts {
  kind?: string;
  limit?: number;
  order?: 'asc' | 'desc';
  before_ts?: string;
}

/** Read the channel backlog. Oldest→newest by default (chat order). */
export async function fetchScope(
  project: string,
  scope: string,
  opts: FetchOpts = {},
): Promise<ScopePost[]> {
  const { posts } = await svc('scopes').fn<{ posts: ScopePost[]; total: number }>(
    'scope_fetch',
    { project, scope, order: 'asc', ...opts },
  );
  return posts ?? [];
}

export interface PostOpts {
  /** Defaults to the current operator identity (`awmAs()`, e.g. 'user:operator'). */
  author?: string;
  kind?: string;
  to_scope?: string;
  meta?: Record<string, unknown>;
}

/** Append a message to the channel. Returns the stored (normalized) post. */
export async function postToScope(
  project: string,
  scope: string,
  body: string,
  opts: PostOpts = {},
): Promise<ScopePost> {
  const { post } = await svc('scopes').fn<{ post: ScopePost }>('scope_post', {
    project,
    scope,
    author: opts.author ?? awmAs(),
    body,
    kind: opts.kind ?? 'message',
    ...(opts.to_scope ? { to_scope: opts.to_scope } : {}),
    ...(opts.meta ? { meta: opts.meta } : {}),
  });
  return post;
}
