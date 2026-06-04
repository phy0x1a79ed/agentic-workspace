<script lang="ts">
  // Import VoiceChip purely so its :global chip styles ship in the bundle.
  // The chip DOM is still created imperatively below to keep the
  // contenteditable caret stable across mutations.
  import VoiceChip from '../frontend/src/lib/primitives/VoiceChip.svelte';
  void VoiceChip;

  // Contenteditable composer where PTT utterances render as atomic,
  // non-editable pills with a × delete button. Keyboard typing produces
  // plain text nodes between them. The pill text itself is immutable —
  // cursor cannot enter it; backspace at its boundary removes the whole
  // pill. The DOM is the source of truth (no Svelte-model re-render fight).
  //
  // During an active PTT press the shell calls beginLiveChunk() once, then
  // updateLiveChunk(text) on every partial, then finalizeLiveChunk(text)
  // on stt_result. The live pill renders a skeleton while empty and
  // updates in place as the partial grows.
  //
  // This is the PTT tab inside PttComposerShell. Send + PTT-button slot
  // are now shell-level; only the contenteditable surface + its kbd
  // hide/show toggle live here.

  interface Props {
    disabled?: boolean;
    // Mock-only seed: chunk text strings or { chunk, text } pairs.
    initialChunks?: Array<string | { chunk?: string; text?: string }>;
  }
  let {
    disabled = false,
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

  /** Concatenate text nodes (excluding × buttons), collapse runs of
   *  spaces/tabs to single spaces, trim. Used by the shell's Send. */
  export function walkText(): string {
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
    return stripZws(out).replace(/[ \t]+/g, ' ').trim();
  }

  /** Clear the editor — including any live pill. If PTT is still in flight
   *  the next partial respawns the live pill at the (now-empty) caret. */
  export function clear() {
    if (editor) editor.replaceChildren();
    liveChunk = null;
    savedRange = null;
  }

  function pruneEmptyChunks() {
    if (!editor) return;
    for (const c of editor.querySelectorAll('.chunk')) {
      const el = c as HTMLSpanElement;
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

<div class="ptt-tab">
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

  <button
    type="button"
    class="kbd-btn"
    onclick={onToggleKeyboard}
    title={kbdShown ? 'hide keyboard' : 'show keyboard'}
    aria-label={kbdShown ? 'hide keyboard' : 'show keyboard'}
  >{kbdShown ? '⌨ ↓' : '⌨ ↑'}</button>
</div>

<style>
  .ptt-tab {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
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

  .kbd-btn {
    align-self: flex-end;
    min-height: 32px;
    min-width: 48px;
    padding: 0 10px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text2);
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 1px;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .kbd-btn:hover { background: var(--surface3); color: var(--text); }
  @media (hover: hover) and (pointer: fine) {
    .kbd-btn { display: none; }
  }
  @media (hover: none), (max-width: 720px) {
    .editor { font-size: 16px; }
  }
</style>
