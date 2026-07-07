<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { notesApi, type Note, type NoteMeta, type Tree } from './lib/api';
  import { buildTree } from './lib/tree';
  import { renderMarkdown } from './lib/markdown';
  import { createDictation, type DictationState } from './lib/dictation';
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
  let saveState = $state<'new' | 'unsaved' | 'saving' | 'saved'>('new');

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
  const words = $derived(current.content.trim() ? current.content.trim().split(/\s+/).length : 0);

  let dictation: ReturnType<typeof createDictation> | null = null;
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  let searchTimer: ReturnType<typeof setTimeout> | null = null;

  // ---- persistence -------------------------------------------------------

  async function loadTree() { tree = await notesApi.tree(true); }
  async function loadVocab() {
    vocab = (await notesApi.vocabList()).terms;
    dictation?.setHotwords(vocab);
  }

  function scheduleSave() {
    saveState = current.id ? 'unsaved' : 'new';
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => void doSave(), 650);
  }

  async function doSave() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    const hasBody = current.content.trim().length > 0 || current.path.trim().length > 0;
    if (current.id === null) {
      if (!hasBody) return;            // nothing to persist yet — stay ephemeral
      saveState = 'saving';
      const n = await notesApi.create(current.path, current.content);
      // Only adopt the new id if the user hasn't since started a different note.
      current.id = n.id;
      current.file_path = n.file_path;
    } else {
      saveState = 'saving';
      await notesApi.save(current.id, { content: current.content, path: current.path });
    }
    saveState = 'saved';
    await loadTree();
  }

  async function flush() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; await doSave(); }
  }

  // ---- note switching ----------------------------------------------------

  async function openNote(id: string) {
    if (id === current.id) return;
    await flush();
    const n: Note = await notesApi.get(id);
    current = { id: n.id, path: n.path, content: n.content, file_path: n.file_path, deleted_at: n.deleted_at };
    mode = 'edit';
    results = null; query = '';
    saveState = 'saved';
    await focusEditor();
  }

  async function newNote() {
    await flush();
    current = blank();
    mode = 'edit';
    saveState = 'new';
    await focusEditor();
  }

  async function trashNote(id: string) {
    await notesApi.trash(id);
    if (id === current.id) current = blank();
    await loadTree();
  }
  async function restoreNote(id: string) { await notesApi.restore(id); await loadTree(); }
  async function purgeNote(id: string) {
    await notesApi.purge(id);
    if (id === current.id) current = blank();
    await loadTree();
  }

  function toggleFolder(path: string) {
    const next = new Set(collapsed);
    next.has(path) ? next.delete(path) : next.add(path);
    collapsed = next;
  }

  // ---- editing -----------------------------------------------------------

  function onBody() { scheduleSave(); }
  function onPath() { scheduleSave(); }

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
      onCommit: (t) => { void insertAtCaret(t); scheduleSave(); },
      onState: (s) => (dictState = s),
      onLevel: (v) => (micLevel = v),
    });
    try { await loadVocab(); } catch { /* service may still be booting */ }
    try { await loadTree(); } catch { /* ditto */ }
    current = blank();
    await focusEditor();
  });

  onDestroy(() => {
    dictation?.destroy();
    if (saveTimer) clearTimeout(saveTimer);
    if (searchTimer) clearTimeout(searchTimer);
  });

  const saveLabel = $derived(
    saveState === 'saving' ? 'Saving…'
    : saveState === 'unsaved' ? 'Unsaved'
    : saveState === 'saved' ? 'Saved'
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
      <button class="rail-btn" title={leftOpen ? 'Hide notes' : 'Show notes'} aria-label="Toggle notes panel" onclick={() => (leftOpen = !leftOpen)}>☰</button>

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
        <button class="rail-btn" title={rightOpen ? 'Hide vocabulary' : 'Dictation vocabulary'} aria-label="Toggle vocabulary panel" class:on={rightOpen} onclick={() => (rightOpen = !rightOpen)}>Aa</button>
      </div>
    </div>

    <div class="editor-wrap">
      {#if mode === 'edit'}
        <textarea
          class="editor"
          bind:this={taEl}
          bind:value={current.content}
          oninput={onBody}
          placeholder="Start writing in Markdown…  ⌘ or press Dictate to speak."
          spellcheck="true"
        ></textarea>
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
        <span class="wc">{words} {words === 1 ? 'word' : 'words'}</span>
        <span class="save-state" data-state={saveState}>{saveLabel}</span>
      </span>
    </div>
  </main>

  <!-- RIGHT: dictation vocabulary -->
  <aside class="right" class:closed={!rightOpen}>
    <div class="panel-head">
      <span class="panel-title">Vocabulary</span>
      <button class="ghost-btn" title="Close" aria-label="Close vocabulary" onclick={() => (rightOpen = false)}>×</button>
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
