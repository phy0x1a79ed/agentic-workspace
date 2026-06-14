<script lang="ts">
  import { onMount } from 'svelte';
  import { Gallery, PanelLabel, Input } from '@awm/primitives';
  import {
    SttComposer,
    SttComposerShell,
    SttButton,
    TextTab,
    VoiceTab,
    VoiceChip,
  } from '@awm/stt-composer';
  import { TtsHistory, type Post } from '@awm/tts-history';
  import { AgentChat } from '@awm/agent-chat';

  // ── Theme / token surface ──────────────────────────────────────────────
  //
  // The single source of truth for shipped values is the primitives package's
  // tokens.css `:root` block. We never re-hardcode the hexes here — we keep one
  // list of token *names* (grouped) and read the live values from the cascade
  // with getComputedStyle. Editing rewrites the `--var` on `:root`, so every
  // component on the page (primitives + composites) re-themes with no reload.

  type TokenKind = 'color' | 'space' | 'radius' | 'font' | 'ease';
  interface TokenGroup {
    label: string;
    kind: TokenKind;
    names: string[];
  }

  const GROUPS: TokenGroup[] = [
    { label: 'surfaces', kind: 'color', names: ['--bg', '--surface', '--surface2', '--surface3'] },
    { label: 'borders', kind: 'color', names: ['--border', '--border2'] },
    { label: 'text', kind: 'color', names: ['--text', '--text2', '--text3'] },
    { label: 'accents', kind: 'color', names: ['--atomizer', '--recording', '--mgr'] },
    { label: 'semantic', kind: 'color', names: ['--ok', '--warn', '--danger'] },
    {
      label: 'spacing',
      kind: 'space',
      names: ['--space-0', '--space-1', '--space-2', '--space-3', '--space-4', '--space-5', '--space-6'],
    },
    { label: 'radii', kind: 'radius', names: ['--radius-sm', '--radius-md', '--radius-lg'] },
    { label: 'fonts', kind: 'font', names: ['--mono', '--sans'] },
    { label: 'easing', kind: 'ease', names: ['--ease-mech'] },
  ];

  const ALL_NAMES = GROUPS.flatMap((g) => g.names);

  // `shipped` is the tokens.css baseline snapshotted once on mount (before any
  // inline override); `values` is the live, edited copy that drives the inputs
  // and the value readouts.
  let shipped: Record<string, string> = {};
  let values = $state<Record<string, string>>({});

  function readCascade(): Record<string, string> {
    const cs = getComputedStyle(document.documentElement);
    const out: Record<string, string> = {};
    for (const name of ALL_NAMES) {
      // getPropertyValue returns the declared token text with a leading space.
      out[name] = cs.getPropertyValue(name).trim();
    }
    return out;
  }

  onMount(() => {
    shipped = readCascade();
    values = { ...shipped };
    // Outline boxes are positioned from live getBoundingClientRect reads, so a
    // viewport resize can invalidate them — recompute any active outline.
    const onResize = () => recomputeOutlines();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  });

  function setToken(name: string, v: string) {
    values[name] = v;
    document.documentElement.style.setProperty(name, v);
  }

  function resetTokens() {
    for (const name of ALL_NAMES) {
      // Drop the inline override so the value falls back to tokens.css.
      document.documentElement.style.removeProperty(name);
    }
    // Re-seed the controls from the baseline snapshot.
    values = { ...shipped };
  }

  // ── Composites: mock data (all offline, no live service) ────────────────

  const composerChips = ['set the build to release mode', 'then run the full test suite'];

  const shellChips = ['shell utterance one', 'shell utterance two'];

  const voiceTabChips = ['dictated line A', 'dictated line B', 'dictated line C'];

  // Post[] shaped per @awm/tts-history's types.ts — text + tool + membership
  // kinds so the grouping logic (solo / membership / tool cluster) all show.
  const mockPosts: Post[] = [
    { id: '1', ts: '2026-06-13T09:00:01Z', author: 'user:tony', body: 'Kick off the deploy when ready.', kind: 'text' },
    { id: '2', ts: '2026-06-13T09:00:04Z', author: 'agent:planner', body: 'On it — reading the current state first.', kind: 'text' },
    { id: '3', ts: '2026-06-13T09:00:05Z', author: 'agent:worker', body: '', kind: 'join' },
    { id: '4', ts: '2026-06-13T09:00:05Z', author: 'subscriber:web-ui', body: '', kind: 'join' },
    { id: '5', ts: '2026-06-13T09:00:07Z', author: 'agent:worker', body: 'read_file(path="awm/gateway/install.sh")', kind: 'tool_use' },
    { id: '6', ts: '2026-06-13T09:00:08Z', author: 'agent:worker', body: '→ 142 lines', kind: 'tool_result' },
    { id: '7', ts: '2026-06-13T09:00:09Z', author: 'agent:worker', body: 'run(cmd="awm services list")', kind: 'tool_use' },
    { id: '8', ts: '2026-06-13T09:00:11Z', author: 'agent:worker', body: '→ 6 services, all ready', kind: 'tool_result' },
    { id: '9', ts: '2026-06-13T09:00:13Z', author: 'agent:worker', body: 'Deploy looks safe. Promoting feat → dev → release now.', kind: 'text' },
    { id: '10', ts: '2026-06-13T09:01:02Z', author: 'subscriber:web-ui', body: '', kind: 'leave' },
  ];

  function noopSpeak(_post: Post) {
    // Surface the speak affordance without a TTS backend.
  }

  // ── Components explorer: composition forest ─────────────────────────────
  //
  // There is no runtime import scan in the browser, so the dependency forest is
  // hand-authored declared data. Subtrees are shared by object reference so a
  // component's children are identical wherever it appears (the same SttComposer
  // subtree hangs under AgentChat and as its own root). Node identity for the
  // tree is the *path* (so duplicates highlight independently); component
  // identity is the *name* (so the live-card set dedupes across duplicates).

  interface CNode { name: string; children: CNode[]; }
  const cnode = (name: string, children: CNode[] = []): CNode => ({ name, children });

  const voiceChip = cnode('VoiceChip');
  const voiceTab = cnode('VoiceTab', [voiceChip]);
  const textTab = cnode('TextTab');
  const sttShell = cnode('SttComposerShell', [textTab, voiceTab]);
  const sttButton = cnode('SttButton');
  const sttComposer = cnode('SttComposer', [sttShell, sttButton]);
  const ttsHistory = cnode('TtsHistory');
  const agentChat = cnode('AgentChat', [sttComposer, ttsHistory]);
  const FOREST: CNode[] = [agentChat, sttComposer, ttsHistory];

  // Primitives are standalone — nothing composite renders them — so they form a
  // flat leaf group to satisfy "all components". Their live demos live in the
  // Primitives section, so a click scrolls there rather than re-rendering.
  const PRIMITIVES = [
    'Button', 'Card', 'CollapsibleSection', 'Input', 'PanelLabel',
    'Pill', 'Select', 'Slider', 'Tag', 'Tooltip',
  ];

  // Shallowest tree-depth per component name (root = 1).
  function computeMinDepths(forest: CNode[]): Map<string, number> {
    const m = new Map<string, number>();
    const walk = (node: CNode, d: number) => {
      const prev = m.get(node.name);
      if (prev === undefined || d < prev) m.set(node.name, d);
      for (const c of node.children) walk(c, d + 1);
    };
    for (const r of forest) walk(r, 1);
    return m;
  }
  const MIN_DEPTH = computeMinDepths(FOREST);
  const MAX_DEPTH = Math.max(...MIN_DEPTH.values());

  // The canonical (shallowest) node per name drives each live card's
  // subcomponent tree. Children are identical across occurrences, so any wins.
  function canonicalNodes(forest: CNode[]): Map<string, CNode> {
    const best = new Map<string, { d: number; node: CNode }>();
    const walk = (node: CNode, d: number) => {
      const prev = best.get(node.name);
      if (prev === undefined || d < prev.d) best.set(node.name, { d, node });
      for (const c of node.children) walk(c, d + 1);
    };
    for (const r of forest) walk(r, 1);
    const out = new Map<string, CNode>();
    for (const [k, v] of best) out.set(k, v.node);
    return out;
  }
  const NODE_BY_NAME = canonicalNodes(FOREST);

  let depth = $state(1);
  let refreshKey = $state(0);

  // Distinct component names whose shallowest depth is within the selector,
  // ordered shallowest-first. min-depth ≤ N is monotonic: raising N only adds
  // cards, and N = MAX_DEPTH renders every component exactly once.
  const visibleComponents = $derived(
    [...MIN_DEPTH.entries()]
      .filter(([, d]) => d <= depth)
      .sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]))
      .map(([name, d]) => ({ name, depth: d })),
  );

  // ── Hover-highlight (path-based: a node's literal descendant subtree) ─────
  let hoveredPath = $state<string | null>(null);
  function isDescendant(path: string): boolean {
    return hoveredPath !== null && path !== hoveredPath && path.startsWith(hoveredPath + ' ');
  }

  function scrollToComponent(name: string) {
    if (PRIMITIVES.includes(name)) {
      document.getElementById('primitives')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    // Reveal a deeper composite by raising depth, then scroll to its card.
    const md = MIN_DEPTH.get(name);
    if (md && md > depth) setDepth(md);
    requestAnimationFrame(() =>
      document.getElementById(`c-${name}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    );
  }

  // ── Debug outline (per live card) ─────────────────────────────────────────
  //
  // hosts[name] is the live card's relative container. Clicking a subcomponent
  // name reads each [data-awm-component="<name>"] node's rect relative to the
  // host and renders absolutely-positioned boxes in the overlay layer.
  interface Rect { top: number; left: number; width: number; height: number; }
  const hosts: Record<string, HTMLElement | undefined> = {};
  let outlineTarget = $state<Record<string, string | null>>({});
  let outlineRects = $state<Record<string, Rect[]>>({});

  function computeRects(card: string, target: string): Rect[] {
    const host = hosts[card];
    if (!host) return [];
    const base = host.getBoundingClientRect();
    return [...host.querySelectorAll(`[data-awm-component="${target}"]`)].map((el) => {
      const r = (el as HTMLElement).getBoundingClientRect();
      return { top: r.top - base.top, left: r.left - base.left, width: r.width, height: r.height };
    });
  }

  function toggleOutline(card: string, target: string) {
    if (outlineTarget[card] === target) {
      outlineTarget[card] = null;
      outlineRects[card] = [];
    } else {
      outlineTarget[card] = target;
      outlineRects[card] = computeRects(card, target);
    }
  }

  function recomputeOutlines() {
    for (const card of Object.keys(outlineTarget)) {
      const t = outlineTarget[card];
      if (t) outlineRects[card] = computeRects(card, t);
    }
  }

  // A re-mount (Refresh / depth change) replaces the DOM the rects were read
  // from, so any active outline is stale — drop it.
  function clearOutlines() {
    outlineTarget = {};
    outlineRects = {};
  }

  function setDepth(v: number) {
    depth = Math.max(1, Math.min(MAX_DEPTH, v));
    clearOutlines();
  }
  function refresh() {
    refreshKey++;
    clearOutlines();
  }
</script>

<!-- Recursive dependency-tree node (path = identity; depth-gated expansion). -->
{#snippet treeNode(node: CNode, d: number, path: string)}
  <li>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="tnode mono"
      class:descendant={isDescendant(path)}
      class:hovered={hoveredPath === path}
      role="treeitem"
      tabindex="-1"
      aria-selected={hoveredPath === path}
      onmouseenter={() => (hoveredPath = path)}
      onmouseleave={() => { if (hoveredPath === path) hoveredPath = null; }}
    >
      <button type="button" class="tname" onclick={() => scrollToComponent(node.name)}>{node.name}</button>
      <span class="tdepth">d{d}</span>
      {#if node.children.length && d >= depth}
        <span class="tmore" title="raise depth to expand">…</span>
      {/if}
    </div>
    {#if node.children.length && d < depth}
      <ul class="tchildren">
        {#each node.children as c, i (i)}
          {@render treeNode(c, d + 1, path + ' ' + i)}
        {/each}
      </ul>
    {/if}
  </li>
{/snippet}

<!-- Per-card subcomponent tree (by name). Click outlines matching DOM nodes. -->
{#snippet subTree(card: string, node: CNode)}
  <ul class="tchildren">
    {#each node.children as c, i (i)}
      <li>
        <button
          type="button"
          class="tname sub"
          class:active={outlineTarget[card] === c.name}
          onclick={() => toggleOutline(card, c.name)}
        >{c.name}</button>
        {#if c.children.length}
          {@render subTree(card, c)}
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

<!-- Live demo per component, reusing the existing offline mock props. -->
{#snippet liveDemo(name: string)}
  {#if name === 'AgentChat'}
    <div class="agent-frame"><AgentChat autoConnect={false} offline /></div>
  {:else if name === 'SttComposer'}
    <SttComposer mockInitialChips={composerChips} />
  {:else if name === 'SttComposerShell'}
    <SttComposerShell initialChips={shellChips}>
      {#snippet voiceControls()}
        <button type="button" class="demo-ctl mono">PTT</button>
      {/snippet}
      {#snippet voiceMeter()}
        <div class="demo-meter"><div class="demo-meter-bar"></div></div>
      {/snippet}
    </SttComposerShell>
  {:else if name === 'SttButton'}
    <div class="stage narrow"><SttButton onpttdown={() => {}} onpttup={() => {}} /></div>
  {:else if name === 'TextTab'}
    <TextTab />
  {:else if name === 'VoiceTab'}
    <VoiceTab initialChips={voiceTabChips} />
  {:else if name === 'VoiceChip'}
    <div class="stage col chips">
      <VoiceChip text="a settled voice chip" />
      <VoiceChip text="a live, streaming chip" live />
    </div>
  {:else if name === 'TtsHistory'}
    <div class="tts-frame"><TtsHistory posts={mockPosts} onspeak={noopSpeak} /></div>
  {/if}
{/snippet}

<div class="layout">
  <nav class="toc mono">
    <div class="toc-head">
      <PanelLabel tone="atomizer">components</PanelLabel>
      <span class="toc-sub">gallery</span>
    </div>
    <ul class="toc-list">
      <li><a href="#theme">Theme</a></li>
      <li><a href="#primitives">Primitives</a></li>
      <li>
        <a href="#components">Components</a>
        <ul class="toc-sublist">
          {#each visibleComponents as c (c.name)}
            <li><a href="#c-{c.name}">{c.name}</a></li>
          {/each}
        </ul>
      </li>
    </ul>
  </nav>

  <div class="scroll">
    <!-- ── THEME ──────────────────────────────────────────────────────── -->
    <section id="theme" class="section">
      <header class="section-head">
        <h1 class="section-title mono">Theme</h1>
        <button type="button" class="reset mono" onclick={resetTokens}>Reset</button>
      </header>
      <p class="note mono">
        Edits are live but <strong>in-memory only</strong> — they rewrite each
        <code>--var</code> on <code>:root</code> so the whole page re-themes, but
        nothing is written back to <code>tokens.css</code>. Copy a refined palette
        out by hand.
      </p>

      {#each GROUPS as g}
        <article class="card">
          <h2 class="name mono">{g.label}</h2>

          {#if g.kind === 'color'}
            <div class="swatch-grid">
              {#each g.names as name}
                <label class="swatch-row">
                  <span class="swatch-box" style:background={`var(${name})`}></span>
                  <span class="tok-name mono">{name}</span>
                  <span class="tok-val mono">{values[name] ?? ''}</span>
                  <input
                    type="color"
                    class="color-input"
                    value={values[name] ?? '#000000'}
                    oninput={(e) => setToken(name, e.currentTarget.value)}
                  />
                </label>
              {/each}
            </div>

          {:else if g.kind === 'space'}
            <div class="scale-grid">
              {#each g.names as name}
                <div class="scale-row">
                  <span class="bar" style:width={`var(${name})`}></span>
                  <span class="tok-name mono">{name}</span>
                  <input
                    class="text-input mono"
                    type="text"
                    value={values[name] ?? ''}
                    oninput={(e) => setToken(name, e.currentTarget.value)}
                  />
                </div>
              {/each}
            </div>

          {:else if g.kind === 'radius'}
            <div class="scale-grid">
              {#each g.names as name}
                <div class="scale-row">
                  <span class="radius-box" style:border-radius={`var(${name})`}></span>
                  <span class="tok-name mono">{name}</span>
                  <input
                    class="text-input mono"
                    type="text"
                    value={values[name] ?? ''}
                    oninput={(e) => setToken(name, e.currentTarget.value)}
                  />
                </div>
              {/each}
            </div>

          {:else if g.kind === 'font'}
            <div class="scale-grid">
              {#each g.names as name}
                <div class="scale-row font-row">
                  <span class="specimen" style:font-family={`var(${name})`}>
                    The quick brown fox · 0123456789
                  </span>
                  <span class="tok-name mono">{name}</span>
                  <input
                    class="text-input mono wide"
                    type="text"
                    value={values[name] ?? ''}
                    oninput={(e) => setToken(name, e.currentTarget.value)}
                  />
                </div>
              {/each}
            </div>

          {:else}
            <!-- easing -->
            <div class="scale-grid">
              {#each g.names as name}
                <div class="scale-row">
                  <span class="tok-name mono">{name}</span>
                  <input
                    class="text-input mono wide"
                    type="text"
                    value={values[name] ?? ''}
                    oninput={(e) => setToken(name, e.currentTarget.value)}
                  />
                </div>
              {/each}
            </div>
          {/if}
        </article>
      {/each}
    </section>

    <!-- ── PRIMITIVES ─────────────────────────────────────────────────── -->
    <section id="primitives" class="section">
      <header class="section-head">
        <h1 class="section-title mono">Primitives</h1>
      </header>
      <Gallery flat />
    </section>

    <!-- ── COMPONENTS EXPLORER ────────────────────────────────────────── -->
    <section id="components" class="section">
      <header class="section-head">
        <h1 class="section-title mono">Components</h1>
        <div class="depth-ctl mono">
          <label for="depth-sel">depth</label>
          <select
            id="depth-sel"
            class="depth-sel"
            value={depth}
            onchange={(e) => setDepth(Number(e.currentTarget.value))}
          >
            {#each Array.from({ length: MAX_DEPTH }) as _, i}
              <option value={i + 1}>{i + 1}</option>
            {/each}
          </select>
          <span class="depth-max">/ {MAX_DEPTH}</span>
          <button type="button" class="reset mono" onclick={refresh}>Refresh</button>
        </div>
      </header>
      <p class="note mono">
        The dependency tree of every component. <strong>Depth</strong> expands the
        tree and renders one live card per component whose shallowest tree-depth is
        within it (deduped to that shallowest level — a reused component shows
        once). Hover a tree node to highlight its descendant subtree; inside a live
        card, click a subcomponent name to outline its real DOM nodes.
      </p>

      <div class="explorer">
        <!-- dependency tree -->
        <aside class="tree-pane">
          <h2 class="name mono">dependency tree</h2>
          <ul class="tree" role="tree">
            {#each FOREST as root, i (i)}
              {@render treeNode(root, 1, String(i))}
            {/each}
          </ul>
          <h2 class="name mono prims-head">primitives</h2>
          <ul class="tree" role="tree">
            {#each PRIMITIVES as p (p)}
              <li>
                <div class="tnode mono">
                  <button type="button" class="tname" onclick={() => scrollToComponent(p)}>{p}</button>
                  <span class="tdepth">leaf</span>
                </div>
              </li>
            {/each}
          </ul>
        </aside>

        <!-- live cards -->
        <section class="list cards-pane">
          {#each visibleComponents as comp (comp.name)}
            <article class="card" id="c-{comp.name}">
              <header class="card-head">
                <h2 class="name mono">{comp.name}</h2>
                <span class="card-depth mono">depth {comp.depth}</span>
              </header>
              <div class="stage">
                <div class="live-host" bind:this={hosts[comp.name]}>
                  {#key refreshKey + '/' + depth}
                    {@render liveDemo(comp.name)}
                  {/key}
                  {#each (outlineRects[comp.name] ?? []) as r, ri (ri)}
                    <div
                      class="outline-box"
                      style="top:{r.top}px; left:{r.left}px; width:{r.width}px; height:{r.height}px"
                    ></div>
                  {/each}
                </div>
              </div>
              {#if NODE_BY_NAME.get(comp.name)?.children.length}
                <div class="subtree">
                  <span class="subtree-label mono">subcomponents — click to outline</span>
                  {@render subTree(comp.name, NODE_BY_NAME.get(comp.name)!)}
                </div>
              {:else}
                <span class="subtree-label mono leaf-note">leaf — no subcomponents</span>
              {/if}
            </article>
          {/each}
        </section>
      </div>
    </section>
  </div>
</div>

<style>
  .layout {
    display: flex;
    align-items: flex-start;
    min-height: 100vh;
  }

  /* ── side TOC ── */
  .toc {
    position: sticky;
    top: 0;
    flex: 0 0 200px;
    height: 100vh;
    overflow-y: auto;
    padding: var(--space-6);
    border-right: 1px solid var(--border);
    background: var(--surface);
    font-size: 12px;
  }
  .toc-head {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-bottom: var(--space-5);
  }
  .toc-sub {
    color: var(--text3);
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .toc-list,
  .toc-sublist {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }
  .toc-list > li > a {
    color: var(--text);
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .toc-sublist {
    margin: var(--space-2) 0 0 var(--space-3);
    gap: var(--space-1);
    padding-left: var(--space-3);
    border-left: 1px solid var(--border);
  }
  .toc-sublist a {
    color: var(--text2);
    font-size: 11px;
  }
  .toc a {
    text-decoration: none;
  }
  .toc a:hover {
    color: var(--atomizer);
  }

  /* ── scroll column ── */
  .scroll {
    flex: 1 1 auto;
    min-width: 0;
    padding: var(--space-6);
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }
  .section {
    scroll-margin-top: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
  }
  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    border-bottom: 1px solid var(--border2);
    padding-bottom: var(--space-3);
  }
  .section-title {
    font-size: 18px;
    color: var(--text);
    letter-spacing: 2px;
    text-transform: uppercase;
  }
  .reset {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius-md);
    color: var(--text2);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: var(--space-2) var(--space-4);
    cursor: pointer;
    transition: background 0.12s var(--ease-mech), color 0.12s, border-color 0.12s;
  }
  .reset:hover {
    background: color-mix(in oklab, var(--atomizer) 22%, var(--surface2));
    border-color: var(--atomizer);
    color: var(--text);
  }
  .note {
    color: var(--text3);
    font-size: 11px;
    line-height: 1.6;
    max-width: 720px;
  }
  .note code {
    color: var(--text2);
  }
  .note strong {
    color: var(--warn);
  }

  /* ── token panel ── */
  .swatch-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: var(--space-3);
  }
  .swatch-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    cursor: pointer;
  }
  .swatch-box {
    flex: 0 0 auto;
    width: 32px;
    height: 32px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border2);
  }
  .tok-name {
    flex: 0 0 auto;
    color: var(--text);
    font-size: 12px;
  }
  .tok-val {
    flex: 1 1 auto;
    color: var(--text3);
    font-size: 11px;
    text-align: right;
  }
  .color-input {
    flex: 0 0 auto;
    width: 34px;
    height: 26px;
    padding: 0;
    background: none;
    border: 1px solid var(--border2);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .scale-grid {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  .scale-row {
    display: flex;
    align-items: center;
    gap: var(--space-4);
  }
  .bar {
    flex: 0 0 auto;
    height: 14px;
    min-width: 2px;
    background: var(--atomizer);
    border-radius: var(--radius-sm);
  }
  .radius-box {
    flex: 0 0 auto;
    width: 32px;
    height: 32px;
    background: var(--surface3);
    border: 1px solid var(--border2);
  }
  .font-row {
    flex-wrap: wrap;
  }
  .specimen {
    flex: 1 1 240px;
    color: var(--text);
    font-size: 16px;
  }
  .text-input {
    flex: 0 0 auto;
    width: 110px;
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: 11px;
    padding: var(--space-1) var(--space-2);
  }
  .text-input.wide {
    width: 300px;
    max-width: 100%;
  }
  .text-input:focus {
    outline: none;
    border-color: var(--atomizer);
  }
  .scale-row .tok-name {
    flex: 1 1 auto;
  }

  /* ── inline demo snippets for SttComposerShell ── */
  .demo-ctl {
    min-height: 52px;
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    color: var(--text2);
    font-size: 11px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
  }
  .demo-meter {
    width: 100%;
    height: 10px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 5px;
    overflow: hidden;
  }
  .demo-meter-bar {
    width: 45%;
    height: 100%;
    background: var(--recording);
    opacity: 0.6;
  }

  .tts-frame {
    height: 340px;
    display: flex;
    width: 100%;
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }
  .agent-frame {
    height: 460px;
    display: flex;
    width: 100%;
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  /* ── components explorer ── */
  .depth-ctl {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    font-size: 11px;
    color: var(--text2);
  }
  .depth-ctl label {
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text3);
  }
  .depth-sel {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-family: var(--mono);
    font-size: 11px;
    padding: var(--space-1) var(--space-2);
    cursor: pointer;
  }
  .depth-sel:focus {
    outline: none;
    border-color: var(--atomizer);
  }
  .depth-max {
    color: var(--text3);
  }

  .explorer {
    display: grid;
    grid-template-columns: minmax(220px, 280px) 1fr;
    gap: var(--space-5);
    align-items: start;
  }
  .tree-pane {
    position: sticky;
    top: var(--space-4);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    max-height: calc(100vh - var(--space-6) * 2);
    overflow-y: auto;
  }
  .prims-head {
    margin-top: var(--space-3);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border);
  }
  .tree,
  .tchildren {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .tchildren {
    margin-left: var(--space-3);
    padding-left: var(--space-3);
    border-left: 1px solid var(--border);
  }
  .tnode {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: 1px var(--space-2);
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    transition: background 0.1s, border-color 0.1s;
  }
  .tnode.descendant {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface));
    border-color: color-mix(in oklab, var(--atomizer) 40%, var(--border));
  }
  .tnode.hovered {
    background: color-mix(in oklab, var(--atomizer) 30%, var(--surface));
    border-color: var(--atomizer);
  }
  .tname {
    background: none;
    border: 0;
    padding: 0;
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
    cursor: pointer;
    text-align: left;
  }
  .tname:hover {
    color: var(--atomizer);
  }
  .tname.sub {
    font-size: 11px;
    color: var(--text2);
    padding: var(--space-0) var(--space-2);
    border-radius: var(--radius-sm);
  }
  .tname.sub:hover {
    color: var(--atomizer);
  }
  .tname.sub.active {
    background: color-mix(in oklab, var(--atomizer) 28%, var(--surface2));
    border: 1px solid var(--atomizer);
    color: var(--text);
  }
  .tdepth {
    color: var(--text3);
    font-size: 9px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .tmore {
    color: var(--atomizer);
    font-size: 12px;
    line-height: 1;
  }

  .cards-pane {
    min-width: 0;
  }
  .card-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
  }
  .card-depth {
    color: var(--text3);
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .live-host {
    position: relative;
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    align-items: flex-start;
  }
  .outline-box {
    position: absolute;
    pointer-events: none;
    border: 2px solid var(--atomizer);
    border-radius: var(--radius-sm);
    background: color-mix(in oklab, var(--atomizer) 12%, transparent);
    box-shadow: 0 0 0 1px var(--bg);
    z-index: 6;
  }
  .subtree {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    border-top: 1px solid var(--border);
    padding-top: var(--space-3);
  }
  .subtree-label {
    color: var(--text3);
    font-size: 10px;
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  .leaf-note {
    border-top: 1px solid var(--border);
    padding-top: var(--space-3);
  }

  /* ── shared card styling — global so it also reaches Gallery's unscoped
       (style-less) markup rendered in flat mode, keeping primitives + composite
       cards visually identical. Scoped to this single-page bundle. ── */
  :global(.list) {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
  }
  :global(.card) {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: var(--space-5);
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }
  :global(.card .name) {
    font-size: 12px;
    color: var(--text2);
    letter-spacing: 1px;
    text-transform: uppercase;
  }
  :global(.card .body),
  :global(.card .card-body) {
    color: var(--text3);
    font-size: 11px;
    line-height: 1.5;
  }
  :global(.card .stage) {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    align-items: flex-start;
  }
  :global(.card .stage.row) {
    flex-direction: row;
    align-items: center;
  }
  :global(.card .stage.col) {
    flex-direction: column;
    align-items: flex-start;
  }
  :global(.card .stage.wrap) {
    flex-wrap: wrap;
  }
  :global(.card .stage.narrow) {
    max-width: 200px;
    width: 100%;
  }
  :global(.card .stage.chips) {
    max-width: 360px;
    width: 100%;
  }
  :global(.missing) {
    color: var(--warn);
    border: 1px solid color-mix(in oklab, var(--warn) 40%, var(--border));
    border-radius: var(--radius-md);
    padding: var(--space-3);
    font-size: 11px;
  }
  :global(.ttip-target) {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius-md);
    color: var(--text);
    font-size: 12px;
    padding: var(--space-2) var(--space-3);
    cursor: pointer;
  }
  :global(.mono) {
    font-family: var(--mono);
  }

  @media (max-width: 720px) {
    .layout {
      flex-direction: column;
    }
    .toc {
      position: static;
      flex-basis: auto;
      width: 100%;
      height: auto;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }
    .explorer {
      grid-template-columns: 1fr;
    }
    .tree-pane {
      position: static;
      max-height: none;
    }
  }
</style>
