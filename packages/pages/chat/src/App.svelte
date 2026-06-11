<script lang="ts">
  /**
   * Text-only human ↔ agent chat page.
   *
   * Reuses the shared room client from @awm/client (ensureRoom + RoomAttach +
   * postText) and the @awm/tts-history transcript view — but with no voice
   * coupling: no <PttComposer>, no auto-speak, and <TtsHistory> is rendered
   * without `onspeak` so no TTS replay buttons appear. The only page-local UI
   * is a connect bar (project/scope) and a plain text composer.
   *
   * Room resolution: ?project=…&scope=… from the URL (with localStorage
   * fallback); the connect bar lets the operator change it in-page.
   */
  import { onMount, onDestroy } from 'svelte';
  import { TtsHistory } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { Button, Input } from '@awm/primitives';
  import { RoomAttach, ensureRoom, postText } from '@awm/client';
  import type { RoomEvent } from '@awm/client';

  const KEY_PROJECT = 'awm.chat.project';
  const KEY_SCOPE = 'awm.chat.scope';

  function readUrlOrStorage(): { project: string; scope: string } {
    const params = new URLSearchParams(window.location.search);
    const project = params.get('project') ?? localStorage.getItem(KEY_PROJECT) ?? '';
    const scope = params.get('scope') ?? localStorage.getItem(KEY_SCOPE) ?? '';
    return { project, scope };
  }

  const initial = readUrlOrStorage();
  let project = $state(initial.project);
  let scope = $state(initial.scope);
  let scopeIdent = $derived(project.trim() && scope.trim() ? `${project.trim()}/${scope.trim()}` : '');

  let posts = $state<Post[]>([]);
  let roomId = $state<string | null>(null);
  let attach: RoomAttach | null = null;
  let error = $state<string | null>(null);
  let connecting = $state(false);
  let sending = $state(false);
  let draft = $state('');

  function onRoomEvent(ev: RoomEvent) {
    if (ev.type === 'history') {
      posts = (ev.posts ?? []) as Post[];
    } else if (ev.type === 'post') {
      posts = [...posts, ev.post as Post];
    } else if (ev.type === 'room_closed') {
      error = 'room closed';
    } else if (ev.type === 'error') {
      error = ev.message;
    }
  }

  async function connect() {
    error = null;
    if (!scopeIdent) { error = 'enter a project and scope'; return; }
    localStorage.setItem(KEY_PROJECT, project.trim());
    localStorage.setItem(KEY_SCOPE, scope.trim());
    attach?.close();
    attach = null;
    posts = [];
    roomId = null;
    connecting = true;
    try {
      roomId = await ensureRoom(scopeIdent);
      attach = new RoomAttach(roomId, onRoomEvent);
    } catch (err) {
      error = (err as Error).message;
    } finally {
      connecting = false;
    }
  }

  async function send() {
    const text = draft.trim();
    if (!roomId || !text || sending) return;
    sending = true;
    try {
      await postText(roomId, text);
      draft = '';
    } catch (err) {
      error = (err as Error).message;
    } finally {
      sending = false;
    }
  }

  function onComposerKeydown(e: KeyboardEvent) {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  onMount(() => { if (scopeIdent) void connect(); });
  onDestroy(() => { attach?.close(); });
</script>

<main class="app">
  <header class="hdr">
    <div class="connect">
      <Input bind:value={project} placeholder="project" />
      <span class="slash">/</span>
      <Input bind:value={scope} placeholder="scope" />
      <Button kind="primary" disabled={connecting || !scopeIdent} onclick={connect}>
        {connecting ? 'connecting…' : roomId ? 'reconnect' : 'connect'}
      </Button>
    </div>
    {#if roomId}<span class="room mono">room {roomId.slice(0, 8)}</span>{/if}
  </header>

  {#if error}<p class="error">{error}</p>{/if}

  <div class="history-wrap">
    {#if roomId}
      <TtsHistory {posts} />
    {:else}
      <p class="empty">connect to a scope to start chatting</p>
    {/if}
  </div>

  <div class="composer-wrap">
    <textarea
      class="composer"
      bind:value={draft}
      onkeydown={onComposerKeydown}
      placeholder={roomId ? 'message… (Enter to send, Shift+Enter for newline)' : 'connect first'}
      disabled={!roomId || sending}
      rows="2"
    ></textarea>
    <Button kind="primary" disabled={!roomId || sending || !draft.trim()} onclick={send}>
      {sending ? 'sending…' : 'send'}
    </Button>
  </div>
</main>

<style>
  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    max-width: 760px;
    margin: 0 auto;
  }
  .hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-size: 11px;
  }
  .connect {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1 1 auto;
  }
  .connect :global(.input) { width: auto; flex: 1 1 0; min-width: 80px; }
  .slash { color: var(--text3); }
  .room { color: var(--text3); white-space: nowrap; }
  .error {
    margin: 0;
    padding: 6px 14px;
    background: color-mix(in oklab, var(--warn) 14%, transparent);
    color: var(--warn);
    font-size: 12px;
    font-family: var(--mono);
  }
  .history-wrap {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  :global(.history-wrap > .transcript) { flex: 1; }
  .empty {
    margin: auto;
    color: var(--text3);
    font-family: var(--mono);
    font-size: 12px;
  }
  .composer-wrap {
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    align-items: flex-end;
    gap: 8px;
  }
  .composer {
    flex: 1 1 auto;
    resize: none;
    padding: var(--space-2) var(--space-4);
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    outline: none;
  }
  .composer:focus { border-color: var(--atomizer); }
  .composer::placeholder { color: var(--text3); }
  .composer:disabled { opacity: 0.5; }
  .mono { font-family: var(--mono); }
</style>
