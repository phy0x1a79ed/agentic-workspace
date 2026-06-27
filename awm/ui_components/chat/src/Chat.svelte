<script lang="ts">
  /**
   * The standardized chat composite — text+voice input over a transcript.
   *
   * Composes the two ownable sub-components:
   *   - <SttComposer> from @awm/stt-composer — TEXT + VOICE (STT) input;
   *   - <TtsHistory>  from @awm/tts-history  — the transcript + per-row replay.
   *
   * It is **controlled and presentational**: the parent owns the `posts` buffer
   * and the send handler, Chat renders them and owns only the input wiring. It
   * knows nothing about agents / scopes / TTS engines — every consumer (the
   * agent page via @awm/agent-chat, the stt mock page, the tts engine tester)
   * mounts this same component and injects its own data source via `posts` +
   * `onSend`, plus optional `header` / `aside` snippet slots.
   *
   * The send-once contract lives HERE, once, so it propagates to all consumers:
   * both composer signals (`onsend` button, `onText` finalized utterance) route
   * through one `submit()` guarded by `makeSubmitGate` — see submit-gate.ts.
   * Auto-speak is intentionally NOT here: it must fire on the data source's
   * finalized-message signal (a streamed `partial` shares the final row's id, so
   * speaking on row-appearance would speak a fragment), so it stays in the
   * agent data source which has that signal.
   */
  import type { Snippet } from 'svelte';
  import { TtsHistory } from '@awm/tts-history';
  import type { Post } from '@awm/tts-history';
  import { SttComposer } from '@awm/stt-composer';
  import type { TelemetryEvent } from '@awm/stt-telemetry';
  import { makeSubmitGate } from './submit-gate';

  interface Props {
    /** Transcript to render (parent-owned). */
    posts: Post[];
    /** One call per user turn. The gate collapses same-turn duplicates. */
    onSend: (text: string) => void;
    /** Recent transcript context forwarded to the STT cleanup LLM. */
    chatContext?: string;
    /** Per-row replay; forwarded to <TtsHistory> as `onspeak`. Omit → no replay. */
    onReplay?: (post: Post) => void;
    /** Optional header strip (e.g. a connect bar). */
    header?: Snippet;
    /** Optional panel below the composer (e.g. a TTS engine-config panel). */
    aside?: Snippet;
    /** Fully offline (gallery/demo): put the embedded SttComposer in mock mode
     *  so it opens no /svc/stt session. Default false. */
    offline?: boolean;
    /** Dev telemetry tap: forwarded to the embedded SttComposer. No-op when
     *  absent (the composer's telemetry helpers stay dark). Lets a host render
     *  an <SttTelemetry> timeline against this composite's STT pipeline. */
    onTelemetry?: (e: TelemetryEvent) => void;
  }
  let { posts, onSend, chatContext = '', onReplay, header, aside, offline = false, onTelemetry }: Props = $props();

  // The single send seam. Both SttComposer signals call it; the gate drops a
  // same-turn duplicate and empty sends. Consumers can no longer double-bind.
  const gate = makeSubmitGate();
  function submit(text: string) {
    const t = gate(text);
    if (t) onSend(t);
  }
</script>

<div class="chat">
  {#if header}
    <header class="chat-header">{@render header()}</header>
  {/if}

  <div class="history-wrap">
    <TtsHistory {posts} onspeak={onReplay} />
  </div>

  {#if aside}
    <section class="chat-aside">{@render aside()}</section>
  {/if}

  <div class="composer-wrap">
    <SttComposer
      onsend={submit}
      onText={submit}
      {chatContext}
      {onTelemetry}
      mockInitialChips={offline ? [] : undefined}
    />
  </div>
</div>

<style>
  .chat {
    display: flex;
    flex-direction: column;
    /* Grow to fill a flex-column parent; height:100% covers a block parent
       with a definite height. Every consumer wraps Chat in a flex column. */
    flex: 1 1 auto;
    height: 100%;
    min-height: 0;
    width: 100%;
  }
  .chat-header { flex: 0 0 auto; }
  .history-wrap {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  :global(.history-wrap > .transcript) { flex: 1; }
  .chat-aside { flex: 0 0 auto; }
  .composer-wrap {
    padding: 12px 14px;
    border-top: 1px solid var(--border, #333);
    background: var(--surface, #1a1a1a);
    display: flex;
    justify-content: center;
    flex: 0 0 auto;
  }
</style>
