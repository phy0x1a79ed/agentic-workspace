<script lang="ts">
  import { runAgentSlash, ApiError } from '$lib/api/client';

  interface Props {
    roomId: string;
    scope: string;
    onresult?: (msg: string) => void;
  }
  let { roomId, scope, onresult }: Props = $props();

  let busy = $state<string | null>(null);

  async function run(cmd: string, confirmMsg?: string) {
    if (busy) return;
    if (confirmMsg && typeof window !== 'undefined' && !window.confirm(confirmMsg)) return;
    busy = cmd;
    try {
      const r = await runAgentSlash(roomId, scope, cmd);
      onresult?.(r.result || `${cmd} ok`);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : (e as Error).message;
      onresult?.(msg);
    } finally {
      busy = null;
    }
  }
</script>

<div class="bar mono">
  <button type="button" disabled={busy !== null} onclick={() => run('/restart')}>
    {busy === '/restart' ? '…' : 'restart'}
  </button>
  <button type="button" disabled={busy !== null} onclick={() => run('/compact')}>
    {busy === '/compact' ? '…' : 'compact'}
  </button>
  <button type="button" disabled={busy !== null} onclick={() => run('/clear')}>
    {busy === '/clear' ? '…' : 'clear'}
  </button>
  <button class="danger" type="button" disabled={busy !== null} onclick={() => run('/kill', `Kill agent ${scope}? This stops the manager process — you'll need to restart to resume.`)}>
    {busy === '/kill' ? '…' : 'kill'}
  </button>
</div>

<style>
  .bar {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 6px;
  }
  button {
    background: var(--surface3);
    color: var(--text2);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 3px 8px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    cursor: pointer;
  }
  button:hover:not(:disabled) {
    background: color-mix(in oklab, var(--atomizer) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--atomizer) 40%, var(--border));
    color: var(--text);
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger:hover:not(:disabled) {
    background: color-mix(in oklab, var(--danger) 18%, var(--surface3));
    border-color: color-mix(in oklab, var(--danger) 40%, var(--border));
    color: var(--text);
  }
  .mono { font-family: var(--mono); }
</style>
