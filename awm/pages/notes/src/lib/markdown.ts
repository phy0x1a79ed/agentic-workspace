// Markdown → sanitized HTML for the render toggle. `marked` for parsing,
// `dompurify` for a defense-in-depth scrub before we set innerHTML (the content
// is the user's own, but rendering untrusted-shaped markdown warrants a scrub).

import { marked } from 'marked';
import DOMPurify from 'dompurify';

marked.setOptions({ gfm: true, breaks: true });

export function renderMarkdown(src: string): string {
  const raw = marked.parse(src ?? '', { async: false }) as string;
  return DOMPurify.sanitize(raw);
}
