<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  // Minimal PTT page: opens a stream session against /svc/ptt, pushes
  // mic PCM up while the button is held, renders transcript text frames
  // back. The full PTT UI (PttPanel, ConvoTab, …) is preserved in the
  // web-ptt scope and ports forward as components/ptt-shared.
  let session = $state<{ id: string; ws_path: string } | null>(null);
  let socket = $state<WebSocket | null>(null);
  let mediaStream = $state<MediaStream | null>(null);
  let audioCtx = $state<AudioContext | null>(null);
  let proc = $state<ScriptProcessorNode | null>(null);
  let transcript = $state('');
  let recording = $state(false);
  let error = $state<string | null>(null);

  async function openSession() {
    const r = await fetch('/svc/ptt/session/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'include',
      body: '{}',
    });
    if (!r.ok) { error = `session open: HTTP ${r.status}`; return; }
    session = await r.json();
    const u = new URL(session.ws_path, window.location.href);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(u.toString());
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('message', (ev) => {
      if (typeof ev.data === 'string') {
        try {
          const obj = JSON.parse(ev.data);
          if (obj?.text) transcript = obj.text;
        } catch { /* ignore */ }
      }
    });
  }

  async function startRecord() {
    if (!socket || socket.readyState !== WebSocket.OPEN) await openSession();
    recording = true;
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioCtx = new AudioContext({ sampleRate: 16000 });
    const src = audioCtx.createMediaStreamSource(mediaStream);
    proc = audioCtx.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = (ev) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const data = ev.inputBuffer.getChannelData(0);
      const out = new Int16Array(data.length);
      for (let i = 0; i < data.length; i++) {
        const s = Math.max(-1, Math.min(1, data[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      socket.send(out.buffer);
    };
    src.connect(proc);
    proc.connect(audioCtx.destination);
  }

  function stopRecord() {
    recording = false;
    proc?.disconnect();
    proc = null;
    audioCtx?.close();
    audioCtx = null;
    mediaStream?.getTracks().forEach((t) => t.stop());
    mediaStream = null;
  }

  onDestroy(() => {
    stopRecord();
    socket?.close();
  });
</script>

<main>
  <h1>PTT</h1>
  {#if error}<p class="error">{error}</p>{/if}
  <button
    onmousedown={startRecord}
    onmouseup={stopRecord}
    onmouseleave={stopRecord}
  >
    {recording ? 'recording…' : 'hold to talk'}
  </button>
  <pre>{transcript}</pre>
</main>

<style>
  main { padding: 2rem; font-family: system-ui, sans-serif; }
  .error { color: #b00; }
  button { padding: 1rem 2rem; font-size: 1.1rem; }
  pre { background: #f5f5f5; padding: 1rem; min-height: 4rem; }
</style>
