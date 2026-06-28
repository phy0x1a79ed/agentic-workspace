<script lang="ts">
  /**
   * Right panel — the selected task's live conversation.
   *
   * Reuses the same chat substrate as @awm/agent-chat, pointed at a PLACEMENT
   * rather than a scopes channel:
   *   - subscribeAgent(workspace_slug) — the task's transcript (the
   *     workspace_slug from orch_status IS the transcript key; one unit
   *     accumulates every leg's acts). Opening this WS is the latent "attached"
   *     signal (the agents service mirrors it to orch.set_attached); closing it
   *     on deselect clears it. Attach is otherwise passive (T2).
   *   - TranscriptFold — partials → one growing bubble, dedupe by act id.
   *   - <TtsHistory> — the transcript view.
   * Human input goes through enqueueAgentPost (→ enqueue_input), NOT postToScope:
   * a placement has no scopes channel, so a message must splice straight into the
   * agent's stdin. The reply rides the agent's next turn back through the stream.
   */
  import { untrack } from 'svelte';
  import {
    subscribeAgent,
    enqueueAgentPost,
    type AgentSubscription,
    type AgentStreamEvent,
  } from '@awm/client';
  import { TtsHistory } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { TranscriptFold, agentAuthor } from '@awm/agent-chat';
  import { TextTab } from '@awm/stt-composer';

  interface Props {
    /** The task's workspace unit slug — the agents transcript key (and identity). */
    workspaceSlug: string;
    /** Goal text for the header. */
    goal?: string;
  }
  let { workspaceSlug, goal }: Props = $props();

  let posts = $state<Post[]>([]);
  let humanPosts = $state<Post[]>([]);
  let error = $state<string | null>(null);
  let sending = $state(false);
  let tab = $state<ReturnType<typeof TextTab> | null>(null);

  let fold: TranscriptFold | null = null;
  let sub: AgentSubscription | null = null;

  function rebuild() {
    const agentPosts = fold ? fold.posts : [];
    posts = [...humanPosts, ...agentPosts].sort((a, b) =>
      (a.ts ?? '') < (b.ts ?? '') ? -1 : (a.ts ?? '') > (b.ts ?? '') ? 1 : 0,
    );
  }

  function onAgentEvent(ev: AgentStreamEvent) {
    if (!fold) return;
    if (ev.type === 'backfill') {
      for (const act of ev.acts) fold.push(act);
      rebuild();
    } else if (ev.type === 'act') {
      fold.push(ev.act);
      rebuild();
    } else if (ev.type === 'lagged') {
      reopen();
    } else if (ev.type === 'error') {
      error = ev.message;
    }
  }

  function openStream() {
    sub?.close();
    const cursor = fold ? { after_ts: fold.lastTs, after_id: fold.lastId } : {};
    sub = subscribeAgent(workspaceSlug, cursor, onAgentEvent);
  }

  function reopen() {
    if (workspaceSlug) openStream();
  }

  /** (Re)attach whenever the selected task changes. */
  $effect(() => {
    // Track the identity; reattach on change.
    const w = workspaceSlug;
    untrack(() => {
      sub?.close();
      sub = null;
      fold = new TranscriptFold(agentAuthor(w));
      humanPosts = [];
      posts = [];
      error = null;
    });
    if (w) openStream();
    return () => {
      sub?.close();
      sub = null;
    };
  });

  async function send() {
    const text = tab?.consumeText?.() ?? '';
    const t = text.trim();
    if (!t || sending || !workspaceSlug) return;
    sending = true;
    try {
      await enqueueAgentPost(workspaceSlug, t, 'you');
      // Optimistic echo — the page doesn't subscribe to the agent's stdin, so
      // our own message won't stream back; append it to the human bucket.
      humanPosts = [
        ...humanPosts,
        { id: `you-${Date.now()}`, ts: new Date().toISOString(), author: 'you', kind: 'text', body: t },
      ];
      rebuild();
      tab?.clear?.();
    } catch (err) {
      error = `send failed: ${(err as Error).message}`;
    } finally {
      sending = false;
    }
  }
</script>

<div class="task-chat">
  <header class="hdr">
    <span class="goal mono">{goal || workspaceSlug}</span>
    <span class="key mono">{workspaceSlug}</span>
  </header>

  {#if error}<p class="error mono">{error}</p>{/if}

  <div class="history-wrap">
    <TtsHistory {posts} />
  </div>

  <div class="composer">
    <TextTab bind:this={tab} disabled={sending} />
    <button class="send mono" onclick={send} disabled={sending}>send</button>
  </div>
</div>

<style>
  .task-chat {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }
  .hdr {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
  }
  .goal { font-size: 12px; color: var(--text, #ddd); }
  .key { font-size: 10px; color: var(--text3, #888); }
  .error {
    margin: 0;
    padding: 6px 12px;
    background: color-mix(in oklab, var(--warn, #f55) 14%, transparent);
    color: var(--warn, #f55);
    font-size: 12px;
    flex: 0 0 auto;
  }
  .history-wrap {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  :global(.history-wrap > .transcript) { flex: 1; }
  .composer {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    padding: 10px 12px;
    border-top: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    flex: 0 0 auto;
  }
  .send {
    flex: 0 0 auto;
    background: var(--surface2, #222);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    color: var(--text2, #bbb);
    padding: 8px 14px;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    cursor: pointer;
  }
  .send:hover:not(:disabled) {
    border-color: var(--atomizer, #ffb74d);
    color: var(--text, #ddd);
  }
  .send:disabled { opacity: 0.5; cursor: default; }
  .mono { font-family: var(--mono, monospace); }
</style>
