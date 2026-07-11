/** Shared fleet types — mirror of the notifications service's list_fleet shape. */

export type FleetState = 'needs-you' | 'error' | 'working' | 'idle' | 'ended';

export interface OpenItem {
  id: string;
  session_id: string;
  kind: string;
  title: string;
  detail: string | null;
  snippet: string | null;
  created_at: number;
  notify_at: number;
  seen_at: number | null;
}

export interface FleetSession {
  session_id: string;
  harness: string;
  cwd: string | null;
  project: string | null;
  title: string | null;
  state: string;
  tmux_session: string | null;
  model: string | null;
  tok_in: number;
  tok_out: number;
  tok_cache_write_5m: number;
  tok_cache_write_1h: number;
  tok_cache_read: number;
  context_tokens: number;
  attachable: boolean;
  uptime: number;
  idle_for: number;
  last_seen: number;
  eoot: number | null;
  attention: number;
  open_items: OpenItem[];
}

export interface FleetConfig {
  column_order: string[];
  hidden_columns: string[];
  spawn_defaults: {
    harness: string;
    model: string;
    effort: string;
    scope: string;
  };
}

export interface FleetResponse {
  sessions: FleetSession[];
  now: number;
  config: FleetConfig;
}
