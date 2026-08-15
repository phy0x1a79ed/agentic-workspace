<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { notesApi, type Note, type NoteMeta, type Tree } from './lib/api';
  import { buildTree } from './lib/tree';
  import { renderMarkdown } from './lib/markdown';
  import { createDictation, type DictationState } from './lib/dictation';
  import { createCollab, type LinkState, type SyncState } from './lib/collab';
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
  // Content state — what the SERVER has, versus what only this browser has.
  // 'new'        — blank note, nothing typed yet
  // 'savedLocal' — buffered to the local draft, server not yet confirmed
  // 'saving'     — an edit is in flight to the server
  // 'synced'     — the server has confirmed our latest text
  // 'offline'    — a send failed; edits are safe in the local draft
  let saveState = $state<'new' | 'savedLocal' | 'saving' | 'synced' | 'offline'>('new');
  // Transport state — kept separate from saveState on purpose. Collapsing the
  // two is what let the status bar read a confident "Synced" at a server that
  // had been unreachable for an hour: nothing was being sent, so nothing failed.
  let link = $state<LinkState>('idle');
  // A refused title rename. Its own flag rather than borrowing 'offline',
  // because a *content* sync succeeding must not erase the warning — nor the
  // local draft, which is the only place the un-persisted title lives.
  let pathFailed = $state(false);

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

  // Theme (light/dark). An explicit choice is persisted to localStorage and
  // applied to <html data-theme> (index.html applies it pre-paint to avoid a
  // flash); absent = follow the OS via the @media default. `theme` mirrors the
  // effective mode for the toggle button's icon.
  function initialTheme(): 'light' | 'dark' {
    try {
      const s = localStorage.getItem('notes_theme');
      if (s === 'light' || s === 'dark') return s;
    } catch { /* storage blocked */ }
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  let theme = $state<'light' | 'dark'>(initialTheme());
  function toggleTheme() {
    theme = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('notes_theme', theme); } catch { /* storage blocked */ }
  }

  // Suppressed while the window is being resized (see onWinResize): the panels'
  // desktop↔mobile layout swap must not animate, or a closed panel visibly
  // slides open→shut as the media query flips it from width-collapse to a
  // transform drawer. Cleared shortly after resizing settles.
  let vpResizing = $state(false);
  let collapsed = $state<Set<string>>(new Set(initial.collapsed));

  // Auto-hiding chrome (mobile only): on the phone layout the document itself
  // scrolls (see the ≤680px block in styles.css), so scrolling the note down
  // slides the top bar + footer away (and lets the native browser bar collapse);
  // scrolling up brings them back. Inert on desktop — the gate never sets it and
  // the CSS that consumes it lives in the mobile media query.
  let chromeHidden = $state(false);
  let mobileMq: MediaQueryList | null = null;
  let lastScrollY = 0;

  let query = $state('');
  let semantic = $state(false);
  let results = $state<NoteMeta[] | null>(null);

  let vocab = $state<string[]>([]);

  let dictState = $state<DictationState>('idle');
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
  let pathRetry: ReturnType<typeof setTimeout> | null = null;
  let creating = false;
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let vpTimer: ReturnType<typeof setTimeout> | null = null;

  /** Kill panel transitions for the duration of a window resize (plus a short
   *  settle) so the desktop↔mobile breakpoint swap snaps instead of animating. */
  function onWinResize() {
    vpResizing = true;
    if (vpTimer) clearTimeout(vpTimer);
    vpTimer = setTimeout(() => { vpResizing = false; }, 200);
    // Leaving the mobile layout must never strand the bars hidden.
    if (!mobileMq?.matches && chromeHidden) chromeHidden = false;
  }

  /** Direction-aware chrome hide/reveal, driven by the document scroll on mobile.
   *  Down past a dead-zone hides the bars; up (or near the top) reveals them. */
  function onScroll() {
    if (!mobileMq?.matches) { if (chromeHidden) chromeHidden = false; return; }
    const y = window.scrollY;
    if (y < 8) { chromeHidden = false; lastScrollY = y; return; }  // always show at top
    const dy = y - lastScrollY;
    if (Math.abs(dy) < 6) return;                                  // ignore jitter
    chromeHidden = dy > 0;
    lastScrollY = y;
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
      // Don't downgrade a failed create back to 'savedLocal' — it would strobe
      // the label between "Saving…" and "Saved locally" as you type at a dead
      // service. 'offline' is sticky until a create actually lands.
      if (!hasBody) saveState = 'new';
      else if (saveState !== 'offline') saveState = 'savedLocal';
      scheduleCreate(saveState === 'offline' ? 5000 : 500);
    } else {
      // Buffered locally; collab.onSync will advance this to saving → synced.
      saveState = 'savedLocal';
      collab?.update(current.content);
    }
  }

  function scheduleCreate(delay = 500) {
    if (createTimer) clearTimeout(createTimer);
    createTimer = setTimeout(() => void createNote(), delay);
  }

  async function createNote() {
    if (createTimer) { clearTimeout(createTimer); createTimer = null; }
    if (creating || current.id !== null) return;
    const hasBody = current.content.trim().length > 0 || current.path.trim().length > 0;
    if (!hasBody) return;               // nothing to persist yet — stay ephemeral
    creating = true;
    if (saveState !== 'offline') saveState = 'saving';   // a retry stays quiet
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
      if (!pathFailed) clearDraft(current.id);  // persisted + joined
      await loadTree();
    } catch (err) {
      // A note that doesn't exist yet has no room to join, so the link pill has
      // nothing to report — the save state is the only honest signal there is.
      // Without this the first note of a session, typed at a stopped service,
      // reads "Saving…" forever (and the throw aborted flush() mid-switch).
      console.error('notes: create failed', err);
      saveState = 'offline';    // the text is in the local draft
      createTimer = setTimeout(() => void createNote(), 5000);
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
    if (pathRetry) { clearTimeout(pathRetry); pathRetry = null; }
    if (current.id === null) return;
    const id = current.id;
    try {
      await notesApi.save(id, { path: current.path });
    } catch (err) {
      // A refused rename must not fail silently: the new title lives only in the
      // local draft until this lands, so say so in the status bar and keep
      // retrying (the note stays renamed on screen meanwhile).
      console.error('notes: title save failed', err);
      pathFailed = true;
      pathRetry = setTimeout(() => { if (current.id === id) void savePath(); }, 5000);
      return;
    }
    pathFailed = false;
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
    pathFailed = false;
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
    pathFailed = false;
    setOpen(null);
    await focusEditor();
  }

  async function trashNote(id: string) {
    await notesApi.trash(id);
    clearDraft(id);
    if (id === current.id) { collab?.leave(); current = blank(); editor?.setDoc(''); saveState = 'new'; pathFailed = false; setOpen(null); }
    await loadTree();
  }
  async function restoreNote(id: string) { await notesApi.restore(id); await loadTree(); }
  async function purgeNote(id: string) {
    await notesApi.purge(id);
    clearDraft(id);
    if (id === current.id) { collab?.leave(); current = blank(); editor?.setDoc(''); saveState = 'new'; pathFailed = false; setOpen(null); }
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
   *  sticking on a buffered state — a solo editor now reaches 'synced').
   *  `id` is checked because a slow send can land after the user switched
   *  notes, and clearing the draft would then drop the *other* note's copy. */
  function onCollabSync(s: SyncState, id: string) {
    if (id !== current.id) return;
    if (s === 'synced') {
      saveState = 'synced';
      // Not while a rename is still unsaved: the draft holds the new title.
      if (!pathFailed) clearDraft(id);
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
      onInterim: (t) => editor?.setInterim(t),
      onCommit: (t) => editor?.insertAtCaret(t),
      onState: (s) => (dictState = s),
      onLevel: (v) => (micLevel = v),
    });
    collab = createCollab({
      onText: (t) => applyCollabText(t),
      onLink: (s) => (link = s),
      onSync: (s, id) => onCollabSync(s, id),
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
    // Auto-hiding chrome: watch the document scroll (mobile layout only).
    mobileMq = window.matchMedia('(max-width: 680px)');
    lastScrollY = window.scrollY;
    window.addEventListener('scroll', onScroll, { passive: true });
    await focusEditor();
  });

  async function onHashChange() {
    const id = readHashId();
    if (id && id !== current.id && tree.active.some((n) => n.id === id)) {
      await openNote(id);
    }
  }

  onDestroy(() => {
    if (pathRetry) clearTimeout(pathRetry);
    dictation?.destroy();
    collab?.destroy();       // leaves the room AND drops the wake-up listeners
    editor?.destroy();
    spellcheck?.destroy();
    window.removeEventListener('hashchange', onHashChange);
    window.removeEventListener('resize', onWinResize);
    window.removeEventListener('scroll', onScroll);
    if (createTimer) clearTimeout(createTimer);
    if (pathTimer) clearTimeout(pathTimer);
    if (searchTimer) clearTimeout(searchTimer);
    if (vpTimer) clearTimeout(vpTimer);
  });

  // ---- status indicators -------------------------------------------------
  //
  // Two truths, two indicators. The pill reports the transport; the save state
  // reports where the text lives. Read together they answer "is my writing
  // safe?" — which neither can answer alone.

  const linkLabel = $derived(
    link === 'live' ? 'Live' : link === 'down' ? 'Disconnected' : 'Connecting…',
  );
  const linkTitle = $derived(
    link === 'live' ? 'Live — changes sync between open tabs'
    : link === 'down' ? 'No connection to the notes service — edits are kept in this browser'
    : 'Reconnecting to the notes service…',
  );

  // First match wins. Two deliberate calls in the ladder:
  //  • 'savedLocal' renders as "Saving…", not "Saved locally" — it is set on
  //    every keystroke and cleared ~180ms later, so the orange would strobe as
  //    you type. Reserving the phrase for genuinely-unsent work is what makes it
  //    mean something.
  //  • Disconnected-with-everything-sent stays "Synced", but muted rather than
  //    green: it is true, the red pill carries the connection fact, and the grey
  //    stops it reading as "all good".
  const save = $derived.by((): { label: string; tone: 'ok' | 'muted' | 'grey' | 'warn' } => {
    if (saveState === 'new') return { label: 'New note', tone: 'grey' };
    if (pathFailed) return { label: 'Title not saved', tone: 'warn' };
    const unsent = saveState === 'savedLocal' || saveState === 'saving';
    if (saveState === 'offline' || (link === 'down' && unsent)) {
      return { label: 'Saved locally', tone: 'warn' };
    }
    if (link === 'down') return { label: 'Synced', tone: 'grey' };
    if (unsent) return { label: 'Saving…', tone: 'muted' };
    return { label: 'Synced', tone: 'ok' };
  });
</script>

<div class="app" class:ready={panelsReady} class:resizing={resizing !== null}
     class:vp-resizing={vpResizing}
     class:chrome-hidden={chromeHidden}
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
          <span class="mic-glyph" style="--lvl: {dictState === 'listening' ? micLevel : 0}" aria-hidden="true"></span>
          <span class="mic-label">{dictState === 'listening' ? 'Listening' : dictState === 'connecting' ? '…' : 'Dictate'}</span>
        </button>
        <button class="rail-btn" title={rightOpen ? 'Hide vocabulary' : 'Dictation vocabulary'} aria-label="Toggle vocabulary panel" class:on={rightOpen} onclick={toggleRight}>Aa</button>
        <button
          class="rail-btn theme-btn"
          title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          aria-label="Toggle color theme"
          onclick={toggleTheme}
        >{theme === 'dark' ? '☀' : '☾'}</button>
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
        <!-- `link !== 'idle'` is not redundant with `current.id`: between a
             leave and the next join, current.id still names the OLD note. -->
        {#if current.id && link !== 'idle'}
          <span class="link-pill" data-link={link} title={linkTitle}>
            <span class="link-dot"></span>{linkLabel}
          </span>
        {/if}
        <span class="wc">{words} {words === 1 ? 'word' : 'words'}</span>
        <span class="save-state" data-tone={save.tone}>{save.label}</span>
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
