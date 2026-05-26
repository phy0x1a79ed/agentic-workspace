// PTT → STT → focus composer.
//
// The user holds the PTT button (or spacebar while the focus tab is
// visible). We capture mic audio via mic-worklet.js, stream int16 PCM
// frames over /voice/ws while recording, and on release the server
// runs STT and broadcasts `{type:"stt_result", text}` to every tab of
// the same user. We append that text to #focus-post-body. The user
// then picks recipients and sends like any typed message.
//
// HTTP fallback: if the WebSocket is closed, we accumulate PCM locally
// and POST the whole buffer to /voice/stt on PTT release.

(function () {
  "use strict";

  // ---- DOM refs -----------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const ui = {
    conn: $("vp-conn"),
    stage: $("vp-stage"),
    ptt: $("vp-ptt"),
    meterVal: $("vp-meter-val"),
  };

  function on(el, evt, fn, opts) {
    if (el) el.addEventListener(evt, fn, opts);
  }

  function setConn(state, text) {
    if (!ui.conn) return;
    ui.conn.className = "vp-conn " + (state || "");
    ui.conn.textContent = text || "";
  }
  function setStage(stage, text) {
    if (!ui.stage) return;
    ui.stage.className = "vp-stage " + (stage || "idle");
    ui.stage.textContent = text || stage || "idle";
  }

  // ---- API helper for the HTTP fallback ----------------------------------

  async function postPcm(pcmBytes) {
    const r = await fetch("/voice/stt", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/octet-stream" },
      body: pcmBytes,
    });
    let json = null;
    try { json = await r.json(); } catch {}
    if (!r.ok) {
      const detail = json && json.detail ? JSON.stringify(json.detail) : await r.text();
      throw new Error(`POST /voice/stt → ${r.status}: ${String(detail).slice(0, 200)}`);
    }
    return (json && json.text) || "";
  }

  // ---- Compose target -----------------------------------------------------

  function appendTranscript(text) {
    if (!text) return;
    const el = $("focus-post-body");
    if (!el) return;
    const cur = el.value || "";
    const sep = cur && !/\s$/.test(cur) ? " " : "";
    el.value = cur + sep + text;
    try { el.dispatchEvent(new Event("input", { bubbles: true })); } catch {}
    el.focus();
  }

  // PTT semantics: speaking IS the send. After STT lands, fire the focus
  // composer's send the same way the Send button does. sendFocusPost
  // clears the textarea on success, which doubles as the "sent" signal.
  function autoSend() {
    if (typeof window.sendFocusPost === "function") {
      try { window.sendFocusPost(); } catch {}
    }
  }

  // ---- Audio capture ------------------------------------------------------

  let audioCtx = null;
  let micStream = null;
  let workletNode = null;
  let micConnected = false;
  let recording = false;
  // Local PCM accumulator — used by the HTTP fallback when the WS is down.
  let pcmBuf = [];
  let pcmBytes = 0;
  const MAX_PCM_BYTES = 1024 * 1024; // 1 MiB cap (~32s at 16k mono int16)

  async function ensureAudio() {
    if (audioCtx) return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    await audioCtx.audioWorklet.addModule("/ui/mic-worklet.js");
  }

  async function startMicCapture() {
    if (micConnected) return;
    await ensureAudio();
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    const src = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, "mic-downsampler");
    workletNode.port.onmessage = (e) => {
      if (!recording) return;
      const data = e.data;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(data); } catch {}
      } else {
        // Buffer for the HTTP fallback at PTT release.
        if (pcmBytes + data.byteLength <= MAX_PCM_BYTES) {
          pcmBuf.push(new Uint8Array(data));
          pcmBytes += data.byteLength;
        }
      }
    };
    src.connect(workletNode);
    // Worklet must reach the destination for processing to run.
    const silent = audioCtx.createGain();
    silent.gain.value = 0;
    workletNode.connect(silent).connect(audioCtx.destination);

    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    src.connect(analyser);
    const buf = new Float32Array(analyser.fftSize);
    function tick() {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const bar = "▮".repeat(Math.min(20, Math.round(rms * 200)));
      if (ui.meterVal) ui.meterVal.textContent = `${rms.toFixed(3)} ${bar}`;
      requestAnimationFrame(tick);
    }
    tick();
    micConnected = true;
  }

  function resetPcmBuf() { pcmBuf = []; pcmBytes = 0; }

  function takePcmBuf() {
    if (!pcmBytes) { resetPcmBuf(); return null; }
    const out = new Uint8Array(pcmBytes);
    let off = 0;
    for (const chunk of pcmBuf) { out.set(chunk, off); off += chunk.byteLength; }
    resetPcmBuf();
    return out;
  }

  // ---- WebSocket ----------------------------------------------------------

  let ws = null;
  let wsBackoff = 1000;

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/voice/ws`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      setConn("ok", "connected");
      wsBackoff = 1000;
    };
    ws.onclose = (ev) => {
      setConn("err", `disconnected (${ev.code})`);
      setTimeout(connectWs, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 15000);
    };
    ws.onerror = () => setConn("err", "ws error");
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") return;
      let msg = null;
      try { msg = JSON.parse(e.data); } catch { return; }
      switch (msg.type) {
        case "ready":
          setStage("idle", "idle");
          break;
        case "status":
          setStage(msg.stage, msg.text || msg.stage);
          break;
        case "stt_result":
          if (msg.text) { appendTranscript(msg.text); autoSend(); }
          break;
        case "error":
          setStage("error", msg.message || "error");
          break;
      }
    };
  }

  // ---- PTT ----------------------------------------------------------------

  async function pttDown() {
    if (recording) return;
    try { await startMicCapture(); }
    catch (e) {
      setStage("error", "mic blocked");
      return;
    }
    if (audioCtx.state === "suspended") await audioCtx.resume();
    resetPcmBuf();
    recording = true;
    if (ui.ptt) ui.ptt.classList.add("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "start" })); } catch {}
    } else {
      setStage("recording", "recording (offline)…");
    }
  }

  async function pttUp() {
    if (!recording) return;
    recording = false;
    if (ui.ptt) ui.ptt.classList.remove("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "end" })); } catch {}
      return;
    }
    // HTTP fallback path.
    const pcm = takePcmBuf();
    if (!pcm) { setStage("idle", "idle"); return; }
    setStage("transcribing", "transcribing…");
    try {
      const text = await postPcm(pcm);
      if (text) { appendTranscript(text); autoSend(); }
      setStage("idle", "idle");
    } catch (e) {
      setStage("error", "stt failed");
    }
  }

  on(ui.ptt, "mousedown", pttDown);
  on(ui.ptt, "mouseup", pttUp);
  on(ui.ptt, "mouseleave", () => recording && pttUp());
  on(ui.ptt, "touchstart", (e) => { e.preventDefault(); pttDown(); });
  on(ui.ptt, "touchend", (e) => { e.preventDefault(); pttUp(); });

  // Spacebar PTT — gated so it only fires when the focus tab is visible
  // and no input has focus.
  function isTypingFocus() {
    const el = document.activeElement;
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
  }
  function isFocusTabVisible() {
    if (!ui.ptt) return false;
    const tab = document.getElementById("tab-focus");
    return !!(tab && !tab.hidden);
  }
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || isTypingFocus()) return;
    if (!isFocusTabVisible()) return;
    e.preventDefault();
    pttDown();
  });
  window.addEventListener("keyup", (e) => {
    if (e.code !== "Space" || isTypingFocus()) return;
    if (!isFocusTabVisible() && !recording) return;
    e.preventDefault();
    pttUp();
  });

  // ---- Boot ---------------------------------------------------------------

  setConn("", "connecting…");
  connectWs();
})();
