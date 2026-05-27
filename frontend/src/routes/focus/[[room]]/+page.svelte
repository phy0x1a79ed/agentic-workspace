<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    listRooms, getRoomAgents, postToRoom, runAgentSlash,
    type Room, type RoomAgent, type Post
  } from '$lib/api/client';
  import { RoomWsPool, type RoomEvent } from '$lib/api/ws.svelte';
  import UnifiedSidebar from '$lib/components/UnifiedSidebar.svelte';
  import DetailsPanel  from '$lib/components/DetailsPanel.svelte';
  import Transcript    from '$lib/components/Transcript.svelte';
  import Composer      from '$lib/components/Composer.svelte';
  import PttButton     from '$lib/components/PttButton.svelte';
  import Sheet         from '$lib/components/Sheet.svelte';
  import { recipients } from '$lib/state/recipients.svelte';
  import { ui } from '$lib/state/ui.svelte';
  import { voice } from '$lib/state/voice.svelte';

  let rooms        = $state<Room[]>([]);
  let activeId     = $state<string | null>(null);
  let agentsByRoom = $state<Record<string, RoomAgent[]>>({});
  let postsByRoom  = $state<Record<string, Post[]>>({});
  let composerText = $state<string>('');
  let errorBanner  = $state<string>('');

  const pool = new RoomWsPool();
  const wsUnsubs = new Map<string, () => void>();
  let unsubVoice: (() => void) | null = null;

  // Derived: posts + agents for the currently focused room.
  const posts    = $derived<Post[]>(activeId ? (postsByRoom[activeId] ?? []) : []);
  const agents   = $derived<RoomAgent[]>(activeId ? (agentsByRoom[activeId] ?? []) : []);
  const banner   = $derived<string>(errorBanner || (activeId ? pool.bannerOf(activeId) : ''));

  // Route effect is the SINGLE writer of `activeId`. Sidebar clicks call
  // `gotoRoom()` which only updates the URL — the effect below then sees the
  // new $page.params.room and switches focus. No WS churn — the room's
  // socket either already exists or gets opened once and stays open.
  $effect(() => {
    const fromUrl = $page.params.room ? decodeURIComponent($page.params.room) : null;
    if (fromUrl && fromUrl !== activeId) focusRoom(fromUrl);
  });

  // Auto-pick the first room when landing on bare /focus. Separated from the
  // route effect so it doesn't depend on activeId.
  $effect(() => {
    if (!$page.params.room && !activeId && rooms.length) {
      goto(`${base}/focus/${encodeURIComponent(rooms[0].id)}`, { replaceState: true });
    }
  });

  // Keep one open socket per room in the visible rail. Switching focus is
  // pure UI — sockets stay open in the background so posts accumulate.
  $effect(() => {
    const want = new Set(rooms.map(r => r.id));
    for (const id of want) {
      if (!wsUnsubs.has(id)) {
        wsUnsubs.set(id, pool.on(id, (ev) => handleEvent(id, ev)));
      }
    }
    for (const id of Array.from(wsUnsubs.keys())) {
      if (!want.has(id)) {
        wsUnsubs.get(id)?.();
        wsUnsubs.delete(id);
        pool.detach(id);
      }
    }
  });

  onMount(async () => {
    // STT result → append to composer + auto-send. Mirrors legacy
    // appendTranscript + autoSend semantics: speaking IS the send.
    unsubVoice = voice.onResult((text) => {
      const cur = composerText;
      const sep = cur && !/\s$/.test(cur) ? ' ' : '';
      const next = (cur + sep + text).trim();
      composerText = next;
      onSend(next);
      composerText = '';
    });
    await refreshRooms();
    // Effects handle WS attach and initial navigation.
  });

  onDestroy(() => {
    for (const u of wsUnsubs.values()) u();
    wsUnsubs.clear();
    pool.closeAll();
    unsubVoice?.();
  });

  async function refreshRooms() {
    try { rooms = (await listRooms('active')).rooms; }
    catch (e) { errorBanner = (e as Error).message; rooms = []; }
  }

  function gotoRoom(roomId: string) {
    if (roomId === activeId) return;
    // Just navigate; the route effect picks it up. Don't touch state here.
    goto(`${base}/focus/${encodeURIComponent(roomId)}`);
    ui.closeAll();
  }

  async function focusRoom(roomId: string) {
    activeId = roomId;
    composerText = '';
    errorBanner = '';
    // Ensure an agent list exists for this room — refresh on each focus to
    // pick up scope/agent changes that happened in the background.
    await refreshAgents(roomId);
  }

  async function refreshAgents(roomId: string) {
    try { agentsByRoom[roomId] = (await getRoomAgents(roomId)).agents; }
    catch (e) { errorBanner = (e as Error).message; agentsByRoom[roomId] = []; }
  }

  function handleEvent(roomId: string, ev: RoomEvent) {
    switch (ev.type) {
      case 'history':
        if ('posts' in ev && Array.isArray(ev.posts)) postsByRoom[roomId] = ev.posts;
        break;
      case 'post':
        if ('post' in ev) {
          const cur = postsByRoom[roomId] ?? [];
          postsByRoom[roomId] = [...cur, ev.post as Post];
        }
        break;
      case 'participant_joined':
      case 'participant_left':
        refreshAgents(roomId);
        break;
    }
  }

  async function onSend(text: string) {
    if (!activeId) return;
    const selected = recipients.get(activeId);
    const scopes = selected.filter(k => k.startsWith('scope:')).map(k => k.slice('scope:'.length));
    try {
      if (scopes.length === 0) {
        await postToRoom(activeId, { body: text });
      } else {
        // Fan out to each selected scope. Backend echoes via WS; no local insert.
        await Promise.all(scopes.map(s => postToRoom(activeId!, { body: text, to_scope: s })));
      }
    } catch (e) { errorBanner = (e as Error).message; }
  }

  // Slash picker → POST /slash. The backend posts the slash echo to the
  // transcript itself, so we don't insert anything locally.
  async function onSlashPick(cmd: string, _autorun: boolean) {
    if (!activeId) return;
    const scope = ui.managerScope;
    if (!scope) { errorBanner = 'no manager scope — bootstrap missing'; return; }
    try {
      const res = await runAgentSlash(activeId, scope, cmd);
      if (res.result) errorBanner = res.result;
    } catch (e) { errorBanner = (e as Error).message; }
  }

  const activeRoom = $derived(rooms.find(r => r.id === activeId));
  const selectedKeys = $derived(activeId ? recipients.get(activeId) : []);
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
      <span class="banner mono">{banner}</span>
      <span class="spacer"></span>
      <button class="sheet-toggle right" type="button" aria-label="details" onclick={() => ui.openRight()}>
        <span class="bracket">[</span><span class="word">AGENTS</span><span class="glyph">@</span><span class="bracket">]</span>
      </button>
    </header>

    {#if activeId}
      <Transcript {posts} />
      <Composer
        disabled={!activeId}
        bind:text={composerText}
        bind:slashOpen={ui.slashOpen}
        roomId={activeId}
        scope={ui.managerScope}
        onsubmit={onSend}
        onslashpick={onSlashPick}
      />
      <div class="ptt-row">
        <PttButton
          disabled={!activeId}
          onpttdown={() => voice.start()}
          onpttup={() => voice.end()}
        />
      </div>
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

  .ptt-row { padding: 10px 14px 14px; background: var(--surface); border-top: 1px solid var(--border); }
  .dim     { color: var(--text3); }

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
    .ptt-row { padding: 8px 10px 10px; }
  }
</style>
