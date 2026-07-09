<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { notesApi, type Note, type NoteMeta, type Tree } from './lib/api';
  import { buildTree } from './lib/tree';
  import { renderMarkdown } from './lib/markdown';
  import { createDictation, type DictationState } from './lib/dictation';
  import { createCollab } from './lib/collab';
  import { createNoteEditor, type NoteEditor } from './lib/editor';
  import { createSpellchecker, type SpellController } from './lib/spellcheck';
  import {
    readState, writeState, readHashId, writeHashId, type TabState,
    LEFT_MIN, LEFT_MAX, RIGHT_MIN, RIGHT_MAX,
  } from './lib/persist';
  import { writeDraft, readDraft, clearDraft, migrateBlankDraft } from './lib/draft';
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
  // 'new'        — blank note, nothing typed yet
  // 'editing'    — transient, mid-keystroke
  // 'savedLocal' — buffered to the local draft, server not yet confirmed
  // 'saving'     — an edit is in flight to the server
  // 'synced'     — the server has confirmed our latest text
  // 'offline'    — a send failed; edits are safe in the local draft
  let saveState = $state<'new' | 'editing' | 'savedLocal' | 'saving' | 'synced' | 'offline'>('new');
  let collabLive = $state(false);

  // Panel open-state + widths are read synchronously from the cookie at init
  // (see `initial` below) so panels mount in their final state — no reload flash
  // of the left panel opening then closing. Width transitions are suppressed
  // until `panelsReady` flips after first paint (see onMount).
  const initial = readState();
  let leftOpen = $state(initial.left);
  let rightOpen = $state(initial.right);
  let leftW = $state(initial.leftW);
  let rightW = $state(initial.rightW);
  let panelsReady = $state(false);
  // Suppressed while the window is being resized (see onWinResize): the panels'
  // desktop↔mobile layout swap must not animate, or a closed panel visibly
  // slides open→shut as the media query flips it from width-collapse to a
  // transform drawer. Cleared shortly after resizing settles.
  let vpResizing = $state(false);
  let collapsed = $state<Set<string>>(new Set(initial.collapsed));

  let query = $state('');
  let semantic = $state(false);
  let results = $state<NoteMeta[] | null>(null);

  let vocab = $state<string[]>([]);

  let dictState = $state<DictationState>('idle');
  let interim = $state('');
  let micLevel = $state(0);

  let editorHost = $state<HTMLDivElement | null>(null);
  let copied = $state(false);

  const treeNodes = $derived(buildTree(tree.active));
  const segs = $derived(current.path.split('/').map((s) => s.trim()).filter(Boolean));
  const rendered = $derived(renderMarkdown(current.content));
  const words = $derived(current.content.trim() ? current.content.trim().split(/\s+/).length : 0);

  let dictation: ReturnType<typeof createDictation> | null = null;
  let collab: ReturnType<typeof createCollab> | null = null;
  let editor: NoteEditor | null = null;
  let spellcheck: SpellController | null = null;
  let createTimer: ReturnType<typeof setTimeout> | null = null;
  let pathTimer: ReturnType<typeof setTimeout> | null = null;
  let creating = false;
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let vpTimer: ReturnType<typeof setTimeout> | null = null;

  /** Kill panel transitions for the duration of a window resize (plus a short
   *  settle) so the desktop↔mobile breakpoint swap snaps instead of animating. */
  function onWinResize() {
    vpResizing = true;
    if (vpTimer) clearTimeout(vpTimer);
    vpTimer = setTimeout(() => { vpResizing = false; }, 200);
  }

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
    spellcheck?.setVocab(vocab);
  }

  /** A local content edit (typing or dictation). Routes to create-then-join for
   *  a fresh note, or streams into the live room for an existing one. Always
   *  mirrors the text to the local draft first, so nothing is lost if the
   *  connection breaks before the edit is confirmed synced. */
  function onLocalEdit() {
    writeDraft(current.id, current.path, current.content);
    const hasBody = current.content.trim().length > 0 || current.path.trim().length > 0;
    if (current.id === null) {
      saveState = hasBody ? 'savedLocal' : 'new';
      scheduleCreate();
    } else {
      // Buffered locally; collab.onSync will advance this to saving → synced.
      saveState = 'savedLocal';
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
      migrateBlankDraft(n.id);          // carry the local safety copy onto the real id
      current.id = n.id;
      current.file_path = n.file_path;
      setOpen(n.id);
      await collab?.join(n.id, current.content);
      // Reconcile the title: edits made while create was in flight (id still
      // null) can't route through savePath, so persist the latest path now.
      if (current.path !== n.path) await notesApi.save(n.id, { path: current.path });
      saveState = 'synced';
      clearDraft(current.id);   // persisted + joined — the safety copy has served
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
    return {
      last: current.id, collapsed: [...collapsed],
      left: leftOpen, right: rightOpen, leftW, rightW,
    };
  }
  function persistTab() { writeState(tabState()); }
  function setOpen(id: string | null) { writeHashId(id); persistTab(); }

  // ---- note switching ----------------------------------------------------

  async function openNote(id: string) {
    if (id === current.id) return;
    await flush();
    collab?.leave();
    const n: Note = await notesApi.get(id);
    // If a local draft survived a dropped connection / reload with edits the
    // server never confirmed, restore it; collab.join pushes the diff back up.
    const d = readDraft(id);
    const useDraft = !!d && d.content !== n.content;
    const content = useDraft ? d!.content : n.content;
    current = { id: n.id, path: n.path, content, file_path: n.file_path, deleted_at: n.deleted_at };
    editor?.setDoc(content);   // load into the editor (remote → no local emit)
    mode = 'edit';
    results = null; query = '';
    if (useDraft) { saveState = 'savedLocal'; } else { saveState = 'synced'; clearDraft(id); }
    setOpen(n.id);
    await collab?.join(n.id, content);
    await focusEditor();
  }

  async function newNote() {
    await flush();
    collab?.leave();
    clearDraft(null);            // start a genuinely fresh blank note
    current = blank();
    editor?.setDoc('');
    mode = 'edit';
    saveState = 'new';
    setOpen(null);
    await focusEditor();
  }

  async function trashNote(id: string) {
    await notesApi.trash(id);
    clearDraft(id);
    if (id === current.id) { collab?.leave(); current = blank(); editor?.setDoc(''); saveState = 'new'; setOpen(null); }
    await loadTree();
  }
  async function restoreNote(id: string) { await notesApi.restore(id); await loadTree(); }
  async function purgeNote(id: string) {
    await notesApi.purge(id);
    clearDraft(id);
    if (id === current.id) { collab?.leave(); current = blank(); editor?.setDoc(''); saveState = 'new'; setOpen(null); }
    await loadTree();
  }

  function toggleFolder(path: string) {
    const next = new Set(collapsed);
    next.has(path) ? next.delete(path) : next.add(path);
    collapsed = next;
    persistTab();
  }

  function toggleLeft() { leftOpen = !leftOpen; persistTab(); afterPanelResize(); }
  function toggleRight() { rightOpen = !rightOpen; persistTab(); afterPanelResize(); }
  function closePanels() { leftOpen = false; rightOpen = false; persistTab(); afterPanelResize(); }

  /** The center pane changed size — let CodeMirror re-measure its viewport. */
  function afterPanelResize() { void tick().then(() => editor?.remeasure()); }

  // ---- panel resize (drag the inner edge) --------------------------------

  let resizing = $state<null | 'left' | 'right'>(null);

  function startResize(side: 'left' | 'right', ev: PointerEvent) {
    ev.preventDefault();
    resizing = side;
    const startX = ev.clientX;
    const startW = side === 'left' ? leftW : rightW;
    const move = (e: PointerEvent) => {
      const dx = e.clientX - startX;
      if (side === 'left') {
        leftW = Math.min(LEFT_MAX, Math.max(LEFT_MIN, startW + dx));
      } else {
        rightW = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, startW - dx)); // grows leftward
      }
    };
    const up = () => {
      resizing = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      persistTab();
      afterPanelResize();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  // ---- editing -----------------------------------------------------------

  function onPath() {
    writeDraft(current.id, current.path, current.content);   // mirror the title too
    schedulePathSave();
  }

  /** A doc change reported BY the editor. Keep app state in step; only a genuine
   *  user edit (`local`) persists/streams — a collab merge or note-load doesn't. */
  function onEditorChange(text: string, local: boolean) {
    current.content = text;
    if (local) onLocalEdit();
  }

  /** Adopt authoritative text (a merge or a peer's edit) into the editor as a
   *  minimal diff; CM maps the caret through it. */
  function applyCollabText(next: string) {
    editor?.applyText(next);
    current.content = next;
    // A merge from the server is authoritative — refresh the local safety copy.
    writeDraft(current.id, current.path, next);
  }

  /** Sync-state transitions reported by collab.ts (the fix for the status
   *  sticking on a buffered state — a solo editor now reaches 'synced'). */
  function onCollabSync(s: 'saving' | 'synced' | 'offline') {
    if (s === 'synced') {
      saveState = 'synced';
      clearDraft(current.id);       // server has it now — drop the safety copy
    } else if (s === 'saving') {
      saveState = 'saving';
    } else {
      saveState = 'offline';
    }
  }

  async function focusEditor() {
    await tick();
    editor?.focus();
  }

  /** Edit/Preview toggle. Re-measure CM when it's shown again after being hidden. */
  function setMode(m: 'edit' | 'preview') {
    mode = m;
    if (m === 'edit') void tick().then(() => editor?.remeasure());
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
    setMode('edit');
    dictation.setHotwords(vocab);
    await focusEditor();
    await dictation.start();
  }

  // ---- lifecycle ---------------------------------------------------------

  onMount(async () => {
    spellcheck = createSpellchecker({
      onAddWord: async (w) => {
        vocab = (await notesApi.vocabAdd(w)).terms;
        dictation?.setHotwords(vocab);
        spellcheck?.setVocab(vocab);
      },
    });
    if (editorHost) {
      editor = createNoteEditor(editorHost, {
        doc: current.content,
        placeholder: 'Start writing in Markdown…  ⌘ or press Dictate to speak.',
        onChange: onEditorChange,
        extensions: spellcheck ? [spellcheck.extension] : [],
      });
    }
    dictation = createDictation({
      onInterim: (t) => (interim = t),
      onCommit: (t) => editor?.insertAtCaret(t),
      onState: (s) => (dictState = s),
      onLevel: (v) => (micLevel = v),
    });
    collab = createCollab({
      onText: (t) => applyCollabText(t),
      onStatus: (live) => (collabLive = live),
      onSync: (s) => onCollabSync(s),
    });

    // Panel open-state/widths were already read synchronously at init (no reload
    // flash). Enable width transitions only now, after the first paint.
    await tick();
    panelsReady = true;

    try { await loadVocab(); } catch { /* service may still be booting */ }
    try { await loadTree(); } catch { /* ditto */ }

    // Resume: URL hash wins (deep link), else the last-open note from the cookie.
    const wantId = readHashId() ?? initial.last;
    const known = wantId && tree.active.some((n) => n.id === wantId);
    if (known) {
      try { await openNote(wantId!); } catch { current = blank(); }
    } else {
      // No note to resume — but a blank-note draft may hold un-created work from
      // a crash/reload; recover it so nothing typed is silently lost.
      const bd = readDraft(null);
      if (bd && (bd.content.trim() || bd.path.trim())) {
        current = { ...blank(), content: bd.content, path: bd.path };
        editor?.setDoc(bd.content);
        saveState = 'savedLocal';
        setOpen(null);
        scheduleCreate();
      } else {
        current = blank();
        setOpen(null);
      }
    }
    // React to hash edits (back/forward, pasted link) while the page is open.
    window.addEventListener('hashchange', onHashChange);
    window.addEventListener('resize', onWinResize);
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
    editor?.destroy();
    spellcheck?.destroy();
    window.removeEventListener('hashchange', onHashChange);
    window.removeEventListener('resize', onWinResize);
    if (createTimer) clearTimeout(createTimer);
    if (pathTimer) clearTimeout(pathTimer);
    if (searchTimer) clearTimeout(searchTimer);
    if (vpTimer) clearTimeout(vpTimer);
  });

  const saveLabel = $derived(
    saveState === 'saving' ? 'Saving…'
    : saveState === 'synced' ? 'Synced'
    : saveState === 'savedLocal' ? 'Saved locally'
    : saveState === 'offline' ? 'Offline · saved locally'
    : saveState === 'editing' ? 'Editing…'
    : 'New note',
  );
</script>

<div class="app" class:ready={panelsReady} class:resizing={resizing !== null}
     class:vp-resizing={vpResizing}
     class:panel-open={leftOpen || rightOpen}>
  <!-- Mobile-only backdrop: tap to dismiss an open drawer -->
  <button
    class="scrim"
    class:show={leftOpen || rightOpen}
    aria-label="Close panels"
    tabindex="-1"
    onclick={closePanels}
  ></button>

  <!-- LEFT: notes tree + trash -->
  <aside class="left" class:closed={!leftOpen} style:width={leftOpen ? leftW + 'px' : null}>
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
    <button
      type="button"
      class="chip-toggle"
      class:on={semantic}
      role="switch"
      aria-checked={semantic}
      title="Semantic search — match by meaning, not just keywords"
      onclick={() => { semantic = !semantic; onQuery(); }}
    >
      <span class="chip-dot" aria-hidden="true"></span>
      semantic
    </button>

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

  {#if leftOpen}
    <div
      class="resizer resizer-left"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize notes panel"
      onpointerdown={(e) => startResize('left', e)}
    ></div>
  {/if}

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
          <button class="seg-btn" class:on={mode === 'edit'} onclick={() => setMode('edit')}>Edit</button>
          <button class="seg-btn" class:on={mode === 'preview'} onclick={() => setMode('preview')}>Preview</button>
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
      <div class="cm-host" bind:this={editorHost} style:display={mode === 'edit' ? '' : 'none'}></div>
      {#if mode === 'preview'}
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

  {#if rightOpen}
    <div
      class="resizer resizer-right"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize vocabulary panel"
      onpointerdown={(e) => startResize('right', e)}
    ></div>
  {/if}

  <!-- RIGHT: dictation vocabulary -->
  <aside class="right" class:closed={!rightOpen} style:width={rightOpen ? rightW + 'px' : null}>
    <div class="panel-head">
      <span class="panel-title">Vocabulary</span>
      <button class="ghost-btn" title="Close" aria-label="Close vocabulary" onclick={() => { rightOpen = false; persistTab(); afterPanelResize(); }}>×</button>
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
