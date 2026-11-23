/** A small markdown subset rendered to HTML. Every character of the source is escaped before any tag is added. */

const esc = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const inline = (s: string) =>
  esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

export function renderMarkdown(src: string): string {
  const out: string[] = [];
  const lines = src.replace(/\r/g, '').split('\n');
  let para: string[] = [];
  let list = null as { tag: 'ul' | 'ol'; items: string[] } | null;
  const flush = () => {
    if (para.length) out.push(`<p>${inline(para.join(' '))}</p>`);
    if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join('')}</${list.tag}>`);
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
    const item = /^\s*(?:([-*])|\d+\.)\s+(.*)$/.exec(ln);
    if (heading) {
      flush();
      out.push(`<h${heading[1].length + 2}>${inline(heading[2])}</h${heading[1].length + 2}>`);
    } else if (item) {
      const tag = item[1] ? 'ul' : 'ol';
      if (para.length || list?.tag !== tag) flush();
      list ??= { tag, items: [] };
      list.items.push(item[2]);
    } else if (!ln.trim()) {
      flush();
    } else if (list) {
      list.items[list.items.length - 1] += ' ' + ln.trim();
    } else {
      para.push(ln.trim());
    }
  }
  flush();
  return out.join('\n');
}
