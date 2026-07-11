/** Column metadata + the section/status vocabulary shared across the page. */
import type { FleetSession, FleetState } from './types';

export interface ColumnDef {
  key: string;
  label: string;
  /** Right-aligned numeric-ish column. */
  numeric?: boolean;
}

// Authoritative label + alignment per known column key. Order/visibility come
// from the saved config; this only maps a key to how it renders.
export const COLUMN_DEFS: Record<string, ColumnDef> = {
  status: { key: 'status', label: 'status' },
  title: { key: 'title', label: 'title' },
  harness: { key: 'harness', label: 'agent' },
  model: { key: 'model', label: 'model' },
  uptime: { key: 'uptime', label: 'up', numeric: true },
  last_activity: { key: 'last_activity', label: 'seen', numeric: true },
  attention: { key: 'attention', label: '!', numeric: true },
  attachable: { key: 'attachable', label: 'term' },
  tokens: { key: 'tokens', label: 'EOOT', numeric: true },
  context: { key: 'context', label: 'ctx', numeric: true },
  dispose: { key: 'dispose', label: '' },
};

export const ALL_COLUMNS = Object.keys(COLUMN_DEFS);

// Fixed grid track per column so cells line up vertically across every row
// (per-row `auto`/flex sizes to that row's own content, which is exactly why the
// old layout didn't align). `title` takes the flexible remainder and ellipsizes.
const COLUMN_TRACK: Record<string, string> = {
  status: '96px',
  title: 'minmax(0, 1fr)',
  harness: '72px',
  model: '92px',
  uptime: '46px',
  last_activity: '52px',
  attention: '34px',
  attachable: '30px',
  tokens: '58px',
  context: '52px',
  dispose: '44px',
};

/** The `grid-template-columns` value for the current visible column set. */
export function gridTemplate(cols: ColumnDef[]): string {
  return cols.map((c) => COLUMN_TRACK[c.key] ?? 'auto').join(' ');
}

// Status sections, most-urgent first. `starting` sits just above `working` so a
// freshly-spawned agent is visible immediately, then transitions down as it boots.
export const SECTION_ORDER: FleetState[] = [
  'needs-you', 'error', 'starting', 'working', 'idle', 'ended',
];

export const STATE_LABEL: Record<string, string> = {
  'needs-you': 'needs you',
  error: 'error',
  starting: 'starting',
  working: 'working',
  idle: 'idle',
  ended: 'ended',
};

export const STATE_ICON: Record<string, string> = {
  'needs-you': '●',
  error: '▲',
  starting: '◌',
  working: '▶',
  idle: '◦',
  ended: '×',
};

export function sectionOf(state: string): FleetState {
  return (SECTION_ORDER as string[]).includes(state)
    ? (state as FleetState)
    : 'idle';
}

// -- formatting --------------------------------------------------------------

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—';
  const v = Math.round(n);
  if (v < 1000) return `${v}`;
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)}k`;
  if (v < 1_000_000_000) return `${(v / 1_000_000).toFixed(v < 10_000_000 ? 1 : 0)}M`;
  return `${(v / 1_000_000_000).toFixed(1)}B`;
}

export function labelFor(s: FleetSession): string {
  return s.title || s.project || (s.cwd ? s.cwd.split('/').slice(-2).join('/') : '')
    || s.session_id.slice(0, 8);
}

/** The visible columns in order = column_order minus hidden. */
export function visibleColumns(order: string[], hidden: string[]): ColumnDef[] {
  const hide = new Set(hidden);
  const seen = new Set<string>();
  const out: ColumnDef[] = [];
  for (const k of order) {
    if (hide.has(k) || seen.has(k) || !COLUMN_DEFS[k]) continue;
    seen.add(k);
    out.push(COLUMN_DEFS[k]);
  }
  return out;
}

/** The cell text for the "value" columns (status/attachable/dispose render specially). */
export function cellText(col: string, s: FleetSession, now: number): string {
  switch (col) {
    case 'title': return labelFor(s);
    case 'harness': return s.harness;
    case 'model': return s.model ? s.model.replace(/^claude-/, '') : '—';
    case 'uptime': return fmtDuration(s.uptime);
    case 'last_activity': return fmtDuration(now - s.last_seen);
    case 'attention': return s.attention ? `${s.attention}` : '';
    case 'tokens': return fmtTokens(s.eoot);
    case 'context': return fmtTokens(s.context_tokens);
    default: return '';
  }
}
