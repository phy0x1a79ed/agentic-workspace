<script lang="ts">
  import { onMount } from 'svelte';
  import { whoami } from '@awm/client';

  let signedIn = $state(false);

  onMount(() => {
    let cancelled = false;

    async function check() {
      if (cancelled) return;
      try {
        await whoami();
        signedIn = true;
        setTimeout(() => location.replace('/ui/agent'), 400);
        return;
      } catch (_) {
        // not signed in (AuthError) or network blip (HttpError/TypeError)
        // — keep polling.
      }
      setTimeout(check, 2000);
    }

    check();
    return () => { cancelled = true; };
  });
</script>

<main>
  <h1>awm sign-in</h1>
  <p class="muted">
    This control panel uses one-shot login links. Request one of these
    options below — when the link is opened, this page will auto-redirect.
  </p>

  <div class="card">
    <strong>1. Discord</strong>
    <p>Run the <code>/login</code> slash command in your peer's Discord bot.
       The bot will DM you a sign-in link (expires in 60s).</p>
  </div>

  <div class="card">
    <strong>2. Terminal (on the daemon host)</strong>
    <pre>awm login</pre>
    <p class="muted">Prints a one-shot URL. Open it in this browser.</p>
  </div>

  <div class="status">
    <span class="dot" class:on={signedIn}></span>
    <span>{signedIn ? 'signed in — redirecting…' : 'waiting for sign-in…'}</span>
  </div>
</main>

<style>
  main {
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI",
                 Roboto, Helvetica, Arial, sans-serif;
    max-width: 560px;
    margin: 64px auto;
    padding: 0 24px;
    color: #222;
    line-height: 1.55;
  }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
  .muted { color: #666; }
  .card {
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 18px 20px;
    margin-top: 18px;
    background: #fafafa;
  }
  code, pre {
    background: #eee;
    padding: 2px 5px;
    border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.92rem;
  }
  pre { padding: 8px 12px; overflow-x: auto; }
  .status {
    margin-top: 24px;
    font-size: 0.9rem;
    color: #666;
  }
  .dot {
    display: inline-block;
    width: 8px; height: 8px; border-radius: 50%;
    background: #b0b0b0; margin-right: 6px; vertical-align: middle;
  }
  .dot.on { background: #3b9c3b; }
</style>
