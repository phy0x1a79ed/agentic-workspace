// Local-draft safety mirror (localStorage).
//
// While a note is open its authoritative copy lives in the server's in-memory
// room (collab.ts streams edits there). But if the connection breaks — the tab
// goes offline, the socket drops, the page reloads before an edit was acked —
// the un-synced keystrokes would be lost. So on *every* local edit we also write
// the current text to localStorage, synchronously, as a safety copy. It is NOT a
// second source of truth: whenever the server confirms an edit is synced, the
// draft for that note is cleared, so a lingering draft means exactly "there is
// local work the server hasn't confirmed" — which is what we restore on reload.
//
// Keyed by note id; the not-yet-created blank note uses the '@blank' sentinel,
// migrated to the real id once the note is persisted.

const PREFIX = 'notes_draft:';
const BLANK = '@blank';

export interface Draft {
  content: string;
  path: string;
  ts: number;
}

const keyOf = (id: string | null): string => PREFIX + (id ?? BLANK);

/** Write the current text/path for a note as a local safety copy (synchronous). */
export function writeDraft(id: string | null, path: string, content: string): void {
  try {
    localStorage.setItem(keyOf(id), JSON.stringify({ content, path, ts: Date.now() }));
  } catch {
    /* storage full / blocked — non-fatal */
  }
}

/** The saved draft for a note, or null if none / unreadable. */
export function readDraft(id: string | null): Draft | null {
  try {
    const v = localStorage.getItem(keyOf(id));
    if (!v) return null;
    const d = JSON.parse(v);
    if (typeof d?.content !== 'string') return null;
    return { content: d.content, path: typeof d.path === 'string' ? d.path : '', ts: d.ts || 0 };
  } catch {
    return null;
  }
}

/** Drop a note's draft — called once the server confirms it's synced. */
export function clearDraft(id: string | null): void {
  try {
    localStorage.removeItem(keyOf(id));
  } catch {
    /* non-fatal */
  }
}

/** Re-key the blank-note draft onto its freshly-assigned id (create flow). */
export function migrateBlankDraft(newId: string): void {
  const d = readDraft(null);
  if (d) {
    writeDraft(newId, d.path, d.content);
    clearDraft(null);
  }
}
