// Markdown *source* syntax highlighting for the editor overlay. highlight.js's
// markdown grammar emits `.hljs-<scope>` spans (section=heading, strong,
// emphasis, code, bullet, quote, link, string) which we colour with
// component-scoped `:global(.hljs-*)` rules in App.svelte — no global theme
// stylesheet (keeps the tokens-only-global CSS invariant).
//
// The output is HTML-escaped by highlight.js, so it is safe to inject behind the
// transparent textarea with {@html}. A trailing newline keeps the last line's
// box height in step with the textarea when the buffer ends without one.

import hljs from 'highlight.js/lib/core';
import markdown from 'highlight.js/lib/languages/markdown';

hljs.registerLanguage('markdown', markdown);

export function highlightMarkdown(src: string): string {
  const text = src ?? '';
  const html = hljs.highlight(text, { language: 'markdown' }).value;
  // Mirror the textarea's own trailing blank line so scroll/height stay aligned.
  return text.endsWith('\n') || text === '' ? html + '\n' : html;
}
