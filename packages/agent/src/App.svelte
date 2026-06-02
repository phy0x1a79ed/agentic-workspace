<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { ChatHistory, ChatInput } from '@awm/chat-primitives';
  import type { Post } from '@awm/chat-primitives';
  import PttButton from '../../ptt/components/PttButton.svelte';
  import { ensureRoom, postText, runAgentSlash, getRoomAgent, RoomAttach } from './api/rooms';
  import type { RoomEvent } from './api/rooms';
  import { getStt } from './api/stt';
  import * as tts from './api/tts';

  // ---------- scope binding ----------
  // ?project=awm&scope=test-awm-agent. Persisted to localStorage so a bare
  // /agent/ reload sticks to whatever you last opened.
  const params = new URLSearchParams(window.location.search);
  const project = params.get('project') || localStorage.getItem('awm.agent.lastProject') || '';
  const scopeName = params.get('scope')   || localStorage.getItem('awm.agent.lastScope')   || '';
  if (project && scopeName) {
    localStorage.setItem('awm.agent.lastProject', project);
    localStorage.setItem('awm.agent.lastScope', scopeName);
  }
  const scopeIdent = project && scopeName ? `${project}/${scopeName}` : '';

  // Default model — haiku for fast/cheap chat. Override via ?model=opus etc.
  // Empty/"keep" leaves whatever the hub spawned with untouched.
  const desiredModel = params.get('model') ?? 'haiku';

  // ---------- chat state ----------
  let roomId      = $state<string>('');
  let posts       = $state<Post[]>([]);
  let composerText = $state<string>('');
  let slashOpen   = $state<boolean>(false);
  let banner      = $state<string>('');
  let pttBusy     = $state<boolean>(false);

  let attach: RoomAttach | null = null;
  const stt = getStt();
  let pendingPartial = '';   // text inserted into the composer during a press
  let baseBeforePartial = ''; // composer text before this press started

  // ---------- boot ----------
  onMount(async () => {
    if (!scopeIdent) {
      banner = 'no scope — open /agent/?project=<p>&scope=<s>';
      return;
    }
    try {
      // V1: always reuse the most recent active room for this scope so a
      // refresh keeps you in-conversation. localStorage echo is a hint for
      // observability; ensureRoom is the source of truth.
      roomId = await ensureRoom(scopeIdent);
      localStorage.setItem(`awm.agent.lastRoom.${scopeIdent}`, roomId);
      attach = new RoomAttach(roomId, handleEvent);
      // Best-effort model alignment. Only respawn when the live model
      // disagrees with the desired one (substring match — claude returns
      // full ids like "claude-haiku-4-5-20251001").
      if (desiredModel && desiredModel !== 'keep') {
        try {
          const live = await getRoomAgent(roomId, scopeIdent);
          const cur = (live?.model ?? '').toLowerCase();
          if (!cur.includes(desiredModel.toLowerCase())) {
            await runAgentSlash(roomId, scopeIdent, `/model ${desiredModel}`);
          }
        } catch { /* non-fatal — proceed without enforcing */ }
      }
    } catch (e) {
      banner = (e as Error).message;
    }
  });

  onDestroy(() => {
    attach?.close();
    stt.dispose();
  });

  // ---------- WS event handling ----------
  function handleEvent(ev: RoomEvent) {
    switch (ev.type) {
      case 'history':
        posts = ev.posts;
        break;
      case 'post':
        posts = [...posts, ev.post];
        break;
      case 'room_closed':
        banner = 'room closed';
        break;
      case 'error':
        banner = `ws error: ${ev.message}`;
        break;
      default:
        // participant_joined/left/lagged/pong — ignored for V1
        break;
    }
  }

  // ---------- composer wiring ----------
  async function onSend(text: string) {
    if (!roomId) return;
    try {
      await postText(roomId, text);
    } catch (e) {
      banner = (e as Error).message;
    }
  }

  async function onSlashPick(cmd: string, autorun: boolean) {
    if (!roomId || !scopeIdent || !autorun) return;
    try {
      const res = await runAgentSlash(roomId, scopeIdent, cmd);
      if (res.result) banner = res.result;
    } catch (e) {
      banner = (e as Error).message;
    }
  }

  // ---------- PTT wiring ----------
  let stopPartialSub: (() => void) | null = null;

  async function onPttDown() {
    if (!roomId || pttBusy) return;
    pttBusy = true;
    banner = '';
    baseBeforePartial = composerText;
    pendingPartial = '';
    stopPartialSub = stt.onpartial((text) => {
      pendingPartial = text;
      const sep = baseBeforePartial && !/\s$/.test(baseBeforePartial) ? ' ' : '';
      composerText = baseBeforePartial + sep + pendingPartial;
    });
    try {
      await stt.start();
    } catch (e) {
      banner = `mic/ws: ${(e as Error).message}`;
      pttBusy = false;
      stopPartialSub?.();
      stopPartialSub = null;
    }
  }

  async function onPttUp() {
    if (!pttBusy) return;
    try {
      const final = await stt.end();
      const sep = baseBeforePartial && !/\s$/.test(baseBeforePartial) ? ' ' : '';
      composerText = (baseBeforePartial + sep + final).trim();
    } catch (e) {
      banner = `stt: ${(e as Error).message}`;
    } finally {
      pttBusy = false;
      stopPartialSub?.();
      stopPartialSub = null;
      pendingPartial = '';
      baseBeforePartial = '';
    }
  }

  // ---------- TTS speak ----------
  async function onSpeak(p: Post) {
    try {
      await tts.play(p.body ?? '');
    } catch (e) {
      banner = `tts: ${(e as Error).message}`;
    }
  }
</script>

<main class="agent">
  <header class="head">
    <span class="title mono">awm/agent</span>
    {#if scopeIdent}
      <span class="scope mono">▸ {scopeIdent}</span>
    {/if}
    {#if roomId}
      <span class="room mono">· room {roomId.slice(0, 8)}</span>
    {/if}
  </header>

  {#if banner}
    <div class="banner mono">{banner}</div>
  {/if}

  <ChatHistory
    {posts}
    onspeak={onSpeak}
  />

  <ChatInput
    disabled={!roomId}
    bind:text={composerText}
    bind:slashOpen
    roomId={roomId || null}
    scope={scopeIdent || null}
    onsubmit={onSend}
    onslashpick={onSlashPick}
  />

  <div class="ptt-row">
    <PttButton
      disabled={!roomId}
      onpttdown={onPttDown}
      onpttup={onPttUp}
    />
  </div>
</main>

<style>
  .agent {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg);
  }
  .head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-size: 11px;
  }
  .title { color: var(--atomizer); letter-spacing: 0.5px; }
  .scope { color: var(--text2); }
  .room  { color: var(--text3); }
  .mono  { font-family: var(--mono); }
  .banner {
    background: color-mix(in oklab, var(--warn) 12%, var(--surface));
    color: var(--warn);
    padding: 6px 14px;
    font-size: 11px;
    border-bottom: 1px solid var(--border);
  }
  .ptt-row {
    padding: 10px 14px 14px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: center;
  }
</style>
