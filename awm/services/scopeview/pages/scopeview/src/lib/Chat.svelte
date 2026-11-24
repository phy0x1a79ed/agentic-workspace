<script lang="ts">
  /** The conversation with the study's curator: a long poll on its room while this pane is mounted. */
  import * as api from './api';

  interface Props {
    peer: string;
    project: string;
    scope: string;
    /** What a sent message is about; attached unless the chip is switched off. */
    context: Record<string, string>;
    /** The curator wrote, so the scope may have changed under the page. */
    onhost: () => void;
  }
  let { peer, project, scope, context, onhost }: Props = $props();

  let messages = $state.raw<api.ChatMessage[]>([]);
  let status = $state<'loading' | 'open' | 'unseated' | 'ended' | 'error'>('loading');
  let error = $state<string | null>(null);
  let attempt = $state(0);
  let draft = $state('');
  let attach = $state(true);
  let sending = $state(false);
  let list: HTMLDivElement;

  const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

  $effect(() => {
    void attempt;
    const study = { peer, project, scope };
    let live = true;
    let all: api.ChatMessage[] = [];
    messages = [];
    status = 'loading';
    error = null;
    (async () => {
      try {
        for (let after = 0, backlog = true; live; backlog = false) {
          const r = await api.chat(study, after, backlog ? 0 : 25);
          if (!live) return;
          after = r.next;
          if (r.messages.length) {
            messages = all = [...all, ...r.messages];
            if (!backlog && r.messages.some((m) => m.by === 'host')) onhost();
          }
          status = r.ended ? 'ended' : 'open';
          if (r.ended) return;
        }
      } catch (e) {
        if (!live) return;
        error = message(e);
        status = /no curator is seated/.test(error) ? 'unseated' : 'error';
      }
    })();
    return () => {
      live = false;
    };
  });

  $effect(() => {
    void messages;
    list.scrollTop = list.scrollHeight;
  });

  const about = $derived(Object.entries(context).map(([k, v]) => `${k}=${v}`).join(' '));

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    sending = true;
    try {
      await api.say({ peer, project, scope }, text, attach ? context : {});
      draft = '';
      error = null;
    } catch (e) {
      error = message(e);
    } finally {
      sending = false;
    }
  }

  /** A page-sent message opens with its context as `[k=v ...]` on a line of its own. */
  const parts = (m: api.ChatMessage) => {
    const tagged = m.by === 'guest' ? /^\[([^\]\n]*)\]\n([\s\S]*)$/.exec(m.text) : null;
    return tagged ? { about: tagged[1], text: tagged[2] } : { about: '', text: m.text };
  };
</script>

<div class="chat">
  <h2>Curator <span>{project}/{scope}</span></h2>
  <div class="list" bind:this={list}>
    {#each messages as m (m.seq)}
      {@const p = parts(m)}
      <div class="msg {m.by}">
        <span class="from">{m.by === 'guest' ? 'you' : m.from || 'curator'}</span>
        {#if p.about}<span class="about" title={p.about}>{p.about}</span>{/if}
        <p>{p.text}</p>
      </div>
    {/each}
    {#if status === 'loading'}
      <p class="state">reading the room…</p>
    {:else if status === 'unseated'}
      <p class="state">No curator is seated in {project}/{scope}.</p>
      <button class="retry" onclick={() => attempt++}>look again</button>
    {:else if status === 'ended'}
      <p class="state">The curator closed this room.</p>
      <button class="retry" onclick={() => attempt++}>look again</button>
    {:else if status === 'error'}
      <p class="state error">{error}</p>
      <button class="retry" onclick={() => attempt++}>reconnect</button>
    {:else if !messages.length}
      <p class="state">No messages yet.</p>
    {/if}
  </div>
  {#if status === 'open'}
    {#if error}<p class="state error">{error}</p>{/if}
    <form onsubmit={(e) => (e.preventDefault(), send())}>
      {#if about}
        <button type="button" class="about chip" class:off={!attach} aria-pressed={attach} onclick={() => (attach = !attach)} title="Sent with the message. Click to {attach ? 'leave it out' : 'attach it'}.">
          {about}
        </button>
      {/if}
      <textarea
        bind:value={draft}
        rows="3"
        placeholder="Ask the curator. Enter sends, Shift+Enter breaks the line."
        onkeydown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), send())}
      ></textarea>
    </form>
  {/if}
</div>

<style>
  .chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
  h2 { flex: none; margin: 0; padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--border); font-family: var(--mono); font-size: 10px; font-weight: 400; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); }
  h2 span { color: var(--text3); text-transform: none; letter-spacing: 0; margin-left: var(--space-2); }
  .list { flex: 1; min-height: 0; overflow: auto; padding: var(--space-3) var(--space-4); display: flex; flex-direction: column; gap: var(--space-4); }
  .msg { max-width: 92%; padding: var(--space-2) var(--space-3); border-radius: var(--radius-lg); background: var(--surface2); align-self: flex-start; }
  .msg.guest { align-self: flex-end; background: var(--surface3); }
  .msg p { margin: var(--space-0) 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.45; }
  .from { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); }
  .msg.host .from { color: var(--atomizer); }
  .about { display: block; font-family: var(--mono); font-size: 10px; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .state { margin: 0; color: var(--text2); }
  .chat > .state { padding: 0 var(--space-4) var(--space-2); }
  .error { color: var(--danger); overflow-wrap: anywhere; }
  .retry { all: unset; cursor: pointer; align-self: flex-start; padding: var(--space-0) var(--space-3); border: 1px solid var(--border2); border-radius: var(--radius-md); font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--text2); }
  .retry:hover { color: var(--text); background: var(--surface2); }
  form { flex: none; display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3) var(--space-4); border-top: 1px solid var(--border); }
  .chip { all: unset; cursor: pointer; display: block; box-sizing: border-box; max-width: 100%; padding: var(--space-0) var(--space-3); border: 1px solid var(--border2); border-radius: 10px; font-family: var(--mono); font-size: 10px; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; align-self: flex-start; }
  .chip.off { text-decoration: line-through; color: var(--text3); border-style: dashed; }
  textarea { width: 100%; resize: vertical; padding: var(--space-2) var(--space-3); background: var(--bg); color: var(--text); border: 1px solid var(--border2); border-radius: var(--radius-md); font: inherit; }
  textarea:focus-visible, .chip:focus-visible, .retry:focus-visible { outline: 1px solid var(--atomizer); }
</style>
