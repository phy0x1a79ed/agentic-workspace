// Voice side panel — always-visible in the control center.
//
// Connects to /voice/ws using the bearer token from the URL hash, runs
// push-to-talk capture via mic-worklet, plays back streaming audio,
// and renders transcript + show() boxes. Cross-window state (joined
// rooms, transcripts, audio) is broadcast by the server so multiple
// tabs of the same user stay in sync.

(function () {
  "use strict";

  // ---- Hash config (shared with the main app) -----------------------------

  function parseHashConfig() {
    const raw = location.hash;
    let configPart = raw.slice(1);
    const routeIdx = configPart.indexOf("#/");
    if (routeIdx >= 0) configPart = configPart.slice(0, routeIdx);
    else if (configPart.startsWith("/")) configPart = "";
    const params = new URLSearchParams(configPart);
    return {
      token: params.get("token") || "",
      as: params.get("as") || "user:operator",
      peer: params.get("peer") || "",
    };
  }

  const cfg = parseHashConfig();
  if (!cfg.token) return; // main app already showed the no-token error.

  // ---- DOM refs -----------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const ui = {
    conn: $("vp-conn"),
    stage: $("vp-stage"),
    ptt: $("vp-ptt"),
    meterVal: $("vp-meter-val"),
    latency: $("vp-latency"),
    transcript: $("vp-transcript"),
    roomsList: $("vp-rooms-list"),
    joinToggle: $("vp-join-toggle"),
    roomPicker: $("vp-room-picker"),
    roomSelect: $("vp-room-select"),
    roomJoin: $("vp-room-join"),
    roomCancel: $("vp-room-cancel"),
    textInput: $("vp-text"),
    textSend: $("vp-text-send"),
    vol: $("vp-vol"),
    volVal: $("vp-vol-val"),
  };

  // ---- API helper (mirrors the main app's pattern) ------------------------

  async function api(method, path, body) {
    const init = {
      method,
      headers: {
        "Authorization": `Bearer ${cfg.token}`,
        "X-Awm-As": cfg.as,
      },
    };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const r = await fetch(location.origin + path, init);
    let json = null;
    try { json = await r.json(); } catch {}
    if (!r.ok) {
      const detail = json && json.detail ? JSON.stringify(json.detail) : await r.text();
      throw new Error(`${method} ${path} → ${r.status}: ${String(detail).slice(0, 200)}`);
    }
    return json;
  }

  // ---- Audio (PTT + playback) --------------------------------------------

  let audioCtx = null;
  let masterGain = null;
  let micStream = null;
  let workletNode = null;
  let micConnected = false;
  let recording = false;

  function readVol() { return (parseInt(ui.vol.value, 10) || 0) / 100; }
  ui.vol.addEventListener("input", () => {
    const v = readVol();
    ui.volVal.textContent = `${Math.round(v * 100)}%`;
    if (masterGain) masterGain.gain.value = v;
  });

  async function ensureAudio() {
    if (audioCtx) return;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    await audioCtx.audioWorklet.addModule("/ui/mic-worklet.js");
    masterGain = audioCtx.createGain();
    masterGain.gain.value = readVol();
    masterGain.connect(audioCtx.destination);
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
      if (!recording || !ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(e.data);
    };
    src.connect(workletNode);
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
      ui.meterVal.textContent = `${rms.toFixed(3)} ${bar}`;
      requestAnimationFrame(tick);
    }
    tick();
    micConnected = true;
  }

  // Playback queue.
  const playQueue = [];
  let playing = false;
  let currentSource = null;
  let pendingAudio = null;

  function pcmToFloat32(int16) {
    const f = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      f[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);
    }
    return f;
  }

  function flushPlayback() {
    playQueue.length = 0;
    if (currentSource) {
      try { currentSource.stop(); } catch (e) {}
      currentSource = null;
    }
    playing = false;
  }

  async function playNext() {
    if (playing) return;
    const next = playQueue.shift();
    if (!next) return;
    playing = true;
    const buf = audioCtx.createBuffer(1, next.pcm.length, next.sampleRate);
    buf.getChannelData(0).set(pcmToFloat32(next.pcm));
    const src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.playbackRate.value = window.VOICE_PLAYBACK_RATE || 1.1;
    const gain = audioCtx.createGain();
    src.connect(gain).connect(masterGain);
    currentSource = src;
    src.onended = () => { currentSource = null; playing = false; playNext(); };
    src.start();
  }

  // ---- Transcript rendering ----------------------------------------------

  function setConn(state, label) {
    ui.conn.className = "vp-conn " + (state || "");
    ui.conn.textContent = label;
  }

  function setStage(stage, text) {
    ui.stage.className = "vp-stage " + (stage || "");
    ui.stage.textContent = text || stage || "idle";
  }

  function appendLine(role, text) {
    const div = document.createElement("div");
    div.className = "vp-line " + role;
    div.textContent = `${role}: ${text}`;
    ui.transcript.appendChild(div);
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function appendAssistantDelta(text) {
    let last = ui.transcript.lastElementChild;
    if (!last || !last.classList.contains("assistant-stream")) {
      last = document.createElement("div");
      last.className = "vp-line assistant assistant-stream";
      last.textContent = "assistant: ";
      ui.transcript.appendChild(last);
    }
    last.textContent += text;
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function endAssistantStream() {
    const last = ui.transcript.lastElementChild;
    if (last && last.classList.contains("assistant-stream")) {
      last.classList.remove("assistant-stream");
    }
  }

  function renderShow(content, kind) {
    const box = document.createElement("div");
    box.className = "vp-show " + (kind || "text");
    const label = document.createElement("div");
    label.className = "vp-show-label";
    label.textContent = kind || "shown";
    box.appendChild(label);
    if (kind === "link") {
      const a = document.createElement("a");
      a.href = content;
      a.textContent = content;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      box.appendChild(a);
    } else {
      const pre = document.createElement("pre");
      pre.textContent = content;
      box.appendChild(pre);
    }
    ui.transcript.appendChild(box);
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function setLatency(stage, ms) {
    ui.latency.dataset[stage] = ms;
    ui.latency.textContent =
      `STT ${ui.latency.dataset.stt ?? "—"}ms · ` +
      `first-token ${ui.latency.dataset.first_token ?? "—"}ms · ` +
      `first-audio ${ui.latency.dataset.first_audio ?? "—"}ms`;
  }

  function renderJoinedRooms(rooms) {
    if (!rooms || rooms.length === 0) {
      ui.roomsList.innerHTML = "<em>none</em>";
      return;
    }
    ui.roomsList.innerHTML = "";
    for (const r of rooms) {
      const chip = document.createElement("span");
      chip.className = "vp-chip";
      chip.textContent = r;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "×";
      btn.title = `leave ${r}`;
      btn.addEventListener("click", () => leaveRoom(r));
      chip.appendChild(btn);
      ui.roomsList.appendChild(chip);
    }
  }

  // ---- WebSocket ----------------------------------------------------------

  let ws = null;
  let wsBackoff = 1000;

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/voice/ws`,
                       [`bearer.${cfg.token}`]);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { setConn("ok", "connected"); wsBackoff = 1000; };
    ws.onclose = (ev) => {
      setConn("err", `disconnected (${ev.code})`);
      setTimeout(connectWs, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 15000);
    };
    ws.onerror = () => setConn("err", "ws error");
    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        try { handleWsText(JSON.parse(e.data)); } catch {}
      } else {
        handleWsBinary(e.data);
      }
    };
  }

  function handleWsText(msg) {
    switch (msg.type) {
      case "ready":
        setStage("idle", "idle");
        break;
      case "status":
        setStage(msg.stage, msg.text || msg.stage);
        break;
      case "transcript":
        if (msg.text) appendLine("user", msg.text);
        break;
      case "agent_text":
        appendAssistantDelta(msg.delta);
        break;
      case "agent_turn_end":
        endAssistantStream();
        break;
      case "tool_use":
        appendLine("tool", `→ ${msg.body}`);
        break;
      case "tool_result":
        appendLine("tool-result", `← ${msg.body}`);
        break;
      case "show":
        renderShow(msg.content, msg.kind);
        break;
      case "audio":
        pendingAudio = { sampleRate: msg.sample_rate };
        break;
      case "latency":
        setLatency(msg.stage, msg.ms);
        break;
      case "joined_rooms":
        renderJoinedRooms(msg.rooms || []);
        break;
      case "error":
        appendLine("error", msg.message);
        break;
    }
  }

  function handleWsBinary(buf) {
    if (!pendingAudio || !audioCtx) return;
    const pcm = new Int16Array(buf);
    playQueue.push({ sampleRate: pendingAudio.sampleRate, pcm });
    pendingAudio = null;
    playNext();
  }

  // ---- PTT controls -------------------------------------------------------

  async function pttDown() {
    if (recording) return;
    try { await startMicCapture(); }
    catch (e) {
      setStage("error", "mic blocked");
      appendLine("error", `mic: ${e.message}`);
      return;
    }
    if (audioCtx.state === "suspended") await audioCtx.resume();
    flushPlayback();
    recording = true;
    ui.ptt.classList.add("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "start" }));
    }
  }

  function pttUp() {
    if (!recording) return;
    recording = false;
    ui.ptt.classList.remove("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "end" }));
    }
  }

  ui.ptt.addEventListener("mousedown", pttDown);
  ui.ptt.addEventListener("mouseup", pttUp);
  ui.ptt.addEventListener("mouseleave", () => recording && pttUp());
  ui.ptt.addEventListener("touchstart", (e) => { e.preventDefault(); pttDown(); });
  ui.ptt.addEventListener("touchend", (e) => { e.preventDefault(); pttUp(); });

  // Spacebar PTT — but only when no text input is focused.
  function isTypingFocus() {
    const el = document.activeElement;
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
  }
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || isTypingFocus()) return;
    e.preventDefault();
    pttDown();
  });
  window.addEventListener("keyup", (e) => {
    if (e.code !== "Space" || isTypingFocus()) return;
    e.preventDefault();
    pttUp();
  });

  // ---- Typed input --------------------------------------------------------

  async function sendText() {
    const body = ui.textInput.value;
    if (!body.trim()) return;
    ui.textInput.value = "";
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "text", body }));
    } else {
      try { await api("POST", "/voice/text", { body }); }
      catch (e) { appendLine("error", e.message); }
    }
  }
  ui.textSend.addEventListener("click", sendText);
  ui.textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendText(); }
  });

  // ---- Room join/leave ----------------------------------------------------

  ui.joinToggle.addEventListener("click", async () => {
    ui.roomPicker.hidden = false;
    ui.joinToggle.disabled = true;
    try {
      const r = await api("GET", "/rooms?status=active");
      const sel = ui.roomSelect;
      sel.innerHTML = "";
      for (const room of (r.rooms || [])) {
        const opt = document.createElement("option");
        opt.value = room.id;
        opt.textContent = `${room.id}${room.topic ? " — " + room.topic : ""}`;
        sel.appendChild(opt);
      }
      if (!r.rooms || r.rooms.length === 0) {
        sel.innerHTML = "<option value=''>no active rooms</option>";
      }
    } catch (e) {
      appendLine("error", e.message);
    } finally {
      ui.joinToggle.disabled = false;
    }
  });

  ui.roomCancel.addEventListener("click", () => {
    ui.roomPicker.hidden = true;
  });

  ui.roomJoin.addEventListener("click", async () => {
    const roomId = ui.roomSelect.value;
    if (!roomId) return;
    try {
      await api("POST", `/voice/rooms/${encodeURIComponent(roomId)}/join`);
      ui.roomPicker.hidden = true;
    } catch (e) {
      appendLine("error", e.message);
    }
  });

  async function leaveRoom(roomId) {
    try { await api("POST", `/voice/rooms/${encodeURIComponent(roomId)}/leave`); }
    catch (e) { appendLine("error", e.message); }
  }

  // ---- Boot ---------------------------------------------------------------

  setConn("", "connecting…");
  connectWs();
})();
