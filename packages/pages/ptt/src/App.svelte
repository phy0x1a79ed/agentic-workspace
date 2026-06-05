<script lang="ts">
  // PTT demo page. The page itself does almost nothing — all the real
  // work (session against /svc/ptt, mic worklet, transcript chips,
  // PTT/Convo modes) lives in @awm/ptt-composer. We keep a running log
  // of finalized utterances below the composer to make the component's
  // output visible standalone.
  import { PttComposer } from '@awm/ptt-composer';

  let sent = $state<string[]>([]);
  let live = $state<string[]>([]);

  function onSend(text: string) {
    sent = [...sent, text];
  }
  function onText(text: string) {
    live = [...live, text];
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

  <PttComposer onsend={onSend} onText={onText} />

  <section class="log">
    <h2>Finalized utterances</h2>
    {#if live.length === 0}
      <p class="empty">none yet</p>
    {:else}
      <ol>
        {#each live as t, i (`${i}-${t}`)}
          <li>{t}</li>
        {/each}
      </ol>
    {/if}

    <h2>Sent (Send button pressed)</h2>
    {#if sent.length === 0}
      <p class="empty">none yet</p>
    {:else}
      <ol>
        {#each sent as t, i (`${i}-${t}`)}
          <li>{t}</li>
        {/each}
      </ol>
    {/if}
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
  }
  header { margin-bottom: 1rem; }
  h1 { font-size: 1.2rem; letter-spacing: 0.05em; text-transform: uppercase; margin: 0 0 0.25rem; }
  h2 { font-size: 0.85rem; letter-spacing: 0.05em; text-transform: uppercase; margin: 1.5rem 0 0.5rem; color: var(--text2, #bbb); }
  .hint { font-size: 0.85rem; color: var(--text3, #888); margin: 0; }
  .empty { font-style: italic; color: var(--text3, #888); font-size: 0.85rem; }
  ol { padding-left: 1.25rem; margin: 0; font-size: 0.9rem; line-height: 1.5; }
  kbd {
    background: var(--surface2, #222);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: var(--mono, monospace);
    font-size: 0.85em;
  }
</style>
