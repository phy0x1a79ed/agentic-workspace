<script lang="ts">
  import { onMount } from 'svelte';
  import { Gallery, PanelLabel, Input } from '@awm/primitives';
  import {
    PttComposer,
    PttComposerShell,
    PttButton,
    TextTab,
    VoiceTab,
    VoiceChip,
  } from '@awm/ptt-composer';
  import { TtsHistory, type Post } from '@awm/tts-history';

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

  const COMPOSITES = [
    'PttComposer',
    'PttComposerShell',
    'PttButton',
    'TextTab',
    'VoiceTab',
    'VoiceChip',
    'TtsHistory',
  ];
</script>

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
        <a href="#composites">Composites</a>
        <ul class="toc-sublist">
          {#each COMPOSITES as c}
            <li><a href="#c-{c}">{c}</a></li>
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

    <!-- ── COMPOSITES ─────────────────────────────────────────────────── -->
    <section id="composites" class="section">
      <header class="section-head">
        <h1 class="section-title mono">Composites</h1>
      </header>
      <section class="list">
        <article class="card" id="c-PttComposer">
          <h2 class="name mono">PttComposer</h2>
          <p class="body mono">
            full composer (transport + shell) on its offline mock path — no
            /svc/ptt session opened.
          </p>
          <div class="stage">
            <PttComposer mockInitialChips={composerChips} />
          </div>
        </article>

        <article class="card" id="c-PttComposerShell">
          <h2 class="name mono">PttComposerShell</h2>
          <p class="body mono">bare shell with inline control + meter snippets.</p>
          <div class="stage">
            <PttComposerShell initialChips={shellChips}>
              {#snippet voiceControls()}
                <button type="button" class="demo-ctl mono">PTT</button>
              {/snippet}
              {#snippet voiceMeter()}
                <div class="demo-meter"><div class="demo-meter-bar"></div></div>
              {/snippet}
            </PttComposerShell>
          </div>
        </article>

        <article class="card" id="c-PttButton">
          <h2 class="name mono">PttButton</h2>
          <p class="body mono">presentational push-to-talk affordance.</p>
          <div class="stage narrow">
            <PttButton onpttdown={() => {}} onpttup={() => {}} />
          </div>
        </article>

        <article class="card" id="c-TextTab">
          <h2 class="name mono">TextTab</h2>
          <p class="body mono">plain type-and-send escape hatch.</p>
          <div class="stage">
            <TextTab />
          </div>
        </article>

        <article class="card" id="c-VoiceTab">
          <h2 class="name mono">VoiceTab</h2>
          <p class="body mono">uneditable voice chip list (seeded mock chips).</p>
          <div class="stage">
            <VoiceTab initialChips={voiceTabChips} />
          </div>
        </article>

        <article class="card" id="c-VoiceChip">
          <h2 class="name mono">VoiceChip</h2>
          <p class="body mono">atomic transcribed-utterance card; live + settled states.</p>
          <div class="stage col chips">
            <VoiceChip text="a settled voice chip" />
            <VoiceChip text="a live, streaming chip" live />
          </div>
        </article>

        <article class="card" id="c-TtsHistory">
          <h2 class="name mono">TtsHistory</h2>
          <p class="body mono">
            chat transcript with grouped membership / tool clusters and a
            per-message speak affordance (mock Post[]).
          </p>
          <div class="stage">
            <div class="tts-frame">
              <TtsHistory posts={mockPosts} onspeak={noopSpeak} />
            </div>
          </div>
        </article>
      </section>
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

  /* ── inline demo snippets for PttComposerShell ── */
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
  }
</style>
