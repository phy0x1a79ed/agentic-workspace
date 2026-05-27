<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import {
    listRooms, getRoomAgents, postToRoom,
    type Room, type RoomAgent, type Post
  } from '$lib/api/client';
  import { RoomWs, type RoomEvent } from '$lib/api/ws.svelte';
  import UnifiedSidebar from '$lib/components/UnifiedSidebar.svelte';
  import DetailsPanel  from '$lib/components/DetailsPanel.svelte';
  import Transcript    from '$lib/components/Transcript.svelte';
  import Composer      from '$lib/components/Composer.svelte';
  import PttButton     from '$lib/components/PttButton.svelte';
  import VoicePanel    from '$lib/components/VoicePanel.svelte';
  import Sheet         from '$lib/components/Sheet.svelte';
  import { recipients } from '$lib/state/recipients.svelte';
  import { ui } from '$lib/state/ui.svelte';
  import { voice } from '$lib/state/voice.svelte';

  let rooms       = $state<Room[]>([]);
  let activeId    = $state<string | null>(null);
  let agents      = $state<RoomAgent[]>([]);
  let posts       = $state<Post[]>([]);
  let banner      = $state<string>('');
  let composerText = $state<string>('');

  const ws = new RoomWs();
  let unsubWs: (() => void) | null = null;
  let unsubVoice: (() => void) | null = null;

  // Route effect is the SINGLE writer of `activeId`. Sidebar clicks call
  // `gotoRoom()` which only updates the URL — the effect below then sees the
  // new $page.params.room and runs `loadRoom()`. Mixing direct state writes
  // with goto() in the same handler caused the prior flicker-and-revert
  // (effect re-ran with stale params snapshot and bounced back to the old
  // room).
  $effect(() => {
    const fromUrl = $page.params.room ? decodeURIComponent($page.params.room) : null;
    if (fromUrl && fromUrl !== activeId) loadRoom(fromUrl);
  });

  // Auto-pick the first room when landing on bare /focus. Separated from the
  // route effect so it doesn't depend on activeId.
  $effect(() => {
    if (!$page.params.room && !activeId && rooms.length) {
      goto(`${base}/focus/${encodeURIComponent(rooms[0].id)}`, { replaceState: true });
    }
  });

  // Track banner from WS state.
  $effect(() => { banner = ws.banner; });

  onMount(async () => {
    unsubWs = ws.on(handleEvent);
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
    // Effect handles initial navigation — nothing else to do here.
  });

  onDestroy(() => {
    unsubWs?.();
    ws.close();
    unsubVoice?.();
  });

  async function refreshRooms() {
    try { rooms = (await listRooms('active')).rooms; }
    catch (e) { banner = (e as Error).message; rooms = []; }
  }

  function gotoRoom(roomId: string) {
    if (roomId === activeId) return;
    // Just navigate; the route effect picks it up. Don't touch state here.
    goto(`${base}/focus/${encodeURIComponent(roomId)}`);
    ui.closeAll();
  }

  async function loadRoom(roomId: string) {
    activeId = roomId;
    posts = [];
    composerText = '';
    banner = `attaching to ${roomId}…`;
    await refreshAgents(roomId);
    // WS sends a `history` frame right after attach (rooms.py:770) — that's
    // our source of truth, so no separate REST history fetch here.
    ws.connect(roomId);
  }

  async function refreshAgents(roomId: string) {
    try { agents = (await getRoomAgents(roomId)).agents; }
    catch (e) { banner = (e as Error).message; agents = []; }
  }

  function handleEvent(ev: RoomEvent) {
    switch (ev.type) {
      case 'history':
        if ('posts' in ev && Array.isArray(ev.posts)) posts = ev.posts;
        break;
      case 'post':
        if ('post' in ev) posts = [...posts, ev.post as Post];
        break;
      case 'participant_joined':
      case 'participant_left':
        if (activeId) refreshAgents(activeId);
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
    } catch (e) { banner = (e as Error).message; }
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
      <button class="sheet-toggle" type="button" aria-label="rooms" onclick={() => ui.openLeft()}>≡</button>
      <span class="title mono">{activeRoom?.id ?? 'no room focused'}</span>
      <span class="topic">{activeRoom?.topic ?? ''}</span>
      <span class="banner mono">{banner}</span>
      <span class="spacer"></span>
      <VoicePanel />
      <button class="sheet-toggle right" type="button" aria-label="details" onclick={() => ui.openRight()}>@</button>
    </header>

    {#if activeId}
      <Transcript {posts} />
      <Composer disabled={!activeId} bind:text={composerText} onsubmit={onSend} onslashopen={() => ui.slashOpen = !ui.slashOpen} />
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
        onagentresult={(m) => banner = m}
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
        onagentresult={(m) => banner = m}
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
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text2);
    font-family: var(--mono);
    font-size: 18px;
    line-height: 1;
    width: 36px;
    height: 36px;
    border-radius: 4px;
    cursor: pointer;
  }
  .sheet-toggle:hover { background: var(--surface3); }
  .sheet-toggle.right { margin-left: auto; }

  .ptt-row { padding: 10px 14px 14px; background: var(--surface); border-top: 1px solid var(--border); }
  .dim     { color: var(--text3); }

  @media (max-width: 1024px) {
    .focus, .focus.with-details { grid-template-columns: 1fr; }
    .left, .right { display: none; }
    .sheet-toggle { display: inline-flex; align-items: center; justify-content: center; }
  }
  @media (max-width: 720px) {
    .rh { padding: 8px 10px; }
    .ptt-row { padding: 8px 10px 10px; }
  }
</style>
