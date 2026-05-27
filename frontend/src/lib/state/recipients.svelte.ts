/**
 * Per-room recipient selection persisted in localStorage. Ported from
 * awm/static/index.html lines 1547–1582. Stored as
 *   {"<room_id>": ["scope:<identifier>", ...]}
 */

const KEY = 'awm.focus.recipients';

function readAll(): Record<string, string[]> {
  if (typeof localStorage === 'undefined') return {};
  let raw: unknown = null;
  try { raw = JSON.parse(localStorage.getItem(KEY) || '{}'); }
  catch { return {}; }
  if (!raw || typeof raw !== 'object') return {};
  const out: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (Array.isArray(v)) out[k] = v.filter((x): x is string => typeof x === 'string');
    else if (typeof v === 'string') out[k] = [v];
    else out[k] = [];
  }
  return out;
}

function writeAll(m: Record<string, string[]>): void {
  if (typeof localStorage === 'undefined') return;
  try { localStorage.setItem(KEY, JSON.stringify(m)); } catch { /* ignore quota */ }
}

class RecipientsStore {
  private cache = $state<Record<string, string[]>>(readAll());

  get(roomId: string): string[] {
    return this.cache[roomId] ?? [];
  }

  set(roomId: string, keys: string[]): void {
    this.cache = { ...this.cache, [roomId]: [...new Set(keys)] };
    writeAll(this.cache);
  }

  toggle(roomId: string, key: string): void {
    const cur = new Set(this.get(roomId));
    if (cur.has(key)) cur.delete(key); else cur.add(key);
    this.set(roomId, [...cur]);
  }
}

export const recipients = new RecipientsStore();
