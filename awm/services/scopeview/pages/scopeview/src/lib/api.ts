/** Typed view of the scopeview service's read verbs; `new` and `accept` are left to agents. */
import { tool, type CallOpts } from '@awm/client';

const scopeview = tool('scopeview');

export type Stage = 'table' | 'question' | 'figure' | 'interpretation' | 'point' | 'prose';

export interface ScopeInfo {
  project: string;
  scope: string;
  datasets: string[];
  chain: boolean;
}

export interface GraphNode {
  id: string;
  stage: Stage;
  title: string;
  path: string;
  hash?: string | null;
  used_by: string[];
  /** Table stage only. `path` is a table a chain node names that no dataset lists. */
  kind?: 'table' | 'product' | 'path';
  dataset?: string;
  subject?: string;
  files?: number;
  samples?: number;
  gaps?: Record<string, string>[];
  source?: string | string[] | null;
  /** Chain stages only. */
  from?: string[];
  artifact?: string | null;
  made_by?: string | null;
  status?: 'draft' | 'accepted';
  review?: { note: string; on: string } | null;
  stale?: boolean;
  stale_inputs?: string[];
  upstream_stale?: boolean;
  stale_ok?: unknown;
}

export interface Problem {
  node: string;
  code: string;
  message: string;
}

export interface Graph {
  commit: string;
  ref: string | null;
  stages: Stage[];
  nodes: GraphNode[];
  edges: { from: string; to: string }[];
  problems: Problem[];
  ok: boolean;
}

export interface Preview {
  format: string;
  file?: string;
  rows?: number;
  bytes?: number;
  columns?: { name: string; type?: string }[];
  head?: Record<string, unknown>[];
}

export interface PlotlyFigure {
  data: unknown[];
  layout?: Record<string, unknown>;
}

export interface NodeDetail extends GraphNode {
  preview?: Preview;
  listing?: { file: string }[];
  body?: string;
  figure?: PlotlyFigure;
  artifact_error?: string;
  made_by_source?: string;
}

export interface Commit {
  commit: string;
  short: string;
  date: string;
  author: string;
  subject: string;
}

/** Where a call reads: a scope on one node, at a ref ('' is the worktree as it stands). */
export interface Ctx {
  peer: string;
  project: string;
  scope: string;
  ref: string;
}

const opts = (c: { peer: string }): CallOpts => (c.peer && c.peer !== 'local' ? { peer: c.peer } : {});
const at = (c: Ctx) => ({ project: c.project, scope: c.scope, ...(c.ref ? { ref: c.ref } : {}) });

export const scopes = (peer: string) =>
  scopeview.call<{ scopes: ScopeInfo[] }>('scopes', {}, opts({ peer })).then((r) => r.scopes);
export const graph = (c: Ctx) => scopeview.call<Graph>('graph', at(c), opts(c));
export const node = (c: Ctx, id: string) => scopeview.call<NodeDetail>('node', { ...at(c), id }, opts(c));
/** `history` takes no ref: it always walks back from the worktree's HEAD. */
export const history = (c: Ctx, id?: string) =>
  scopeview
    .call<{ commits: Commit[] }>('history', { project: c.project, scope: c.scope, ...(id ? { id } : {}) }, opts(c))
    .then((r) => r.commits);
