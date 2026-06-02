<script lang="ts">
  import type { Snippet } from 'svelte';

  // Contenteditable composer where PTT utterances render as atomic,
  // non-editable pills with a × delete button. Keyboard typing produces
  // plain text nodes between them. The pill text itself is immutable —
  // cursor cannot enter it; backspace at its boundary removes the whole
  // pill. The DOM is the source of truth (no Svelte-model re-render fight).
  //
  // During an active PTT press the panel calls beginLiveChunk() once, then
  // updateLiveChunk(text) on every partial, then finalizeLiveChunk(text)
  // on stt_result. The live pill renders a skeleton while empty and
  // updates in place as the partial grows.

  interface Props {
    disabled?: boolean;
    onsend?: (text: string) => void;
    ptt?: Snippet;
    // Mock-only seed: chunk text strings or { chunk, text } pairs.
    initialChunks?: Array<string | { chunk?: string; text?: string }>;
  }
  let {
    disabled = false,
    onsend,
    ptt,
    initialChunks,
  }: Props = $props();

  let editor: HTMLDivElement | null = $state(null);
  let savedRange: Range | null = null;
  let kbdShown = $state(true);

  // The currently-streaming pill (null when no PTT press in flight, or after
  // the user × it). `liveAbandoned` blocks respawn within the same PTT
  // session — user × on the live pill means "drop this utterance entirely",
  // even though the backend keeps sending partials until release.
  let liveChunk: HTMLSpanElement | null = null;
  let liveAbandoned = false;

  const ZWS = '​'; // legacy zero-width space — stripped from output (older inserts may still carry it)

  function stripZws(s: string | null | undefined): string {
    return (s ?? '').replace(/​/g, '');
  }

  // Visible-text neighbor inspection: walks past empty / ZWS-only text nodes
  // so the "is there a space at the boundary?" check looks at actual content.
  function endsWithSpaceVisible(beforeNode: Node): boolean {
    let n: Node | null = beforeNode.previousSibling;
    while (n) {
      if (n.nodeType === Node.TEXT_NODE) {
        const v = stripZws((n as Text).nodeValue);
        if (v.length === 0) { n = n.previousSibling; continue; }
        return /[\s]$/.test(v);
      }
      if (n instanceof HTMLElement && n.classList.contains('chunk')) return false;
      return true;
    }
    return true;
  }
  function startsWithSpaceVisible(afterNode: Node): boolean {
    let n: Node | null = afterNode.nextSibling;
    while (n) {
      if (n.nodeType === Node.TEXT_NODE) {
        const v = stripZws((n as Text).nodeValue);
        if (v.length === 0) { n = n.nextSibling; continue; }
        return /^[\s]/.test(v);
      }
      if (n instanceof HTMLElement && n.classList.contains('chunk')) return false;
      return true;
    }
    return true;
  }
  function ensureLeadingSpace(node: Node) {
    if (!node.parentNode) return;
    if (endsWithSpaceVisible(node)) return;
    node.parentNode.insertBefore(document.createTextNode(' '), node);
  }
  function ensureTrailingSpaceNode(node: Node): Text {
    // Returns the text node sitting immediately after `node` that begins with
    // a real space — creating one if absent. The caret-place step uses this
    // as a landing spot so adjacent content stays separated.
    let after = node.nextSibling;
    if (after instanceof Text && /^[ \t]/.test(after.nodeValue ?? '')) {
      return after;
    }
    const t = document.createTextNode(' ');
    if (node.parentNode) node.parentNode.insertBefore(t, node.nextSibling);
    return t;
  }

  function makeChunk(text: string, opts?: { live?: boolean }): HTMLSpanElement {
    const span = document.createElement('span');
    span.className = opts?.live ? 'chunk live' : 'chunk';
    span.contentEditable = 'false';
    span.dataset.chunkId = `c${Math.floor(performance.now() * 1000)}-${Math.floor(Math.random() * 1e6)}`;

    const textWrap = document.createElement('span');
    textWrap.className = 'chunk-text';
    if (text) textWrap.textContent = text;
    span.appendChild(textWrap);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'chunk-del';
    del.contentEditable = 'false';
    del.tabIndex = -1;
    del.setAttribute('aria-label', 'delete chunk');
    del.textContent = '×';
    span.appendChild(del);

    return span;
  }

  function setChunkText(chunk: HTMLSpanElement, text: string) {
    const wrap = chunk.querySelector('.chunk-text') as HTMLSpanElement | null;
    if (wrap) wrap.textContent = text;
  }

  function chunkTextOf(chunk: HTMLSpanElement): string {
    const wrap = chunk.querySelector('.chunk-text') as HTMLSpanElement | null;
    return stripZws(wrap?.textContent ?? '');
  }

  function placeCaretAfter(node: Node) {
    // Always leave a real space immediately after the pill (and consequently
    // before any later neighbor). The caret lands after that space so typing
    // and re-inserted pills both stay separated.
    const sel = window.getSelection();
    if (!sel) return;
    const tail = ensureTrailingSpaceNode(node);
    const r = document.createRange();
    r.setStart(tail, Math.min(1, (tail.nodeValue ?? '').length));
    r.collapse(true);
    sel.removeAllRanges();
    sel.addRange(r);
    savedRange = r.cloneRange();
  }

  function spliceAtCaret(node: Node) {
    if (!editor) return;
    editor.focus({ preventScroll: true });
    const sel = window.getSelection();
    let range: Range | null = null;
    if (savedRange && editor.contains(savedRange.startContainer)) {
      range = savedRange;
      if (sel) {
        sel.removeAllRanges();
        sel.addRange(range);
      }
    } else if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      range = sel.getRangeAt(0);
    }
    if (!range) {
      editor.appendChild(node);
    } else {
      range.deleteContents();
      range.insertNode(node);
    }
    ensureLeadingSpace(node);
    placeCaretAfter(node);
  }

  /** One-shot insertion (used by mock paths and any non-streaming callers). */
  export function insertChunk(text: string) {
    if (!editor || !text) return;
    spliceAtCaret(makeChunk(text));
  }

  /** Snapshot the caret so the next pill insertion lands where the user
   *  was editing, even if focus drifts during recording. */
  export function captureCaret() {
    if (!editor) return;
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) {
      savedRange = sel.getRangeAt(0).cloneRange();
    } else {
      savedRange = null;
    }
  }

  /** Start a new PTT session: drops any prior live pill ref, clears the
   *  "abandoned" flag, and inserts a fresh live skeleton at the caret. */
  export function beginLiveChunk() {
    if (!editor) return;
    liveAbandoned = false;
    liveChunk = makeChunk('', { live: true });
    spliceAtCaret(liveChunk);
  }

  /** Update the streaming text on the current live pill. If the live pill
   *  was destroyed mid-stream (e.g., by Send clearing the editor), respawn
   *  one at the end — unless the user explicitly × it. */
  export function updateLiveChunk(text: string) {
    if (!editor || liveAbandoned) return;
    if (!liveChunk || !editor.contains(liveChunk)) {
      liveChunk = makeChunk(text, { live: true });
      editor.appendChild(liveChunk);
      ensureLeadingSpace(liveChunk);
      placeCaretAfter(liveChunk);
      return;
    }
    setChunkText(liveChunk, text);
  }

  /** Finalize on stt_result: drop the .live class. If text is empty or the
   *  user abandoned it, remove the pill (pills cannot be empty). */
  export function finalizeLiveChunk(text: string) {
    const wasAbandoned = liveAbandoned;
    liveAbandoned = false;
    if (wasAbandoned) {
      liveChunk = null;
      return;
    }
    let chunk = liveChunk;
    liveChunk = null;
    if (!chunk || !editor?.contains(chunk)) {
      if (!text) return;
      chunk = makeChunk(text);
      if (editor) {
        editor.appendChild(chunk);
        ensureLeadingSpace(chunk);
        placeCaretAfter(chunk);
      }
      return;
    }
    if (!text.trim()) {
      chunk.remove();
      return;
    }
    setChunkText(chunk, text);
    chunk.classList.remove('live');
  }

  function walkText(): string {
    if (!editor) return '';
    let out = '';
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        let p: Node | null = n.parentNode;
        while (p && p !== editor) {
          if (p instanceof HTMLElement && p.classList.contains('chunk-del')) {
            return NodeFilter.FILTER_REJECT;
          }
          p = p.parentNode;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let node = walker.nextNode();
    while (node) {
      out += node.nodeValue ?? '';
      node = walker.nextNode();
    }
    // Strip ZWS leftovers, collapse runs of spaces/tabs (newlines preserved
    // so manual line breaks survive), trim.
    return stripZws(out).replace(/[ \t]+/g, ' ').trim();
  }

  function pruneEmptyChunks() {
    if (!editor) return;
    for (const c of editor.querySelectorAll('.chunk')) {
      const el = c as HTMLSpanElement;
      // Never prune the live skeleton — it's intentionally empty while waiting
      // for the first partial.
      if (el === liveChunk || el.classList.contains('live')) continue;
      if (!chunkTextOf(el).trim()) el.remove();
    }
  }

  function onEditorInput() {
    pruneEmptyChunks();
  }

  function onEditorClick(e: MouseEvent) {
    const t = e.target as HTMLElement | null;
    if (!t) return;
    const del = t.closest('.chunk-del') as HTMLElement | null;
    if (!del) return;
    e.preventDefault();
    e.stopPropagation();
    const chunk = del.closest('.chunk') as HTMLSpanElement | null;
    if (!chunk) return;
    if (chunk === liveChunk) {
      // Abandon the active PTT session — don't respawn from later partials.
      liveAbandoned = true;
      liveChunk = null;
    }
    chunk.remove();
    editor?.focus({ preventScroll: true });
  }

  function isCaretAdjacentToChunk(direction: 'before' | 'after'): HTMLSpanElement | null {
    const sel = window.getSelection();
    if (!sel || !sel.isCollapsed || !editor) return null;
    const range = sel.getRangeAt(0);
    const { startContainer, startOffset } = range;
    const lookBack = direction === 'before';

    // Walk from the caret toward the desired direction; skip ZWS-only text
    // nodes; first non-trivial sibling decides.
    let candidate: Node | null = null;
    if (startContainer.nodeType === Node.TEXT_NODE) {
      const text = startContainer.nodeValue ?? '';
      if (lookBack) {
        const left = text.slice(0, startOffset).replace(/​/g, '');
        if (left.length > 0) return null;
        candidate = startContainer.previousSibling;
      } else {
        const right = text.slice(startOffset).replace(/​/g, '');
        if (right.length > 0) return null;
        candidate = startContainer.nextSibling;
      }
      // Walk up if we ran off the end of a text node at the editor root.
      let p: Node | null = startContainer.parentNode;
      while (!candidate && p && p !== editor) {
        candidate = lookBack ? p.previousSibling : p.nextSibling;
        p = p.parentNode;
      }
    } else if (startContainer === editor) {
      const idx = lookBack ? startOffset - 1 : startOffset;
      candidate = editor.childNodes[idx] ?? null;
    } else {
      return null;
    }

    while (candidate && candidate.nodeType === Node.TEXT_NODE) {
      const t = (candidate.nodeValue ?? '').replace(/​/g, '');
      if (t.length > 0) return null;
      candidate = lookBack ? candidate.previousSibling : candidate.nextSibling;
    }
    if (candidate instanceof HTMLElement && candidate.classList.contains('chunk')) {
      return candidate as HTMLSpanElement;
    }
    return null;
  }

  function onEditorKeydown(e: KeyboardEvent) {
    if (e.key === 'Backspace') {
      const target = isCaretAdjacentToChunk('before');
      if (target) {
        e.preventDefault();
        if (target === liveChunk) {
          liveAbandoned = true;
          liveChunk = null;
        }
        target.remove();
      }
    } else if (e.key === 'Delete') {
      const target = isCaretAdjacentToChunk('after');
      if (target) {
        e.preventDefault();
        if (target === liveChunk) {
          liveAbandoned = true;
          liveChunk = null;
        }
        target.remove();
      }
    }
  }

  function onSendClick() {
    const text = walkText();
    if (!text) return;
    onsend?.(text);
    // Clear EVERYTHING — including the live pill. If PTT is still in flight
    // the next partial will respawn the live pill at the (now-empty) caret.
    // The post-send finalized text will include the post-send portion only
    // from the user's POV (backend re-transcribes from start; treat the
    // resulting pill as the new utterance — the user can edit it).
    if (editor) editor.replaceChildren();
    liveChunk = null;
    savedRange = null;
  }

  function isEditorFocused(): boolean {
    return !!editor && document.activeElement === editor;
  }
  function onToggleKeyboard() {
    if (!editor) return;
    if (isEditorFocused()) {
      editor.blur();
      kbdShown = false;
    } else {
      editor.focus({ preventScroll: true });
      kbdShown = true;
    }
  }
  function onFocus() { kbdShown = true; }
  function onBlur() { kbdShown = false; }

  $effect(() => {
    if (!editor || !initialChunks) return;
    for (const item of initialChunks) {
      if (typeof item === 'string') {
        if (!item) continue;
        const c = makeChunk(item);
        editor.appendChild(c);
        ensureLeadingSpace(c);
        ensureTrailingSpaceNode(c);
      } else {
        if (item.chunk) {
          const c = makeChunk(item.chunk);
          editor.appendChild(c);
          ensureLeadingSpace(c);
          ensureTrailingSpaceNode(c);
        }
        if (item.text) editor.appendChild(document.createTextNode(item.text));
      }
    }
  });
</script>

<section class="composer-shell">
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    bind:this={editor}
    class="editor"
    class:disabled
    contenteditable={!disabled}
    role="textbox"
    aria-multiline="true"
    aria-label="message composer"
    spellcheck="true"
    tabindex={disabled ? -1 : 0}
    oninput={onEditorInput}
    onclick={onEditorClick}
    onkeydown={onEditorKeydown}
    onfocus={onFocus}
    onblur={onBlur}
  ></div>

  <div class="footer">
    <button
      type="button"
      class="footer-btn kbd"
      onclick={onToggleKeyboard}
      title={kbdShown ? 'hide keyboard' : 'show keyboard'}
      aria-label={kbdShown ? 'hide keyboard' : 'show keyboard'}
    >{kbdShown ? '⌨ ↓' : '⌨ ↑'}</button>

    <div class="ptt-slot">
      {#if ptt}{@render ptt()}{/if}
    </div>

    <button
      type="button"
      class="footer-btn send"
      onclick={onSendClick}
      {disabled}
      title="send"
      aria-label="send"
    >SEND</button>
  </div>
</section>

<style>
  .composer-shell {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    max-width: 480px;
    width: 100%;
  }

  .editor {
    min-height: 96px;
    max-height: 240px;
    overflow-y: auto;
    padding: var(--space-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.8;
    outline: none;
    transition: border-color 0.12s;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
  .editor:focus { border-color: var(--atomizer); }
  .editor.disabled {
    opacity: 0.5;
    background: var(--surface2);
  }
  .editor:empty::before {
    content: 'type, or hold the PTT button to dictate…';
    color: var(--text3);
    font-style: italic;
    pointer-events: none;
  }

  /* Atomic pill. contenteditable=false set on the element itself; styling
     scoped under .editor via :global because pills are JS-created. */
  :global(.editor .chunk) {
    display: inline-flex;
    align-items: center;
    gap: 0;
    padding: 1px 0 1px 8px;
    margin: 0 2px;
    background: color-mix(in oklab, var(--recording) 16%, var(--surface2));
    border: 1px solid color-mix(in oklab, var(--recording) 50%, var(--border));
    border-radius: var(--radius-md);
    color: var(--text);
    line-height: 1.5;
    user-select: none;
    vertical-align: baseline;
  }
  :global(.editor .chunk .chunk-text) {
    display: inline-block;
    min-height: 1em;
  }
  :global(.editor .chunk-del) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin: 0 2px 0 8px;
    padding: 0 8px;
    height: 1.4em;
    background: transparent;
    border: 0;
    border-left: 1px solid color-mix(in oklab, var(--recording) 50%, var(--border));
    color: var(--text2);
    font-family: var(--mono);
    font-size: 13px;
    cursor: pointer;
    user-select: none;
  }
  :global(.editor .chunk-del:hover) { color: var(--danger); }

  /* Live (streaming) pill — skeleton dots while text is empty, trailing
     pulse bar while the partial is mid-stream. */
  :global(.editor .chunk.live) {
    background: color-mix(in oklab, var(--recording) 26%, var(--surface2));
    border-color: var(--recording);
    box-shadow: 0 0 0 1px color-mix(in oklab, var(--recording) 30%, transparent);
  }
  :global(.editor .chunk.live .chunk-text) {
    color: var(--text);
  }
  :global(.editor .chunk.live .chunk-text:empty::before) {
    content: '• • •';
    letter-spacing: 1px;
    color: var(--text2);
    font-family: var(--mono);
    animation: ptt-skel 1s ease-in-out infinite;
  }
  :global(.editor .chunk.live .chunk-text::after) {
    content: '';
    display: inline-block;
    width: 4px;
    height: 0.95em;
    margin-left: 3px;
    background: var(--recording);
    vertical-align: text-bottom;
    animation: ptt-pulse 1s ease-in-out infinite;
  }

  .footer {
    display: flex;
    align-items: stretch;
    gap: var(--space-2);
  }
  .ptt-slot {
    flex: 1 1 auto;
    display: flex;
  }
  .ptt-slot :global(*) {
    flex: 1 1 auto;
  }
  .footer-btn {
    flex: 0 0 auto;
    min-height: 52px;
    min-width: 52px;
    padding: 0 14px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .footer-btn:hover  { background: var(--surface3); color: var(--text); }
  .footer-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .footer-btn.send {
    border-color: color-mix(in oklab, var(--atomizer) 40%, var(--border));
    color: var(--text);
  }
  .footer-btn.send:hover {
    background: color-mix(in oklab, var(--atomizer) 30%, var(--surface2));
    border-color: var(--atomizer);
  }
  @media (hover: hover) and (pointer: fine) {
    .footer-btn.kbd { display: none; }
  }
  @media (hover: none), (max-width: 720px) {
    .footer-btn { min-height: 60px; font-size: 13px; }
    .editor { font-size: 16px; }
  }

  @keyframes ptt-pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
  @keyframes ptt-skel {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 0.95; }
  }
</style>
