/**
 * A small markdown subset rendered to HTML. Every character of the source is escaped before any tag is added.
 *
 * Citations (`[[id]]`, `[[id#n]]`, `[[id?conds]]`) become `.cite` chips. Each sentence of a paragraph and each
 * list item is a trace unit: it carries `data-t` (its key in this body) and `data-cites` (the citations inside it).
 * A top-level list item also carries `data-item`, its 1-based place among the body's top-level items, which is
 * how the service's `id#n` counts them.
 */

import { esc } from './format';

const CITE = /\[\[([^\[\]\s]+)\]\]/g;

export const citesIn = (s: string) => [...s.matchAll(CITE)].map((m) => m[1]);
/** The node a citation names, without its item or row selection. */
export const citeNode = (cite: string) => cite.split(/[#?]/, 1)[0];
/** A chip's text: the node id, the item number, and `?` standing for a row selection. */
const citeLabel = (cite: string) => cite.replace(/\?.*$/, '?');

const inline = (s: string) =>
  esc(s)
    .replace(/\s*\[\[([^\[\]\s]+)\]\]/g, (_, c) => `<button class="cite" data-cite="${c}" title="${c}">${citeLabel(c)}</button>`)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

/** A full stop that ends one of these does not end the sentence. A decimal never matches: no space follows its point. */
const ABBREVIATION = /(?:\b(?:e\.g|i\.e|et al|cf|vs|ca|approx|Fig|Figs|Eq|No|sp|spp|Dr)|\b[A-Z])\.$/;
/** Closing punctuation, the citations trailing it, then space before a capital, a digit or an opening mark. */
const BOUNDARY = /[.!?]["')]*(?:\s*\[\[[^\[\]\s]+\]\])*\s+(?=["'(]?[A-Z0-9])/g;

export function sentences(text: string): string[] {
  const out: string[] = [];
  let start = 0;
  for (const m of text.matchAll(BOUNDARY)) {
    if (m[0][0] === '.' && ABBREVIATION.test(text.slice(start, m.index + 1))) continue;
    const end = m.index + m[0].length;
    out.push(text.slice(start, end).trimEnd());
    start = end;
  }
  if (start < text.length) out.push(text.slice(start));
  return out;
}

export function renderMarkdown(src: string): string {
  const out: string[] = [];
  const lines = src.replace(/\r/g, '').split('\n');
  let para: string[] = [];
  let list = null as { tag: 'ul' | 'ol'; items: { text: string; n: number }[] } | null;
  let units = 0;
  let topItems = 0;
  const unit = (text: string) => `data-t="${units++}" data-cites="${esc(citesIn(text).join(' '))}"`;
  const flush = () => {
    if (para.length) {
      const spans = sentences(para.join(' ')).map((s) => `<span class="unit" ${unit(s)}>${inline(s)}</span>`);
      out.push(`<p>${spans.join(' ')}</p>`);
    }
    if (list) {
      const items = list.items.map((i) => `<li class="unit" ${unit(i.text)}${i.n ? ` data-item="${i.n}"` : ''}>${inline(i.text)}</li>`);
      out.push(`<${list.tag}>${items.join('')}</${list.tag}>`);
    }
    para = [];
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    if (ln.startsWith('```')) {
      flush();
      const code: string[] = [];
      while (++i < lines.length && !lines[i].startsWith('```')) code.push(lines[i]);
      out.push(`<pre>${esc(code.join('\n'))}</pre>`);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(ln);
    const item = /^(\s*)(?:([-*+])|\d+[.)])\s+(.*)$/.exec(ln);
    if (heading) {
      flush();
      out.push(`<h${heading[1].length + 2}>${inline(heading[2])}</h${heading[1].length + 2}>`);
    } else if (item) {
      const tag = item[2] ? 'ul' : 'ol';
      if (para.length || list?.tag !== tag) flush();
      list ??= { tag, items: [] };
      list.items.push({ text: item[3], n: item[1] ? 0 : ++topItems });
    } else if (!ln.trim()) {
      flush();
    } else if (list) {
      list.items[list.items.length - 1].text += ' ' + ln.trim();
    } else {
      para.push(ln.trim());
    }
  }
  flush();
  return out.join('\n');
}
