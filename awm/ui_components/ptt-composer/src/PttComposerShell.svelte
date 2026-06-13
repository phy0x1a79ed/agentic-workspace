<script lang="ts">
  import type { Snippet } from 'svelte';
  import TextTab from './TextTab.svelte';
  import VoiceTab from './VoiceTab.svelte';

  // Top-level composer surface. The tab axis is trust/flow, not input method:
  //   - TEXT  — plain type-and-send, no STT/WS/AI. Reliable escape hatch.
  //   - VOICE — push-to-talk AND continuous conversation, both feeding one
  //             uneditable chip space.
  // Owns:
  //   - the activeTab state + the tab strip
  //   - the shared footer Send (acts on the active tab's consumeText)
  //   - method dispatch: beginLiveChunk / updateLiveChunk / finalizeLiveChunk
  //     forward to whichever tab is active (no-ops on TextTab)
  //   - getTab() for the transport's gesture-routing decisions
  // The PTT / Convo / Pause controls are transport-owned and passed in via
  // the {voiceControls} snippet, rendered inside the Voice pane.

  type Tab = 'text' | 'voice';

  interface Props {
    onsend?: (text: string) => void;
    onTabSwitchRequest?: (target: Tab) => boolean;
    initialChips?: string[];
    voiceControls?: Snippet;
    voiceMeter?: Snippet;
    // Convo display: when a continuous session is live, the Voice pane renders
    // a flowing text panel of the accumulating message instead of chips.
    convo?: boolean;
    convoText?: string;
  }
  let {
    onsend,
    onTabSwitchRequest,
    initialChips,
    voiceControls,
    voiceMeter,
    convo = false,
    convoText = '',
  }: Props = $props();

  let activeTab = $state<Tab>('voice');

  let textTab: TextTab | null = $state(null);
  let voiceTab: VoiceTab | null = $state(null);

  function activeComponent(): TextTab | VoiceTab | null {
    return activeTab === 'text' ? textTab : voiceTab;
  }

  export function getTab(): Tab { return activeTab; }

  export function beginLiveChunk() { activeComponent()?.beginLiveChunk(); }
  export function updateLiveChunk(text: string) { activeComponent()?.updateLiveChunk(text); }
  export function finalizeLiveChunk(text: string) { activeComponent()?.finalizeLiveChunk(text); }

  function onSendClick() {
    const text = activeComponent()?.consumeText() ?? '';
    if (!text) return;
    onsend?.(text);
    activeComponent()?.clear();
  }

  function selectTab(target: Tab) {
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
      class:active={activeTab === 'text'}
      role="tab"
      aria-selected={activeTab === 'text'}
      onclick={() => selectTab('text')}
    >TEXT</button>
    <button
      type="button"
      class="tab"
      class:active={activeTab === 'voice'}
      role="tab"
      aria-selected={activeTab === 'voice'}
      onclick={() => selectTab('voice')}
    >VOICE</button>
  </div>

  <div class="pane" class:hidden={activeTab !== 'text'}>
    <TextTab bind:this={textTab} />
  </div>
  <div class="pane" class:hidden={activeTab !== 'voice'}>
    <VoiceTab
      bind:this={voiceTab}
      {initialChips}
      {convo}
      {convoText}
      controls={voiceControls}
      meter={voiceMeter}
      onsend={onSendClick}
    />
  </div>

  <!-- The Voice tab carries its own SEND inside the right button column;
       the shared footer SEND is only for the Text tab. -->
  {#if activeTab === 'text'}
    <div class="footer">
      <button
        type="button"
        class="footer-btn send"
        onclick={onSendClick}
        title="send"
        aria-label="send"
      >SEND</button>
    </div>
  {/if}
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
  }
  .footer-btn {
    flex: 1 1 auto;
    min-height: 52px;
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
  }
</style>
