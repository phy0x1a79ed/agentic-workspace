<script lang="ts">
  import type { Snippet } from 'svelte';
  import PttTab from './PttTab.svelte';
  import ConvoTab from './ConvoTab.svelte';

  // Top-level composer surface. Owns:
  //   - the activeTab state + the tab strip
  //   - the footer row (mic-volume meter + PTT-button slot + Send)
  //   - method dispatch: captureCaret / beginLiveChunk / updateLiveChunk /
  //     finalizeLiveChunk forward to whichever tab is active
  //   - setMicLevel + getMode for the panel's WS framing decisions

  type Mode = 'ptt' | 'convo';

  interface Props {
    onsend?: (text: string) => void;
    onTabSwitchRequest?: (target: Mode) => boolean;
    initialChunks?: Array<string | { chunk?: string; text?: string }>;
    initialChips?: string[];
    ptt?: Snippet;
  }
  let {
    onsend,
    onTabSwitchRequest,
    initialChunks,
    initialChips,
    ptt,
  }: Props = $props();

  let activeTab = $state<Mode>('convo');
  let micLevel = $state(0);

  let pttTab: PttTab | null = $state(null);
  let convoTab: ConvoTab | null = $state(null);

  function activeComponent(): PttTab | ConvoTab | null {
    return activeTab === 'ptt' ? pttTab : convoTab;
  }

  export function getMode(): Mode { return activeTab; }

  export function setMicLevel(v: number) {
    micLevel = Math.max(0, Math.min(1, v));
  }

  export function captureCaret() { activeComponent()?.captureCaret(); }
  export function beginLiveChunk() { activeComponent()?.beginLiveChunk(); }
  export function updateLiveChunk(text: string) { activeComponent()?.updateLiveChunk(text); }
  export function finalizeLiveChunk(text: string) { activeComponent()?.finalizeLiveChunk(text); }

  function consumeActiveText(): string {
    if (activeTab === 'ptt') return pttTab?.walkText() ?? '';
    return convoTab?.consumeText() ?? '';
  }

  function onSendClick() {
    const text = consumeActiveText();
    if (!text) return;
    onsend?.(text);
    if (activeTab === 'ptt') pttTab?.clear();
    else convoTab?.clear();
  }

  function selectTab(target: Mode) {
    if (target === activeTab) return;
    if (onTabSwitchRequest && !onTabSwitchRequest(target)) return;
    activeTab = target;
  }
</script>

<section class="shell">
  <div class="tabs" role="tablist">
    <button
      type="button"
      class="tab"
      class:active={activeTab === 'ptt'}
      role="tab"
      aria-selected={activeTab === 'ptt'}
      onclick={() => selectTab('ptt')}
    >PTT</button>
    <button
      type="button"
      class="tab"
      class:active={activeTab === 'convo'}
      role="tab"
      aria-selected={activeTab === 'convo'}
      onclick={() => selectTab('convo')}
    >CONVO</button>
  </div>

  <div class="pane" class:hidden={activeTab !== 'ptt'}>
    <PttTab bind:this={pttTab} {initialChunks} />
  </div>
  <div class="pane" class:hidden={activeTab !== 'convo'}>
    <ConvoTab bind:this={convoTab} {initialChips} />
  </div>

  <div class="footer">
    <div class="mic-meter" aria-hidden="true">
      <div class="mic-bar" style:transform="scaleX({micLevel})"></div>
    </div>

    <div class="ptt-slot">
      {#if ptt}{@render ptt()}{/if}
    </div>

    <button
      type="button"
      class="footer-btn send"
      onclick={onSendClick}
      title="send"
      aria-label="send"
    >SEND</button>
  </div>
</section>

<style>
  .shell {
    display: flex;
    flex-direction: column;
    gap: var(--space-2, 8px);
    max-width: 480px;
    width: 100%;
  }

  .tabs {
    display: flex;
    gap: var(--space-1, 4px);
    border-bottom: 1px solid var(--border, #333);
  }
  .tab {
    flex: 1 1 0;
    padding: var(--space-2, 8px) var(--space-3, 12px);
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text3, #888);
    font-family: var(--mono, monospace);
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s;
  }
  .tab:hover { color: var(--text, #ddd); }
  .tab.active {
    color: var(--text, #ddd);
    border-bottom-color: var(--atomizer, #ffb74d);
  }
  .tab:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  .pane { width: 100%; }
  .pane.hidden { display: none; }

  .footer {
    display: flex;
    align-items: stretch;
    gap: var(--space-2, 8px);
  }
  .mic-meter {
    flex: 0 0 40px;
    align-self: stretch;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
    min-height: 52px;
  }
  .mic-bar {
    position: absolute;
    inset: 0;
    background: linear-gradient(to top,
      color-mix(in oklab, var(--recording, #f55) 65%, transparent) 0%,
      color-mix(in oklab, var(--recording, #f55) 25%, transparent) 100%);
    transform-origin: left center;
    transform: scaleX(0);
    transition: transform 60ms linear;
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
    min-width: 64px;
    padding: 0 14px;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    font-family: var(--mono, monospace);
    font-size: 12px;
    letter-spacing: 1px;
    text-transform: uppercase;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
  }
  .footer-btn:hover  { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }
  .footer-btn.send {
    border-color: color-mix(in oklab, var(--atomizer, #ffb74d) 40%, var(--border, #333));
    color: var(--text, #ddd);
  }
  .footer-btn.send:hover {
    background: color-mix(in oklab, var(--atomizer, #ffb74d) 30%, var(--surface2, #222));
    border-color: var(--atomizer, #ffb74d);
  }
  @media (hover: none), (max-width: 720px) {
    .footer-btn { min-height: 60px; font-size: 13px; }
    .mic-meter { min-height: 60px; }
  }
</style>
