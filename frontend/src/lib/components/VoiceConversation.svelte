<script lang="ts">
  /**
   * VoiceConversation — composes ChatHistory + ChatInput + PttButton over a
   * single room. Owns the WS subscription for that room and (optionally) the
   * STT→composer bridge and auto-speak playback for incoming agent posts.
   *
   * Used by both ``routes/focus`` and the ``/status`` Voice control
   * panel so the conversation flow is identical in both surfaces.
   */
  import { onMount, onDestroy } from 'svelte';
  import {
    getRoomAgents, postToRoom, runAgentSlash,
    type Post, type RoomAgent,
  } from '$lib/api/client';
  import { RoomWsPool, type RoomEvent } from '$lib/api/ws.svelte';
  import ChatHistory from './ChatHistory.svelte';
  import ChatInput   from './ChatInput.svelte';
  // PttButton lives in the @awm/ptt stripe at packages/ptt/components/PttButton.svelte.
  // The SvelteKit `$lib` alias does not reach outside `src/lib/`, so the
  // import uses a relative path back into the workspace package tree.
  import PttButton   from '../../../../packages/ptt/components/PttButton.svelte';
  import { voice } from '$lib/state/voice.svelte';
  import { replay as ttsReplay } from '$lib/voice/tts';

  interface Props {
    roomId: string;
    /** Scope to address by default. Empty → broadcast to room. */
    toScope?: string | null;
    /** Manager scope used as slash-command recipient. */
    managerScope?: string | null;
    /** When true, agent text posts are auto-played via TTS. */
    autoSpeak?: boolean;
    /** When true, STT results are dropped into the composer and auto-sent. */
    sttIntoComposer?: boolean;
    /** When false, ChatInput will only echo locally (no /rooms/{id}/posts). */
    sendToRoom?: boolean;
    /** Surface errors / slash result strings to the parent. */
    onresult?: (msg: string) => void;
    /** Surface agent inventory once it loads (used for "manager" label). */
    onagents?: (agents: RoomAgent[]) => void;
  }
  let {
    roomId,
    toScope = null,
    managerScope = null,
    autoSpeak = false,
    sttIntoComposer = true,
    sendToRoom = true,
    onresult,
    onagents,
  }: Props = $props();

  let posts        = $state<Post[]>([]);
  let composerText = $state<string>('');
  let slashOpen    = $state<boolean>(false);

  const pool = new RoomWsPool();
  let wsUnsub: (() => void) | null = null;
  let voiceUnsub: (() => void) | null = null;
  let lastSpokenTs = '';

  $effect(() => {
    // Tear down whenever roomId changes; re-attach for the new room.
    posts = [];
    composerText = '';
    lastSpokenTs = '';
    wsUnsub?.();
    if (!roomId) return;
    wsUnsub = pool.on(roomId, handleEvent);
    refreshAgents(roomId);
    return () => {
      wsUnsub?.();
      wsUnsub = null;
      pool.detach(roomId);
    };
  });

  onMount(() => {
    if (sttIntoComposer) {
      voiceUnsub = voice.onResult((text) => {
        const cur = composerText;
        const sep = cur && !/\s$/.test(cur) ? ' ' : '';
        const next = (cur + sep + text).trim();
        composerText = next;
        // Speaking IS the send (parity with legacy autoSend semantics).
        onSend(next);
        composerText = '';
      });
    }
  });

  onDestroy(() => {
    wsUnsub?.();
    voiceUnsub?.();
    pool.closeAll();
  });

  function handleEvent(ev: RoomEvent) {
    switch (ev.type) {
      case 'history':
        if ('posts' in ev && Array.isArray(ev.posts)) {
          posts = ev.posts;
          // Don't speak posts present at attach time — only fresh ones.
          if (posts.length) lastSpokenTs = posts[posts.length - 1].ts ?? '';
        }
        break;
      case 'post':
        if ('post' in ev) {
          const p = ev.post as Post;
          posts = [...posts, p];
          if (autoSpeak && isAgentText(p) && (p.ts ?? '') > lastSpokenTs) {
            lastSpokenTs = p.ts ?? '';
            ttsReplay(p.body ?? '');
          }
        }
        break;
      case 'participant_joined':
      case 'participant_left':
        refreshAgents(roomId);
        break;
    }
  }

  function isAgentText(p: Post): boolean {
    if ((p.kind ?? 'text') !== 'text') return false;
    return (p.author ?? '').startsWith('agent:');
  }

  async function refreshAgents(rid: string) {
    if (!onagents) return;
    try {
      const r = await getRoomAgents(rid);
      onagents(r.agents);
    } catch { /* surface via parent if it cares */ }
  }

  async function onSend(text: string) {
    if (!roomId || !text) return;
    if (!sendToRoom) {
      // Local-only echo so user can validate STT/composer without a real post.
      posts = [...posts, {
        ts: new Date().toISOString(),
        author: 'user:local',
        body: text,
        kind: 'text',
      } as Post];
      return;
    }
    try {
      await postToRoom(roomId, toScope ? { body: text, to_scope: toScope } : { body: text });
    } catch (e) {
      onresult?.((e as Error).message);
    }
  }

  async function onSlashPick(cmd: string, _autorun: boolean) {
    if (!roomId) return;
    const scope = managerScope;
    if (!scope) { onresult?.('no manager scope'); return; }
    try {
      const r = await runAgentSlash(roomId, scope, cmd);
      if (r.result) onresult?.(r.result);
    } catch (e) {
      onresult?.((e as Error).message);
    }
  }
</script>

<div class="vconv">
  <ChatHistory {posts} />
  <ChatInput
    disabled={!roomId}
    bind:text={composerText}
    bind:slashOpen
    {roomId}
    scope={managerScope}
    onsubmit={onSend}
    onslashpick={onSlashPick}
  />
  <div class="ptt-row">
    <PttButton
      disabled={!roomId}
      onpttdown={() => voice.start()}
      onpttup={() => voice.end()}
    />
  </div>
</div>

<style>
  .vconv {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    overflow: hidden;
  }
  .ptt-row {
    padding: 10px 14px 14px;
    background: var(--surface);
    border-top: 1px solid var(--border);
  }
  @media (max-width: 720px) {
    .ptt-row { padding: 8px 10px 10px; }
  }
</style>
