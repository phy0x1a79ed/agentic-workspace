<script lang="ts">
  import PttPanel from '../../../components/PttPanel.svelte';
  import ChatHistory, { type ChatMessage } from '$lib/primitives/ChatHistory.svelte';

  let messages = $state<ChatMessage[]>([]);

  function onsend(body: string) {
    messages = [...messages, { body, author: 'you', ts: new Date().toISOString() }];
  }
</script>

<main class="page">
  <div class="stack">
    <ChatHistory {messages} />
    <PttPanel {onsend} />
  </div>
</main>

<style>
  .page {
    display: flex;
    justify-content: center;
    padding: 24px;
    min-height: 100vh;
  }
  .stack {
    display: flex;
    flex-direction: column;
    gap: 16px;
    width: 100%;
    max-width: 480px;
    /* Cap the history height so it scrolls instead of pushing the composer
       below the fold. */
    min-height: 0;
  }
  .stack > :global(.transcript) {
    max-height: 50vh;
    min-height: 120px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }
  @media (max-width: 720px) {
    .page { padding: 12px; }
  }
</style>
