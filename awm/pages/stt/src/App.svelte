<script lang="ts">
  // STT dev page. The real work (session against /svc/stt, mic worklet,
  // transcript chips, PTT/Convo modes) lives in @awm/stt-composer; the
  // standardized <Chat> composite (from @awm/chat) wraps that composer + the
  // <TtsHistory> transcript into one input+transcript widget, so this page only
  // supplies a data source (`posts` + `onSend`). Below it we render the dev
  // telemetry timeline (@awm/stt-telemetry) fed by Chat's `onTelemetry` tap, so
  // the STT scope can profile the pipeline while exercising a live session.
  import { Chat, type Post } from '@awm/chat';
  import { SttTelemetry, appendTelemetry, type TelemetryEvent } from '@awm/stt-telemetry';
  import { untrack } from 'svelte';

  let posts = $state<Post[]>([]);
  let seq = 0;

  // Dev telemetry: every STT pipeline event the composer surfaces, accumulated
  // (coalescing audio-chunk bursts, capped) for the timeline panel below it.
  //
  // The mutation MUST run under `untrack`: some telemetry events are emitted from
  // inside the composer's reactive `$effect`s (e.g. the context-push effect). The
  // signal graph is global, so reading+writing `telemetry` there would subscribe
  // that effect to `telemetry` and then re-trigger it on every event — a runaway
  // loop that floods the log and the WS. untrack severs that dependency; the write
  // still updates the panel's binding.
  let telemetry = $state<TelemetryEvent[]>([]);
  function onTelemetry(e: TelemetryEvent) {
    untrack(() => { telemetry = appendTelemetry(telemetry, e); });
  }

  // Recent chat history fed to the convo cleanup LLM as context. Capped on
  // the composer side; here we just surface the last handful of turns. Because
  // posted messages land in `posts`, they flow into this context too — the
  // cleanup model (when CONVO_REFINE is on) cleans your speech against the
  // live conversation.
  const chatContext = $derived(
    posts.slice(-20).map((p) => `${p.author}: ${p.body}`).join('\n'),
  );

  function add(author: string, body: string): string {
    const id = String(seq++);
    posts = [...posts, { id, ts: new Date().toISOString(), author, body }];
    return id;
  }

  // <Chat> calls this exactly once per user turn (the send-once gate lives in
  // Chat). We just land the user's message as a `you` row — there is no chat
  // partner (the stt service dropped its mock `chat` function; the agents
  // service is the real partner). Posted turns feed back into `chatContext`.
  function onUserMessage(text: string) {
    const t = text.trim();
    if (!t) return;
    add('you', t);
  }
</script>

<main>
  <header>
    <h1>PTT</h1>
    <p class="hint">
      Hold the mic button (or <kbd>SPACE</kbd>) to talk. Toggle between
      PTT and CONVO modes in the tabs above the editor.
    </p>
  </header>

  <div class="chat-host">
    <Chat {posts} onSend={onUserMessage} {chatContext} {onTelemetry} />
  </div>

  <section class="telemetry">
    <h2>Telemetry</h2>
    <SttTelemetry events={telemetry} onclear={() => (telemetry = [])} />
  </section>
</main>

<style>
  main {
    padding: 1.5rem;
    font-family: system-ui, sans-serif;
    max-width: 560px;
    margin: 0 auto;
    color: var(--text, #ddd);
    background: var(--bg, #111);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header { margin-bottom: 1rem; flex: 0 0 auto; }
  h1 { font-size: 1.2rem; letter-spacing: 0.05em; text-transform: uppercase; margin: 0 0 0.25rem; }
  h2 { font-size: 0.85rem; letter-spacing: 0.05em; text-transform: uppercase; margin: 1.5rem 0 0.5rem; color: var(--text2, #bbb); flex: 0 0 auto; }
  .hint { font-size: 0.85rem; color: var(--text3, #888); margin: 0; }
  .chat-host {
    flex: 1 1 auto;
    min-height: 12rem;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    overflow: hidden;
  }
  .telemetry {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
  }
  kbd {
    background: var(--surface2, #222);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: var(--mono, monospace);
    font-size: 0.85em;
  }
</style>
