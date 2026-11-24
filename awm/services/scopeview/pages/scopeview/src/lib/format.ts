/** Text helpers shared by the panels. */

/** HTML-escapes text for `{@html}`, quotes included so it is safe inside an attribute. */
export const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const cell = (v: unknown, empty = '') =>
  v === null || v === undefined ? empty : typeof v === 'object' ? JSON.stringify(v) : String(v);
