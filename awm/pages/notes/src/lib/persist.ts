// Tab-open state persistence (cookies) + deep-link routing (URL hash).
//
// • Cookie `notes_state` remembers the last-open note, which folders are
//   collapsed, and which side panels are open — so reopening the page resumes
//   exactly where you left off.
// • The URL hash (`#<note-id>`) deep-links to a specific note; it overrides the
//   remembered note on load and is kept in sync as you switch notes, so the URL
//   is copyable/shareable and survives a refresh.

const COOKIE = 'notes_state';
const MAX_AGE = 60 * 60 * 24 * 365; // 1 year

export interface TabState {
  last: string | null;      // last-open note id
  collapsed: string[];      // collapsed folder paths
  left: boolean;            // notes panel open
  right: boolean;           // vocabulary panel open
}

const DEFAULT: TabState = { last: null, collapsed: [], left: true, right: false };

export function readState(): TabState {
  try {
    const m = document.cookie.match(new RegExp('(?:^|; )' + COOKIE + '=([^;]*)'));
    if (!m) return { ...DEFAULT };
    const s = JSON.parse(decodeURIComponent(m[1]));
    return {
      last: typeof s.last === 'string' ? s.last : null,
      collapsed: Array.isArray(s.collapsed) ? s.collapsed.filter((x: unknown) => typeof x === 'string') : [],
      left: s.left !== false,
      right: s.right === true,
    };
  } catch {
    return { ...DEFAULT };
  }
}

export function writeState(s: TabState): void {
  try {
    const v = encodeURIComponent(JSON.stringify(s));
    document.cookie = `${COOKIE}=${v}; path=/; max-age=${MAX_AGE}; samesite=lax`;
  } catch { /* cookies blocked — non-fatal */ }
}

// ---- deep-link hash ------------------------------------------------------

/** The note id in the URL hash (`#<id>`), or null. */
export function readHashId(): string | null {
  const h = decodeURIComponent(location.hash.replace(/^#/, '')).trim();
  return h ? h : null;
}

/** Point the URL at a note (or clear it) without adding a history entry. */
export function writeHashId(id: string | null): void {
  const target = id ? `#${id}` : '';
  if ((location.hash || '') === target) return;
  const url = location.pathname + location.search + target;
  try { history.replaceState(null, '', url); }
  catch { location.hash = id ?? ''; }
}
