<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    listRooms, getRoomAgents,
    type Room, type RoomAgent,
  } from '$lib/api/client';
  import UnifiedSidebar     from '$lib/components/UnifiedSidebar.svelte';
  import DetailsPanel       from '$lib/components/DetailsPanel.svelte';
  import VoiceConversation  from '$lib/components/VoiceConversation.svelte';
  import Sheet              from '$lib/components/Sheet.svelte';
  import { recipients } from '$lib/state/recipients.svelte';
  import { ui } from '$lib/state/ui.svelte';

  let rooms        = $state<Room[]>([]);
  let activeId     = $state<string | null>(null);
  let agentsByRoom = $state<Record<string, RoomAgent[]>>({});
  let errorBanner  = $state<string>('');

  const agents = $derived<RoomAgent[]>(activeId ? (agentsByRoom[activeId] ?? []) : []);

  // Route effect is the SINGLE writer of `activeId`. Sidebar clicks call
  // `gotoRoom()` which only updates the URL — the effect below then sees the
  // new ?room= query and switches focus.
  $effect(() => {
    const fromUrl = $page.url.searchParams.get('room');
    if (fromUrl && fromUrl !== activeId) focusRoom(fromUrl);
  });

  // Auto-pick the first room when landing on bare /focus.
  $effect(() => {
    if (!$page.url.searchParams.get('room') && !activeId && rooms.length) {
      goto(`${base}/focus?room=${encodeURIComponent(rooms[0].id)}`, { replaceState: true });
    }
  });

  onMount(async () => {
    await refreshRooms();
  });

  async function refreshRooms() {
    try { rooms = (await listRooms('active')).rooms; }
    catch (e) { errorBanner = (e as Error).message; rooms = []; }
  }

  function gotoRoom(roomId: string) {
    if (roomId === activeId) return;
    goto(`${base}/focus?room=${encodeURIComponent(roomId)}`);
    ui.closeAll();
  }

  async function focusRoom(roomId: string) {
    activeId = roomId;
    errorBanner = '';
    await refreshAgents(roomId);
  }

  async function refreshAgents(roomId: string) {
    try { agentsByRoom[roomId] = (await getRoomAgents(roomId)).agents; }
    catch (e) { errorBanner = (e as Error).message; agentsByRoom[roomId] = []; }
  }

  const activeRoom = $derived(rooms.find(r => r.id === activeId));
  const selectedKeys = $derived(activeId ? recipients.get(activeId) : []);
  // VoiceConversation accepts a single ``toScope`` — pick the first selected
  // scope recipient (the previous fan-out across multiple was a niche path).
  const primaryToScope = $derived(
    selectedKeys.filter(k => k.startsWith('scope:')).map(k => k.slice('scope:'.length))[0] ?? null
  );
</script>

<div class="focus chrome" class:with-details={activeId}>
  <div class="left">
    <UnifiedSidebar {rooms} {activeId} onpick={gotoRoom} />
  </div>

  <main class="main">
    <header class="rh">
      <button class="sheet-toggle" type="button" aria-label="rooms" onclick={() => ui.openLeft()}>
        <span class="bracket">[</span><span class="glyph">≡</span><span class="word">ROOMS</span><span class="bracket">]</span>
      </button>
      <span class="title mono">{activeRoom?.id ?? 'no room focused'}</span>
      <span class="topic">{activeRoom?.topic ?? ''}</span>
      <span class="banner mono">{errorBanner}</span>
      <span class="spacer"></span>
      <button class="sheet-toggle right" type="button" aria-label="details" onclick={() => ui.openRight()}>
        <span class="bracket">[</span><span class="word">AGENTS</span><span class="glyph">@</span><span class="bracket">]</span>
      </button>
    </header>

    {#if activeId}
      <VoiceConversation
        roomId={activeId}
        toScope={primaryToScope}
        managerScope={ui.managerScope}
        sttIntoComposer
        sendToRoom
        onresult={(m) => errorBanner = m}
        onagents={(a) => { if (activeId) agentsByRoom[activeId] = a; }}
      />
    {:else}
      <div class="empty-state">
        <div class="empty-icon">◇</div>
        <div class="empty-text">join a room to start<br /><span class="dim">pick from the list ←</span></div>
      </div>
    {/if}
  </main>

  <div class="right">
    {#if activeId}
      <DetailsPanel
        {agents}
        roomId={activeId}
        recipients={selectedKeys}
        onrecipients={(keys) => activeId && recipients.set(activeId, keys)}
        onagentresult={(m) => errorBanner = m}
      />
    {/if}
  </div>

  <Sheet bind:open={ui.leftSheetOpen} side="left">
    <UnifiedSidebar {rooms} {activeId} onpick={(id) => { gotoRoom(id); ui.closeAll(); }} />
  </Sheet>

  <Sheet bind:open={ui.rightSheetOpen} side="right">
    {#if activeId}
      <DetailsPanel
        {agents}
        roomId={activeId}
        recipients={selectedKeys}
        onrecipients={(keys) => activeId && recipients.set(activeId, keys)}
        onagentresult={(m) => errorBanner = m}
      />
    {/if}
  </Sheet>
</div>

<style>
  .focus {
    display: grid;
    grid-template-columns: 280px 1fr;
    grid-template-rows: 1fr;
    flex: 1;
    overflow: hidden;
    min-height: 0;
  }
  .focus.with-details { grid-template-columns: 280px 1fr 360px; }
  .left, .right { overflow: hidden; min-height: 0; }
  .main {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
    background: var(--bg);
  }
  .rh {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .title  { color: var(--text); font-size: 12px; letter-spacing: 0.5px; }
  .topic  { color: var(--text2); font-size: 12px; }
  .banner { color: var(--text3); font-size: 10px; }
  .spacer { flex: 1; }
  .mono   { font-family: var(--mono); }

  .sheet-toggle {
    display: none;
    align-items: center;
    gap: 4px;
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text2);
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    height: 44px;
    padding: 0 10px;
    border-radius: 3px;
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
  }
  .sheet-toggle:hover,
  .sheet-toggle:focus-visible {
    background: var(--surface3);
    border-color: color-mix(in oklab, var(--atomizer) 50%, var(--border));
    color: var(--text);
    outline: none;
  }
  .sheet-toggle.right { margin-left: auto; }
  .sheet-toggle .bracket { color: var(--text3); font-size: 13px; }
  .sheet-toggle .glyph   { font-size: 16px; color: var(--text); }
  .sheet-toggle .word    { display: inline-block; }

  .dim { color: var(--text3); }

  @media (max-width: 1024px) {
    .focus, .focus.with-details { grid-template-columns: 1fr; }
    .left, .right { display: none; }
    .sheet-toggle { display: inline-flex; }
  }
  @media (max-width: 480px) {
    .sheet-toggle { padding: 0 8px; gap: 0; }
    .sheet-toggle .word { display: none; }
  }
  @media (max-width: 720px) {
    .rh { padding: 8px 10px; }
  }
</style>
