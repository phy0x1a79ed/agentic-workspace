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
  /** Figures only: the table its marks are rows of, and the column each mark's customdata holds. */
  rows?: { table: string; key: string } | null;
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
  /** Any non-tabular file small enough to send; JSON arrives pretty-printed. */
  text?: string;
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

/** Metasmith DAG geometry: a node's ancestors down to raw tables, plus its direct children. */
export interface Layout {
  width: number;
  height: number;
  row_pitch: number;
  font_size: number;
  nodes: {
    id: string;
    stage: Stage;
    kind: 'given' | 'data' | 'transform' | 'target';
    cx: number;
    cy: number;
    label_x: number;
    marker_w: number;
    marker_h: number;
    label: string;
    stale: boolean;
    focus: boolean;
  }[];
  edges: { from: string; to: string; back: boolean; d: string }[];
}

/** Rows of one table that a citation selects. `rows` is capped; `total` counts them all. */
export interface TraceRows {
  table: string;
  where: string;
  total?: number;
  columns?: string[];
  rows?: Record<string, unknown>[];
  /** The figure whose marks the selection named. */
  via?: string;
  error?: string;
}

export interface Trace {
  lit: string[];
  steps: { cite: string; node: string; stage: Stage; title: string; text?: string }[];
  rows: TraceRows[];
}

export interface ChatMessage {
  seq: number;
  by: 'guest' | 'host';
  from: string;
  text: string;
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
export const layout = (c: Ctx, id: string) => scopeview.call<Layout>('layout', { ...at(c), id }, opts(c));
export const history = (c: Ctx, id?: string) =>
  scopeview
    .call<{ commits: Commit[] }>('history', { ...at(c), ...(id ? { id } : {}) }, opts(c))
    .then((r) => r.commits);
export const trace = (c: Ctx, cites: string[]) => scopeview.call<Trace>('trace', { ...at(c), cites }, opts(c));

type Study = Pick<Ctx, 'peer' | 'project' | 'scope'>;
/** The curator's room after `after`. `wait` holds the call open, up to 25 s, until a message arrives. */
export const chat = (c: Study, after: number, wait = 0) =>
  scopeview.call<{ room: string; ended: boolean; messages: ChatMessage[]; next: number }>(
    'chat',
    { project: c.project, scope: c.scope, after, ...(wait ? { wait } : {}) },
    opts(c),
  );
export const say = (c: Study, text: string, context: Record<string, string>) =>
  scopeview.call<{ seq: number }>('say', { project: c.project, scope: c.scope, text, context }, opts(c));
