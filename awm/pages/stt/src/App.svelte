<script lang="ts">
  // STT dev page. It owns nothing but a mock conversation: the standardized
  // <Chat> composite (from @awm/chat) provides the voice/text input AND the
  // transcript, so this page only supplies a data source — a `posts` buffer and
  // an `onSend` that forwards the user's turn to a mock agent over /svc/stt and
  // displays the reply. The stt scope iterates the voice-input sub-component;
  // this page is just where it's exercised against a live STT session.
  import { Chat, type Post } from '@awm/chat';
  import { svc } from '@awm/client';

  let posts = $state<Post[]>([]);
  let seq = 0;

  // Recent chat history fed to the convo cleanup LLM as context. Capped on the
  // composer side; here we just surface the last handful of turns. The mock
  // agent's replies land in `posts`, so they flow into this context too — the
  // cleanup model cleans your speech against the live conversation.
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

  // <Chat> calls this exactly once per user turn (the send-once gate lives in
  // Chat). We display the user's message, forward it to the mock test agent, and
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

  <div class="chat-host">
    <Chat {posts} onSend={onUserMessage} {chatContext} />
  </div>
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
  kbd {
    background: var(--surface2, #222);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: var(--mono, monospace);
    font-size: 0.85em;
  }
</style>
