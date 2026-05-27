<script lang="ts">
  /**
   * Voice control panel — the body of the "voice" CollapsibleSection on
   * /status. Three nested sections: Chat Log (history into the user's
   * voice-tests room), Input (text + STT with send-to-agent toggle),
   * Output (direct TTS with auto-speak-replies toggle). Input and Output
   * each carry their own Engine sub-collapsible with a DynamicConfigForm
   * driven by the loaded engine's schema.
   *
   * Engine selection / hot-load is shared with the parent at /status —
   * loaded state is passed in. This component owns the per-section
   * "draft cfg" state and POSTs to /voice/engines/{kind}/load on Apply.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    ensureVagrantVoiceTests, postToRoom, getRoomHistory,
    loadEngine,
    type EnginesByKind, type LoadedEnginesByKind,
    type Post,
  } from '$lib/api/client';
  import { RoomWsPool, type RoomEvent } from '$lib/api/ws.svelte';
  import CollapsibleSection from '$lib/primitives/CollapsibleSection.svelte';
  import Button from '$lib/primitives/Button.svelte';
  import Input from '$lib/primitives/Input.svelte';
  import Select from './Select.svelte';
  import ChatHistory from './ChatHistory.svelte';
  import ChatInput from './ChatInput.svelte';
  import PttButton from './PttButton.svelte';
  import DynamicConfigForm from './DynamicConfigForm.svelte';
  import { voice } from '$lib/state/voice.svelte';
  import { speak as ttsSpeak } from '$lib/voice/tts';

  interface Props {
    engines: EnginesByKind | null;
    loaded: LoadedEnginesByKind;
    onloaded?: (kind: string, l: LoadedEnginesByKind[string]) => void;
    onerror?: (msg: string) => void;
  }
  let { engines, loaded = $bindable({}), onloaded, onerror }: Props = $props();

  // ── voice-tests scope bootstrap ────────────────────────────────────────
  let roomId         = $state<string>('');
  let scopeId        = $state<string>('');
  let managerLive    = $state<boolean>(false);
  let bootErr        = $state<string>('');
  let bootstrapping  = $state<boolean>(false);

  onMount(async () => {
    bootstrapping = true;
    try {
      const r = await ensureVagrantVoiceTests();
      roomId = r.room_id;
      scopeId = r.scope_identifier;
      managerLive = r.manager_live;
    } catch (e) {
      bootErr = (e as Error).message;
    } finally {
      bootstrapping = false;
    }
  });

  // ── WS subscription for the voice-tests room ───────────────────────────
  const pool = new RoomWsPool();
  let posts = $state<Post[]>([]);
  let wsUnsub: (() => void) | null = null;

  $effect(() => {
    posts = [];
    wsUnsub?.();
    if (!roomId) return;
    wsUnsub = pool.on(roomId, (ev) => handleEvent(ev));
    refreshHistory(roomId);
    return () => { wsUnsub?.(); wsUnsub = null; pool.detach(roomId); };
  });

  function handleEvent(ev: RoomEvent) {
    if (ev.type === 'history' && 'posts' in ev && Array.isArray(ev.posts)) posts = ev.posts;
    else if (ev.type === 'post' && 'post' in ev) {
      const p = ev.post as Post;
      posts = [...posts, p];
      // Auto-speak: agent text replies → TTS, when toggle on. Compare ts to
      // avoid replaying the historical post we already saw on attach.
      if (autoSpeak && isAgentText(p) && (p.ts ?? '') > lastSpokenTs) {
        lastSpokenTs = p.ts ?? '';
        ttsSpeak(p.body ?? '').catch((e) => onerror?.((e as Error).message));
      }
    }
  }

  async function refreshHistory(rid: string) {
    try { posts = (await getRoomHistory(rid)).posts ?? []; }
    catch (e) { onerror?.((e as Error).message); }
  }

  function isAgentText(p: Post): boolean {
    if ((p.kind ?? 'text') !== 'text') return false;
    return (p.author ?? '').startsWith('agent:');
  }

  // ── compose / STT bridge ───────────────────────────────────────────────
  let inputText  = $state<string>('');
  let slashOpen  = $state<boolean>(false);
  let outputText = $state<string>('');
  let sendToAgent = $state<boolean>(true);
  let autoSpeak   = $state<boolean>(false);
  let lastSpokenTs = '';
  let voiceUnsub: (() => void) | null = null;

  onMount(() => {
    // STT result → composer + auto-send (only when sendToAgent is on; otherwise
    // just drop it into the box so the user can inspect / edit before sending).
    voiceUnsub = voice.onResult((text) => {
      const cur = inputText;
      const sep = cur && !/\s$/.test(cur) ? ' ' : '';
      inputText = (cur + sep + text).trim();
      if (sendToAgent) {
        const v = inputText;
        inputText = '';
        submitInput(v);
      }
    });
  });
  onDestroy(() => { voiceUnsub?.(); wsUnsub?.(); pool.closeAll(); });

  async function submitInput(text: string) {
    if (!text || !roomId) return;
    if (sendToAgent) {
      try { await postToRoom(roomId, { body: text }); }
      catch (e) { onerror?.((e as Error).message); }
    } else {
      // Display-only: local echo into the chat log.
      posts = [...posts, {
        ts: new Date().toISOString(),
        author: 'user:local',
        body: text,
        kind: 'text',
      } as Post];
    }
  }

  async function speakOutput() {
    const t = outputText.trim();
    if (!t) return;
    try { await ttsSpeak(t); }
    catch (e) { onerror?.((e as Error).message); }
  }

  // ── engine selection (per kind) ────────────────────────────────────────
  // Draft selections + cfg per kind; Apply commits via loadEngine().
  let draftId = $state<Record<string, string>>({ stt: '', tts: '', llm: '' });
  let draftCfg = $state<Record<string, Record<string, unknown>>>({ stt: {}, tts: {}, llm: {} });
  let applying = $state<string>('');

  $effect(() => {
    // Seed drafts from currently loaded engines whenever they change so the
    // form reflects "what's running" until the user edits it.
    for (const kind of ['stt', 'tts', 'llm']) {
      const cur = loaded[kind];
      if (cur) {
        if (!draftId[kind]) draftId[kind] = cur.id;
        if (Object.keys(draftCfg[kind]).length === 0) draftCfg[kind] = { ...cur.params };
      }
    }
  });

  function engineIds(kind: string): string[] {
    return Object.keys(engines?.[kind] ?? {});
  }

  function currentSchema(kind: string) {
    const id = draftId[kind] || loaded[kind]?.id;
    if (!id) return null;
    return engines?.[kind]?.[id]?.schema ?? null;
  }

  function pickEngine(kind: string, id: string) {
    const same = draftId[kind] === id;
    draftId[kind] = id;
    if (!same) {
      // Seed defaults from schema definition on a fresh pick.
      draftCfg[kind] = { ...(engines?.[kind]?.[id]?.defaults ?? {}) };
    }
  }

  async function applyEngine(kind: string) {
    const id = draftId[kind];
    if (!id) return;
    applying = kind;
    try {
      const r = await loadEngine(kind, id, draftCfg[kind] ?? {});
      loaded = { ...loaded, [kind]: r };
      onloaded?.(kind, r);
    } catch (e) {
      onerror?.((e as Error).message);
    } finally {
      applying = '';
    }
  }

  function engineSection(kind: 'stt' | 'tts' | 'llm') {
    return { kind, label: kind.toUpperCase(), schema: currentSchema(kind) };
  }
</script>

<div class="vp">
  <div class="bootline mono">
    {#if bootstrapping}
      <span class="dim">provisioning voice-tests scope…</span>
    {:else if bootErr}
      <span class="err">{bootErr}</span>
    {:else if roomId}
      <span class="dim">scope:</span> <span>{scopeId}</span>
      <span class="dim">· room:</span> <span class="mono">{roomId}</span>
      {#if !managerLive}<span class="warn"> · manager offline</span>{/if}
    {/if}
  </div>

  <CollapsibleSection label="chat log" rail="none" compact>
    <div class="chat-window">
      <ChatHistory {posts} />
    </div>
  </CollapsibleSection>

  <CollapsibleSection label="input · text + STT" rail="none" compact>
    <div class="toggles">
      <label class="toggle">
        <input type="checkbox" bind:checked={sendToAgent} />
        <span>send to agent (off = display only)</span>
      </label>
    </div>
    <ChatInput
      disabled={!roomId}
      bind:text={inputText}
      bind:slashOpen
      {roomId}
      scope={scopeId}
      onsubmit={submitInput}
    />
    <div class="ptt-wrap">
      <PttButton
        disabled={!roomId}
        onpttdown={() => voice.start()}
        onpttup={() => voice.end()}
      />
    </div>

    {#if engines}
      {@const sec = engineSection('stt')}
      <CollapsibleSection label="engine · stt{loaded.stt ? ' · ' + loaded.stt?.id : ''}" rail="none" compact>
        <div class="engine-pick">
          <span class="dim mono">engine:</span>
          <div class="engine-buttons">
            {#each engineIds('stt') as eid (eid)}
              {@const active = (draftId.stt || loaded.stt?.id) === eid}
              <Button
                kind={active ? 'primary' : 'ghost'}
                disabled={applying === 'stt'}
                onclick={() => pickEngine('stt', eid)}
              >{eid}</Button>
            {/each}
          </div>
        </div>
        {#if sec.schema}
          <DynamicConfigForm
            schema={sec.schema}
            bind:value={draftCfg.stt}
            disabled={applying === 'stt'}
          />
        {/if}
        <div class="apply-row">
          <Button
            kind="primary"
            disabled={applying === 'stt' || !draftId.stt}
            onclick={() => applyEngine('stt')}
          >{applying === 'stt' ? 'loading…' : 'apply'}</Button>
        </div>
      </CollapsibleSection>
    {/if}
  </CollapsibleSection>

  <CollapsibleSection label="output · text → TTS" rail="none" compact>
    <div class="toggles">
      <label class="toggle">
        <input type="checkbox" bind:checked={autoSpeak} />
        <span>auto-speak agent replies</span>
      </label>
    </div>
    <div class="output-row">
      <Input
        bind:value={outputText}
        placeholder="say something via the loaded TTS engine"
      />
      <Button kind="primary" disabled={!outputText.trim()} onclick={speakOutput}>speak</Button>
    </div>

    {#if engines}
      {@const sec = engineSection('tts')}
      <CollapsibleSection label="engine · tts{loaded.tts ? ' · ' + loaded.tts?.id : ''}" rail="none" compact>
        <div class="engine-pick">
          <span class="dim mono">engine:</span>
          <div class="engine-buttons">
            {#each engineIds('tts') as eid (eid)}
              {@const active = (draftId.tts || loaded.tts?.id) === eid}
              <Button
                kind={active ? 'primary' : 'ghost'}
                disabled={applying === 'tts'}
                onclick={() => pickEngine('tts', eid)}
              >{eid}</Button>
            {/each}
          </div>
        </div>
        {#if sec.schema}
          <DynamicConfigForm
            schema={sec.schema}
            bind:value={draftCfg.tts}
            disabled={applying === 'tts'}
          />
        {/if}
        <div class="apply-row">
          <Button
            kind="primary"
            disabled={applying === 'tts' || !draftId.tts}
            onclick={() => applyEngine('tts')}
          >{applying === 'tts' ? 'loading…' : 'apply'}</Button>
        </div>
      </CollapsibleSection>
    {/if}
  </CollapsibleSection>

  {#if engines && engineIds('llm').length}
    {@const sec = engineSection('llm')}
    <CollapsibleSection label="engine · llm{loaded.llm ? ' · ' + loaded.llm?.id : ''}" rail="none" compact>
      <div class="engine-pick">
        <span class="dim mono">engine:</span>
        <div class="engine-buttons">
          {#each engineIds('llm') as eid (eid)}
            {@const active = (draftId.llm || loaded.llm?.id) === eid}
            <Button
              kind={active ? 'primary' : 'ghost'}
              disabled={applying === 'llm'}
              onclick={() => pickEngine('llm', eid)}
            >{eid}</Button>
          {/each}
        </div>
      </div>
      {#if sec.schema}
        <DynamicConfigForm
          schema={sec.schema}
          bind:value={draftCfg.llm}
          disabled={applying === 'llm'}
        />
      {/if}
      <div class="apply-row">
        <Button
          kind="primary"
          disabled={applying === 'llm' || !draftId.llm}
          onclick={() => applyEngine('llm')}
        >{applying === 'llm' ? 'loading…' : 'apply'}</Button>
      </div>
    </CollapsibleSection>
  {/if}
</div>

<style>
  .vp { display: flex; flex-direction: column; gap: var(--space-2); }
  .bootline {
    padding: var(--space-1) var(--space-2);
    font-size: 10px;
    color: var(--text2);
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .dim  { color: var(--text3); }
  .err  { color: var(--danger); }
  .warn { color: var(--warn); }

  .chat-window {
    /* Pre-size the chat region so the section header doesn't jump when posts
       arrive. Internal ChatHistory scrolls within this frame. */
    display: flex;
    flex-direction: column;
    height: 320px;
    border: 1px solid var(--border);
    border-radius: 3px;
    overflow: hidden;
  }

  .toggles {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
    padding: var(--space-1) var(--space-2);
  }
  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--text2);
    cursor: pointer;
  }

  .ptt-wrap {
    padding: var(--space-2);
  }

  .output-row {
    display: flex;
    gap: var(--space-2);
    align-items: stretch;
    padding: var(--space-1) var(--space-2);
  }
  .output-row :global(.input) { flex: 1; }

  .engine-pick {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1) var(--space-2);
    flex-wrap: wrap;
  }
  .engine-buttons { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .apply-row {
    display: flex;
    justify-content: flex-end;
    padding: var(--space-2);
  }
  .mono { font-family: var(--mono); }
</style>
