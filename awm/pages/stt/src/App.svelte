<script lang="ts">
  // PTT demo page. The page itself does almost nothing — all the real
  // work (session against /svc/stt, mic worklet, transcript chips,
  // PTT/Convo modes) lives in @awm/stt-composer. Below the composer we
  // render this session's output through @awm/tts-history's <TtsHistory>
  // so the component's chat-history view is visible standalone: each
  // finalized dictation utterance and each Send becomes a transcript row.
  import { SttComposer } from '@awm/stt-composer';
  import { TtsHistory } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { svc } from '@awm/client';

  let posts = $state<Post[]>([]);
  let seq = 0;

  // Recent chat history fed to the convo cleanup LLM as context. Capped on
  // the composer side; here we just surface the last handful of turns. Because
  // the mock agent's replies land in `posts`, they flow into this context too —
  // the cleanup model cleans your speech against the live conversation.
  const chatContext = $derived(
    posts.slice(-20).map((p) => `${p.author}: ${p.body}`).join('\n'),
  );

  function add(author: string, body: string): string {
    const id = String(seq++);
    posts = [...posts, { id, ts: new Date().toISOString(), author, body }];
    return id;
  }
  function setBody(id: string, body: string) {
    posts = posts.map((p) => (p.id === id ? { ...p, body, ts: new Date().toISOString() } : p));
  }

  // Chat-history listener. The composer notifies this on every finalized send
  // (Convo silence-cut auto-submit via onText, or the SEND button via onsend).
  // We display the user's message, forward it to the mock test agent, and
  // display the agent's reply. Only user-authored messages are forwarded — the
  // agent's own replies are never fed back, so there is no loop.
  async function onUserMessage(text: string) {
    const t = text.trim();
    if (!t) return;
    add('you', t);
    const replyId = add('agent', '…'); // placeholder while the agent thinks
    try {
      const { reply } = await svc('stt').fn<{ reply: string }>('chat', { text: t });
      setBody(replyId, reply?.trim() || '…');
    } catch (err) {
      setBody(replyId, `[chat error: ${(err as Error).message}]`);
    }
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

  <SttComposer onsend={onUserMessage} onText={onUserMessage} {chatContext} />

  <section class="history">
    <h2>Chat history</h2>
    <div class="history-wrap">
      <TtsHistory {posts} />
    </div>
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
  .history {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .history-wrap {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 12rem;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    overflow: hidden;
  }
  :global(.history-wrap > .transcript) { flex: 1; }
  kbd {
    background: var(--surface2, #222);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: var(--mono, monospace);
    font-size: 0.85em;
  }
</style>
