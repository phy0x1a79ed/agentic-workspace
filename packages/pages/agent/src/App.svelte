<script lang="ts">
  /**
   * Rudimentary single-agent convo page.
   *
   * Composes:
   *   <TtsHistory />  from @awm/tts-history — chat transcript with per-row
   *                   replay buttons backed by the /svc/tts service.
   *   <PttComposer /> from @awm/ptt-composer — text + mic composer backed
   *                   by the /svc/ptt service.
   *
   * Wires both against /rooms: text posts hit POST /rooms/{id}/posts,
   * incoming posts arrive via WS /rooms/{id}/attach, and any agent post
   * auto-fires the speak handler so the user hears agent replies without
   * needing to click the replay button.
   *
   * Room resolution: read ?project=…&scope=… from the URL (with
   * localStorage fallback), call ensureRoom(scopeIdent) which either
   * adopts the most recent active room for that scope or creates one.
   */
  import { onMount, onDestroy } from 'svelte';
  import { TtsHistory, playOnce } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { PttComposer } from '@awm/ptt-composer';
  import { RoomAttach, ensureRoom, postText } from './lib/api/rooms';
  import type { RoomEvent } from './lib/api/rooms';

  // --- URL → scope identifier ---------------------------------------------

  const KEY_PROJECT = 'awm.agent.project';
  const KEY_SCOPE = 'awm.agent.scope';

  function readUrlOrStorage(): { project: string; scope: string } {
    const params = new URLSearchParams(window.location.search);
    const project = params.get('project') ?? localStorage.getItem(KEY_PROJECT) ?? '';
    const scope = params.get('scope') ?? localStorage.getItem(KEY_SCOPE) ?? '';
    if (project) localStorage.setItem(KEY_PROJECT, project);
    if (scope) localStorage.setItem(KEY_SCOPE, scope);
    return { project, scope };
  }

  let { project, scope } = readUrlOrStorage();
  let scopeIdent = $derived(project && scope ? `${project}/${scope}` : '');

  // --- Reactive state ----------------------------------------------------

  let posts = $state<Post[]>([]);
  let roomId = $state<string | null>(null);
  let attach: RoomAttach | null = null;
  let error = $state<string | null>(null);
  let sending = $state(false);

  // Auto-speak settings: TTS engine + params. The convo uses one synth
  // for every agent reply; the playground page picks engines/voices.
  // Here we keep it minimal — let the service pick its default by
  // sending {} params; the backend resolves the operator's last-used.
  let autoSpeak = $state(true);
  let speakingIds = $state<Set<string>>(new Set());

  function postKey(p: Post): string {
    return String(p.id ?? `${p.ts}-${p.author}`);
  }

  function isAgentPost(p: Post): boolean {
    return (p.author ?? '').startsWith('agent:');
  }

  async function speak(p: Post): Promise<void> {
    const k = postKey(p);
    if (speakingIds.has(k)) return;
    speakingIds = new Set([...speakingIds, k]);
    try {
      await playOnce(p.body ?? '', '', {});
    } catch (err) {
      console.warn('tts playback failed', err);
    } finally {
      const next = new Set(speakingIds);
      next.delete(k);
      speakingIds = next;
    }
  }

  function onRoomEvent(ev: RoomEvent) {
    if (ev.type === 'history') {
      posts = ev.posts ?? [];
    } else if (ev.type === 'post') {
      posts = [...posts, ev.post];
      if (autoSpeak && isAgentPost(ev.post) && (ev.post.kind ?? 'text') === 'text') {
        void speak(ev.post);
      }
    } else if (ev.type === 'participant_joined' || ev.type === 'participant_left') {
      // The /rooms WS already publishes membership posts in the history
      // stream when relevant; the participant_* frames are bookkeeping.
    } else if (ev.type === 'room_closed') {
      error = 'room closed';
    } else if (ev.type === 'error') {
      error = ev.message;
    }
  }

  async function bootstrap() {
    if (!scopeIdent) {
      error = 'missing ?project=…&scope=…';
      return;
    }
    try {
      roomId = await ensureRoom(scopeIdent);
    } catch (err) {
      error = (err as Error).message;
      return;
    }
    attach = new RoomAttach(roomId, onRoomEvent);
  }

  async function send(text: string) {
    if (!roomId || !text.trim() || sending) return;
    sending = true;
    try {
      await postText(roomId, text);
    } catch (err) {
      error = (err as Error).message;
    } finally {
      sending = false;
    }
  }

  // PTT auto-finalize → post: convo mode emits onText on every silence
  // segment. Hooking it here means dictated utterances post immediately
  // without the user touching SEND.
  function onPttText(text: string) {
    void send(text);
  }

  onMount(() => { void bootstrap(); });
  onDestroy(() => { attach?.close(); });
</script>

<main class="app">
  <header class="hdr">
    <div class="who">
      {#if scopeIdent}
        <span class="scope mono">{scopeIdent}</span>
      {:else}
        <span class="warn">no scope — add <code>?project=…&amp;scope=…</code></span>
      {/if}
      {#if roomId}<span class="room mono">· room {roomId.slice(0, 8)}</span>{/if}
    </div>
    <label class="speak-toggle">
      <input type="checkbox" bind:checked={autoSpeak} /> auto-speak
    </label>
  </header>

  {#if error}<p class="error">{error}</p>{/if}

  <div class="history-wrap">
    <TtsHistory {posts} onspeak={speak} />
  </div>

  <div class="composer-wrap">
    <PttComposer onsend={send} onText={onPttText} />
  </div>
</main>

<style>
  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    max-width: 720px;
    margin: 0 auto;
  }
  .hdr {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    font-size: 11px;
  }
  .scope { color: var(--atomizer); letter-spacing: 0.3px; }
  .room { color: var(--text3); margin-left: 6px; }
  .warn { color: var(--warn); }
  .speak-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--text3);
    font-family: var(--mono);
    cursor: pointer;
  }
  .speak-toggle input { margin: 0; }
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
  .composer-wrap {
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    justify-content: center;
  }
  code { font-family: var(--mono); font-size: 11px; }
  .mono { font-family: var(--mono); }
</style>
