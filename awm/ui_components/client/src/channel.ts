/**
 * Scope channel client.
 *
 *   postToScope(project, scope, body)         -> append a human message to the scope
 *   fetchScope(project, scope)                -> backlog of posts (one-shot)
 *
 * Both are verbs of the `journal` tool over `POST /invoke`:
 *   fetch(project, scope, kind?, limit?, order?, before_ts?)
 *   post(project, scope, author, body, kind?, meta?)
 */

import { tool } from './invoke';
import { awmAs } from './auth';

/**
 * One post on a scope channel. Field names match the backend (`author`,
 * `body`, `ts`).
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
  const { posts } = await tool('journal').call<{ posts: ScopePost[]; total: number }>(
    'fetch',
    { project, scope, order: 'asc', ...opts },
  );
  return posts ?? [];
}

export interface PostOpts {
  /** Defaults to the current operator identity (`awmAs()`, e.g. 'user:operator'). */
  author?: string;
  kind?: string;
  meta?: Record<string, unknown>;
}

/** Append a message to the channel. Returns the stored (normalized) post. */
export async function postToScope(
  project: string,
  scope: string,
  body: string,
  opts: PostOpts = {},
): Promise<ScopePost> {
  const { post } = await tool('journal').call<{ post: ScopePost }>('post', {
    project,
    scope,
    author: opts.author ?? awmAs(),
    body,
    kind: opts.kind ?? 'message',
    ...(opts.meta ? { meta: opts.meta } : {}),
  });
  return post;
}
