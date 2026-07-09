<script lang="ts">
  // Custom dictation vocabulary — the terms whisper is biased toward so it
  // spells your domain words right (project names, acronyms). This is a manual
  // list on purpose: pulling it from the note itself would cost the dictation
  // latency the transcription budget can't spare.
  interface Props {
    terms: string[];
    onAdd: (term: string) => void;
    onRemove: (term: string) => void;
  }
  let { terms, onAdd, onRemove }: Props = $props();
  let draft = $state('');

  function submit() {
    const t = draft.trim();
    if (!t) return;
    onAdd(t);
    draft = '';
  }
</script>

<div class="vocab">
  <p class="blurb">Words whisper should recognize during dictation.</p>
  <form class="add" onsubmit={(e) => { e.preventDefault(); submit(); }}>
    <input
      class="add-input"
      placeholder="Add a term…"
      bind:value={draft}
      spellcheck="false"
      autocomplete="off"
    />
    <button class="add-btn" type="submit" disabled={!draft.trim()}>Add</button>
  </form>

  {#if terms.length === 0}
    <p class="empty">No custom terms yet. Add names like “scadc” or “avarice”.</p>
  {:else}
    <ul class="chips">
      {#each terms as term (term)}
        <li class="chip">
          <span class="term">{term}</span>
          <button class="rm" aria-label="Remove {term}" title="Remove" onclick={() => onRemove(term)}>×</button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .vocab { display: flex; flex-direction: column; gap: 0.75rem; }
  .blurb { margin: 0; font-size: 0.75rem; color: var(--nt-text2); line-height: 1.5; }
  .add { display: flex; gap: 0.4rem; }
  .add-input {
    flex: 1 1 auto;
    min-width: 0;
    padding: 0.4rem 0.55rem;
    background: var(--nt-input-bg);
    border: 1px solid var(--nt-border);
    border-radius: 6px;
    color: var(--nt-text);
    font-family: var(--nt-mono);
    font-size: 0.8rem;
  }
  .add-input:focus { outline: 2px solid var(--nt-accent); outline-offset: -1px; }
  .add-btn {
    flex: 0 0 auto;
    padding: 0 0.7rem;
    border: 1px solid var(--nt-border);
    border-radius: 6px;
    background: var(--nt-accent-soft);
    color: var(--nt-accent);
    font-family: var(--nt-sans);
    font-size: 0.75rem;
    font-weight: 600;
    cursor: pointer;
  }
  .add-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .add-btn:not(:disabled):hover { background: var(--nt-accent); color: var(--nt-on-accent); }
  .empty { margin: 0; font-size: 0.75rem; color: var(--nt-faint); font-style: italic; line-height: 1.5; }
  .chips { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.2rem 0.3rem 0.2rem 0.55rem;
    background: var(--nt-chip-bg);
    border: 1px solid var(--nt-border);
    border-radius: 999px;
    font-family: var(--nt-mono);
    font-size: 0.78rem;
    color: var(--nt-text);
  }
  .rm {
    border: 0;
    background: none;
    color: var(--nt-faint);
    font-size: 0.95rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.15rem;
    border-radius: 50%;
  }
  .rm:hover { color: var(--nt-danger); }
</style>
