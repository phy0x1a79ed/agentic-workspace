<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { notesApi, type Note, type NoteMeta, type Tree } from './lib/api';
  import { buildTree } from './lib/tree';
  import { renderMarkdown } from './lib/markdown';
  import { highlightMarkdown } from './lib/highlight';
  import { createDictation, type DictationState } from './lib/dictation';
  import { createCollab } from './lib/collab';
  import { readState, writeState, readHashId, writeHashId, type TabState } from './lib/persist';
  import NoteTree from './lib/NoteTree.svelte';
  import VocabPanel from './lib/VocabPanel.svelte';

  interface Open {
    id: string | null;      // null = the unsaved blank note (lazy-persisted)
    path: string;
    content: string;
    file_path: string;
    deleted_at: string | null;
  }
  const blank = (): Open => ({ id: null, path: '', content: '', file_path: '', deleted_at: null });

  let tree = $state<Tree>({ active: [], trashed: [] });
  let current = $state<Open>(blank());
  let mode = $state<'edit' | 'preview'>('edit');
  let saveState = $state<'new' | 'editing' | 'saving' | 'synced'>('new');
  let collabLive = $state(false);

  let leftOpen = $state(true);
  let rightOpen = $state(false);
  let collapsed = $state<Set<string>>(new Set());

  let query = $state('');
  let semantic = $state(false);
  let results = $state<NoteMeta[] | null>(null);

  let vocab = $state<string[]>([]);

  let dictState = $state<DictationState>('idle');
  let interim = $state('');
  let micLevel = $state(0);

  let taEl = $state<HTMLTextAreaElement | null>(null);
  let copied = $state(false);

  const treeNodes = $derived(buildTree(tree.active));
  const segs = $derived(current.path.split('/').map((s) => s.trim()).filter(Boolean));
  const rendered = $derived(renderMarkdown(current.content));
  const highlighted = $derived(highlightMarkdown(current.content));
  const words = $derived(current.content.trim() ? current.content.trim().split(/\s+/).length : 0);

  let dictation: ReturnType<typeof createDictation> | null = null;
  let collab: ReturnType<typeof createCollab> | null = null;
  let createTimer: ReturnType<typeof setTimeout> | null = null;
  let pathTimer: ReturnType<typeof setTimeout> | null = null;
  let creating = false;
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let hlEl = $state<HTMLPreElement | null>(null);

  // ---- persistence -------------------------------------------------------
  //
  // Content is not written to disk per-keystroke any more: while a note is open
  // it lives in the server's in-memory room (which the 5-minute flusher and a
  // graceful shutdown persist). Edits stream to that room through `collab`, which
  // also merges a second client's concurrent edits back into this editor. Only
  // structural changes — create, title/path rename — hit disk immediately, since
  // they reshape the tree everyone sees.

  async function loadTree() { tree = await notesApi.tree(true); }
  async function loadVocab() {
    vocab = (await notesApi.vocabList()).terms;
    dictation?.setHotwords(vocab);
  }

  /** A local content edit (typing or dictation). Routes to create-then-join for
   *  a fresh note, or streams into the live room for an existing one. */
  function onLocalEdit() {
    if (current.id === null) {
      saveState = 'new';
      scheduleCreate();
    } else {
      saveState = 'editing';
      collab?.update(current.content);
    }
  }

  function scheduleCreate() {
    if (createTimer) clearTimeout(createTimer);
    createTimer = setTimeout(() => void createNote(), 500);
  }

  async function createNote() {
    if (createTimer) { clearTimeout(createTimer); createTimer = null; }
    if (creating || current.id !== null) return;
    const hasBody = current.content.trim().length > 0 || current.path.trim().length > 0;
    if (!hasBody) return;               // nothing to persist yet — stay ephemeral
    creating = true;
    saveState = 'saving';
    try {
      const n = await notesApi.create(current.path, current.content);
      current.id = n.id;
      current.file_path = n.file_path;
      setOpen(n.id);
      await collab?.join(n.id, current.content);
      // Reconcile the title: edits made while create was in flight (id still
      // null) can't route through savePath, so persist the latest path now.
      if (current.path !== n.path) await notesApi.save(n.id, { path: current.path });
      saveState = 'synced';
      await loadTree();
    } finally {
      creating = false;
    }
  }

  function schedulePathSave() {
    if (current.id === null) { scheduleCreate(); return; }
    if (pathTimer) clearTimeout(pathTimer);
    pathTimer = setTimeout(() => void savePath(), 500);
  }

  async function savePath() {
    if (pathTimer) { clearTimeout(pathTimer); pathTimer = null; }
    if (current.id === null) return;
    await notesApi.save(current.id, { path: current.path });
    await loadTree();
  }

  /** Persist anything pending before switching away from the current note. */
  async function flush() {
    if (createTimer) await createNote();
    if (pathTimer) await savePath();
    await collab?.flush();
  }

  // ---- deep-link + tab state (URL hash + cookie) -------------------------

  function tabState(): TabState {
    return { last: current.id, collapsed: [...collapsed], left: leftOpen, right: rightOpen };
  }
  function persistTab() { writeState(tabState()); }
  function setOpen(id: string | null) { writeHashId(id); persistTab(); }

  // ---- note switching ----------------------------------------------------

  async function openNote(id: string) {
    if (id === current.id) return;
    await flush();
    collab?.leave();
    const n: Note = await notesApi.get(id);
    current = { id: n.id, path: n.path, content: n.content, file_path: n.file_path, deleted_at: n.deleted_at };
    mode = 'edit';
    results = null; query = '';
    saveState = 'synced';
    setOpen(n.id);
    await collab?.join(n.id, n.content);
    await focusEditor();
  }

  async function newNote() {
    await flush();
    collab?.leave();
    current = blank();
    mode = 'edit';
    saveState = 'new';
    setOpen(null);
    await focusEditor();
  }

  async function trashNote(id: string) {
    await notesApi.trash(id);
    if (id === current.id) { collab?.leave(); current = blank(); setOpen(null); }
    await loadTree();
  }
  async function restoreNote(id: string) { await notesApi.restore(id); await loadTree(); }
  async function purgeNote(id: string) {
    await notesApi.purge(id);
    if (id === current.id) { collab?.leave(); current = blank(); setOpen(null); }
    await loadTree();
  }

  function toggleFolder(path: string) {
    const next = new Set(collapsed);
    next.has(path) ? next.delete(path) : next.add(path);
    collapsed = next;
    persistTab();
  }

  function toggleLeft() { leftOpen = !leftOpen; persistTab(); }
  function toggleRight() { rightOpen = !rightOpen; persistTab(); }

  // ---- editing -----------------------------------------------------------

  function onBody() { onLocalEdit(); }
  function onPath() { schedulePathSave(); }

  function syncScroll() {
    if (taEl && hlEl) { hlEl.scrollTop = taEl.scrollTop; hlEl.scrollLeft = taEl.scrollLeft; }
  }

  /** Adopt authoritative text (a merge or a peer's edit) into the editor,
   *  keeping the caret where the user was typing. */
  function applyCollabText(next: string) {
    const el = taEl;
    if (!el || document.activeElement !== el) {
      current.content = next;
      saveState = 'synced';
      return;
    }
    const oldText = current.content;
    const caret = mapCaret(oldText, next, el.selectionStart ?? next.length);
    current.content = next;
    saveState = 'synced';
    void tick().then(() => { if (taEl) { taEl.selectionStart = taEl.selectionEnd = caret; } syncScroll(); });
  }

  /** Map a caret offset from `oldT` onto `newT` via common prefix/suffix. */
  function mapCaret(oldT: string, newT: string, caret: number): number {
    let p = 0;
    const maxP = Math.min(oldT.length, newT.length, caret);
    while (p < maxP && oldT[p] === newT[p]) p++;
    if (caret <= p) return caret;
    const fromEnd = oldT.length - caret;
    let s = 0;
    const maxS = Math.min(fromEnd, newT.length - p);
    while (s < maxS && oldT[oldT.length - 1 - s] === newT[newT.length - 1 - s]) s++;
    if (fromEnd <= s) return newT.length - fromEnd;
    return Math.min(caret, newT.length);
  }

  async function focusEditor() {
    await tick();
    taEl?.focus();
  }

  async function insertAtCaret(text: string) {
    const el = taEl;
    let insert = text;
    if (!el) {
      if (current.content && !/\s$/.test(current.content)) insert = ' ' + insert;
      current.content += insert;
      return;
    }
    const start = el.selectionStart ?? current.content.length;
    const end = el.selectionEnd ?? start;
    const before = current.content.slice(0, start);
    const after = current.content.slice(end);
    if (before && !/\s$/.test(before) && !/^\s/.test(insert)) insert = ' ' + insert;
    current.content = before + insert + after;
    const caret = start + insert.length;
    await tick();
    el.selectionStart = el.selectionEnd = caret;
    el.focus();
  }

  async function copyPath() {
    if (!current.file_path) return;
    try {
      await navigator.clipboard.writeText(current.file_path);
      copied = true;
      setTimeout(() => (copied = false), 1200);
    } catch { /* clipboard blocked — no-op */ }
  }

  // ---- search ------------------------------------------------------------

  function onQuery() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void runSearch(), 200);
  }
  async function runSearch() {
    const q = query.trim();
    if (!q) { results = null; return; }
    const res = await notesApi.search({
      query: q,
      fuzzy: q,
      semantic: semantic ? q : undefined,
      k: 50,
    });
    results = res.results;
  }
  function clearSearch() { query = ''; results = null; }

  // ---- dictation ---------------------------------------------------------

  async function toggleMic() {
    if (!dictation) return;
    if (dictState === 'listening' || dictState === 'connecting') {
      dictation.stop();
      return;
    }
    mode = 'edit';
    dictation.setHotwords(vocab);
    await focusEditor();
    await dictation.start();
  }

  // ---- lifecycle ---------------------------------------------------------

  onMount(async () => {
    dictation = createDictation({
      onInterim: (t) => (interim = t),
      onCommit: (t) => { void insertAtCaret(t).then(onLocalEdit); },
      onState: (s) => (dictState = s),
      onLevel: (v) => (micLevel = v),
    });
    collab = createCollab({
      onText: (t) => applyCollabText(t),
      onStatus: (live) => (collabLive = live),
    });

    // Restore panel/folder state from the cookie before first paint.
    const saved = readState();
    leftOpen = saved.left;
    rightOpen = saved.right;
    collapsed = new Set(saved.collapsed);

    try { await loadVocab(); } catch { /* service may still be booting */ }
    try { await loadTree(); } catch { /* ditto */ }

    // Resume: URL hash wins (deep link), else the last-open note from the cookie.
    const wantId = readHashId() ?? saved.last;
    const known = wantId && tree.active.some((n) => n.id === wantId);
    if (known) {
      try { await openNote(wantId!); } catch { current = blank(); }
    } else {
      current = blank();
      setOpen(null);
    }
    // React to hash edits (back/forward, pasted link) while the page is open.
    window.addEventListener('hashchange', onHashChange);
    await focusEditor();
  });

  async function onHashChange() {
    const id = readHashId();
    if (id && id !== current.id && tree.active.some((n) => n.id === id)) {
      await openNote(id);
    }
  }

  onDestroy(() => {
    dictation?.destroy();
    collab?.leave();
    window.removeEventListener('hashchange', onHashChange);
    if (createTimer) clearTimeout(createTimer);
    if (pathTimer) clearTimeout(pathTimer);
    if (searchTimer) clearTimeout(searchTimer);
  });

  const saveLabel = $derived(
    saveState === 'saving' ? 'Saving…'
    : saveState === 'editing' ? 'Editing…'
    : saveState === 'synced' ? 'Synced'
    : 'New note',
  );
</script>

<div class="app">
  <!-- LEFT: notes tree + trash -->
  <aside class="left" class:closed={!leftOpen}>
    <div class="panel-head">
      <span class="panel-title">Notes</span>
      <button class="ghost-btn" title="New note" aria-label="New note" onclick={newNote}>+</button>
    </div>

    <div class="search">
      <input
        class="search-input"
        placeholder="Search notes…"
        bind:value={query}
        oninput={onQuery}
        spellcheck="false"
        autocomplete="off"
      />
      {#if query}
        <button class="search-clear" aria-label="Clear search" onclick={clearSearch}>×</button>
      {/if}
    </div>
    <label class="sem-toggle">
      <input type="checkbox" bind:checked={semantic} onchange={onQuery} />
      <span>semantic</span>
    </label>

    <div class="tree-scroll">
      {#if results !== null}
        {#if results.length === 0}
          <p class="empty-note">No matches.</p>
        {:else}
          <ul class="results">
            {#each results as r (r.id)}
              <li>
                <button class="result" class:active={r.id === current.id} onclick={() => openNote(r.id)}>
                  <span class="result-path">{r.path || '(untitled)'}</span>
                  {#if r.snippet}<span class="result-snip">{r.snippet}</span>{/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      {:else if tree.active.length === 0}
        <p class="empty-note">No notes yet. Start typing — your note is saved automatically.</p>
      {:else}
        <NoteTree
          nodes={treeNodes}
          currentId={current.id}
          {collapsed}
          onOpen={openNote}
          onTrash={trashNote}
          onToggle={toggleFolder}
        />
      {/if}
    </div>

    {#if tree.trashed.length > 0}
      <div class="trash">
        <div class="trash-head">Trash · {tree.trashed.length}</div>
        <ul class="trash-list">
          {#each tree.trashed as t (t.id)}
            <li class="trash-item">
              <span class="trash-path" title={t.path || '(untitled)'}>{t.path || '(untitled)'}</span>
              <span class="trash-actions">
                <button class="link-btn" onclick={() => restoreNote(t.id)}>restore</button>
                <button class="link-btn danger" onclick={() => purgeNote(t.id)} title="Delete permanently now">delete</button>
              </span>
            </li>
          {/each}
        </ul>
        <p class="trash-foot">Auto-removed 30 days after deletion.</p>
      </div>
    {/if}
  </aside>

  <!-- CENTER: editor -->
  <main class="center">
    <div class="topbar">
      <button class="rail-btn" title={leftOpen ? 'Hide notes' : 'Show notes'} aria-label="Toggle notes panel" onclick={toggleLeft}>☰</button>

      <div class="titlebox">
        <input
          class="path-input"
          placeholder="untitled · type a title, use / for folders"
          bind:value={current.path}
          oninput={onPath}
          spellcheck="false"
          autocomplete="off"
        />
        <div class="crumbs" aria-hidden="true">
          {#if segs.length}
            {#each segs as s, i (i)}
              <span class="crumb" class:leaf={i === segs.length - 1}>{s}</span>
              {#if i < segs.length - 1}<span class="crumb-sep">›</span>{/if}
            {/each}
          {:else}
            <span class="crumb muted">untitled</span>
          {/if}
        </div>
      </div>

      <div class="tools">
        <div class="seg" role="group" aria-label="View mode">
          <button class="seg-btn" class:on={mode === 'edit'} onclick={() => (mode = 'edit')}>Edit</button>
          <button class="seg-btn" class:on={mode === 'preview'} onclick={() => (mode = 'preview')}>Preview</button>
        </div>
        <button
          class="mic-btn"
          class:live={dictState === 'listening'}
          class:connecting={dictState === 'connecting'}
          onclick={toggleMic}
          title={dictState === 'listening' ? 'Stop dictation' : 'Dictate'}
          aria-label={dictState === 'listening' ? 'Stop dictation' : 'Start dictation'}
        >
          <span class="mic-glyph">●</span>
          <span class="mic-label">{dictState === 'listening' ? 'Listening' : dictState === 'connecting' ? '…' : 'Dictate'}</span>
        </button>
        <button class="rail-btn" title={rightOpen ? 'Hide vocabulary' : 'Dictation vocabulary'} aria-label="Toggle vocabulary panel" class:on={rightOpen} onclick={toggleRight}>Aa</button>
      </div>
    </div>

    <div class="editor-wrap">
      {#if mode === 'edit'}
        <div class="edit-stack">
          <pre class="editor-layer editor-hl markdown-hl" bind:this={hlEl} aria-hidden="true">{@html highlighted}</pre>
          <textarea
            class="editor-layer editor"
            bind:this={taEl}
            bind:value={current.content}
            oninput={onBody}
            onscroll={syncScroll}
            placeholder="Start writing in Markdown…  ⌘ or press Dictate to speak."
            spellcheck="true"
          ></textarea>
        </div>
      {:else}
        <div class="preview markdown">
          {#if current.content.trim()}
            {@html rendered}
          {:else}
            <p class="preview-empty">Nothing to preview yet.</p>
          {/if}
        </div>
      {/if}

      {#if dictState === 'listening'}
        <div class="dictation-bar">
          <span class="pulse" style="--lvl: {micLevel}"></span>
          <span class="dictation-text">{interim || 'Listening… speak, then pause to commit a phrase.'}</span>
        </div>
      {/if}
    </div>

    <div class="statusbar">
      {#if current.file_path}
        <button class="filepath" onclick={copyPath} title="Copy file path">
          <span class="fp-icon">⧉</span>
          <code>{current.file_path}</code>
          {#if copied}<span class="copied">copied</span>{/if}
        </button>
      {:else}
        <span class="filepath muted">Not saved yet · a file is created when you type</span>
      {/if}
      <span class="status-right">
        {#if current.id && collabLive}
          <span class="live-pill" title="Live — changes sync between open tabs">
            <span class="live-dot"></span>Live
          </span>
        {/if}
        <span class="wc">{words} {words === 1 ? 'word' : 'words'}</span>
        <span class="save-state" data-state={saveState}>{saveLabel}</span>
      </span>
    </div>
  </main>

  <!-- RIGHT: dictation vocabulary -->
  <aside class="right" class:closed={!rightOpen}>
    <div class="panel-head">
      <span class="panel-title">Vocabulary</span>
      <button class="ghost-btn" title="Close" aria-label="Close vocabulary" onclick={() => { rightOpen = false; persistTab(); }}>×</button>
    </div>
    <div class="vocab-scroll">
      <VocabPanel
        terms={vocab}
        onAdd={async (t) => { vocab = (await notesApi.vocabAdd(t)).terms; dictation?.setHotwords(vocab); }}
        onRemove={async (t) => { vocab = (await notesApi.vocabRemove(t)).terms; dictation?.setHotwords(vocab); }}
      />
    </div>
  </aside>
</div>
