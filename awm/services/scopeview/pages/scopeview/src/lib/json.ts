/** Pretty-printed JSON as HTML with its keys, strings, numbers and atoms wrapped for colour. Every character is escaped. */

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const TOKEN = /("(?:[^"\\]|\\.)*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

export function colourJson(text: string): string {
  let out = '';
  let last = 0;
  for (const m of text.matchAll(TOKEN)) {
    out += esc(text.slice(last, m.index));
    last = m.index + m[0].length;
    if (m[1]) out += `<span class="${m[2] ? 'key' : 'string'}">${esc(m[1])}</span>${m[2] ?? ''}`;
    else out += `<span class="${/^[tfn]/.test(m[0]) ? 'atom' : 'number'}">${m[0]}</span>`;
  }
  return out + esc(text.slice(last));
}
