// Browser client: engine pickers → POST /call/start → WS /call/{id}.
//
// Per-call config: each "apply & reconnect" reads the picker values,
// POSTs them to /call/start, and opens a fresh WebSocket to the
// returned ws_url. PTT, mic capture, and playback are unchanged from
// the original demo.

const ui = {
  status: document.getElementById("status"),
  stage: document.getElementById("stage"),
  ptt: document.getElementById("ptt"),
  transcript: document.getElementById("transcript"),
  latency: document.getElementById("latency"),
  meterVal: document.getElementById("meter-val"),
  vol: document.getElementById("vol"),
  volVal: document.getElementById("vol-val"),
  sttEngine: document.getElementById("stt-engine"),
  ttsEngine: document.getElementById("tts-engine"),
  llmEngine: document.getElementById("llm-engine"),
  ttsKnobs: document.getElementById("tts-knobs"),
  llmKnobs: document.getElementById("llm-knobs"),
  applyBtn: document.getElementById("apply-btn"),
  applyStatus: document.getElementById("apply-status"),
  textInput: document.getElementById("text-input"),
  textSendBtn: document.getElementById("text-send-btn"),
};

// ── per-engine knob spec ──────────────────────────────────────────────
// Lightweight, hand-coded form schemas so we don't pull /engines schemas
// off the server. Adding a new engine = add a row here.

const TTS_KNOBS = {
  piper: [],
  pocket: [
    { key: "voice", label: "voice", type: "text", placeholder: "jean" },
  ],
  kokoro_rvc: [
    { key: "tts_voice", label: "kokoro voice", type: "text", placeholder: "bf_emma" },
    { key: "rvc_label", label: "rvc voice", type: "text", placeholder: "chelly_egoist" },
    { key: "pitch", label: "pitch (st)", type: "number", step: 1, placeholder: "0" },
  ],
  f5tts: [
    { key: "ref_wav_path", label: "ref wav", type: "text" },
    { key: "ref_text", label: "ref text", type: "text" },
  ],
  gptsovits: [
    { key: "ref_audio_path", label: "ref wav", type: "text" },
    { key: "prompt_text", label: "ref text", type: "text" },
  ],
  sbv2: [
    { key: "ref_wav_path", label: "ref wav", type: "text" },
    { key: "ref_text", label: "ref text", type: "text" },
  ],
};

const LLM_KNOBS = {
  openrouter: [
    { key: "model", label: "model", type: "text", placeholder: "z-ai/glm-4.5-air:free" },
  ],
  claude: [],
};

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let micConnected = false;
let recording = false;
let masterGain = null;
let currentCallId = null;

function readVol() {
  return (parseInt(ui.vol.value, 10) || 0) / 100;
}
ui.vol.addEventListener("input", () => {
  const v = readVol();
  ui.volVal.textContent = `${Math.round(v * 100)}%`;
  if (masterGain) masterGain.gain.value = v;
});

const playQueue = [];
let playing = false;
let currentSource = null;
let currentGain = null;

function setStatus(s) { ui.status.textContent = s; }
function setApplyStatus(s) { ui.applyStatus.textContent = s; }

function logTranscript(role, text) {
  const div = document.createElement("div");
  div.className = "line " + role;
  div.textContent = `${role}: ${text}`;
  ui.transcript.appendChild(div);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

function logToolLine(cls, prefix, text) {
  const div = document.createElement("div");
  div.className = "line " + cls;
  div.textContent = `${prefix} ${text}`;
  ui.transcript.appendChild(div);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

function logShow(content, kind) {
  const box = document.createElement("div");
  box.className = "show " + (kind || "text");
  const label = document.createElement("div");
  label.className = "show-label";
  label.textContent = kind || "shown";
  box.appendChild(label);
  if (kind === "link") {
    const a = document.createElement("a");
    a.href = content; a.textContent = content;
    a.target = "_blank"; a.rel = "noopener noreferrer";
    box.appendChild(a);
  } else {
    const pre = document.createElement("pre"); pre.textContent = content;
    box.appendChild(pre);
  }
  ui.transcript.appendChild(box);
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

function setStage(stage, text) {
  ui.stage.className = "";
  if (stage) ui.stage.classList.add(stage);
  ui.stage.textContent = text || stage || "idle";
}

function updateUserPartial(text) {
  let last = ui.transcript.lastElementChild;
  if (!last || !last.classList.contains("user-stream")) {
    last = document.createElement("div");
    last.className = "line user user-stream";
    ui.transcript.appendChild(last);
  }
  last.textContent = `user: ${text}…`;
  ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

function finalizeUserPartial(text) {
  let last = ui.transcript.lastElementChild;
  if (last && last.classList.contains("user-stream")) {
    last.classList.remove("user-stream");
    last.textContent = `user: ${text}`;
  } else if (text) {
    logTranscript("user", text);
  }
}

function appendAssistantDelta(text) {
  let last = ui.transcript.lastElementChild;
  if (!last || !last.classList.contains("assistant-stream")) {
    last = document.createElement("div");
    last.className = "line assistant assistant-stream";
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

function setLatency(stage, ms) {
  ui.latency.dataset[stage] = ms;
  ui.latency.textContent =
    `STT ${ui.latency.dataset.stt ?? "—"}ms · first-token ${ui.latency.dataset.first_token ?? "—"}ms · first-audio ${ui.latency.dataset.first_audio ?? "—"}ms`;
}

async function ensureAudio() {
  if (audioCtx) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.audioWorklet.addModule("/static/mic-worklet.js");
  masterGain = audioCtx.createGain();
  masterGain.gain.value = readVol();
  masterGain.connect(audioCtx.destination);
}

async function startMicCapture() {
  if (micConnected) return;
  await ensureAudio();
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1, echoCancellation: true,
      noiseSuppression: true, autoGainControl: true,
    },
  });
  const track = micStream.getAudioTracks()[0];
  console.log("mic track:", track && track.label, "settings:",
              track && track.getSettings && track.getSettings(),
              "enabled:", track && track.enabled, "muted:", track && track.muted);

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
    ui.meterVal.textContent = `${rms.toFixed(4)} ${bar}`;
    requestAnimationFrame(tick);
  }
  tick();
  micConnected = true;
}

function flushPlayback() {
  playQueue.length = 0;
  if (currentSource) { try { currentSource.stop(); } catch (e) {} currentSource = null; }
  if (currentGain) { currentGain.disconnect(); currentGain = null; }
  playing = false;
}

function pcmToFloat32(int16) {
  const f = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    f[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);
  }
  return f;
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
  src.playbackRate.value = window.PLAYBACK_RATE || 1.1;
  const gain = audioCtx.createGain();
  src.connect(gain).connect(masterGain);
  currentSource = src; currentGain = gain;
  src.onended = () => {
    currentSource = null; currentGain = null; playing = false; playNext();
  };
  src.start();
}

let pendingAudio = null;

function handleWsText(payload) {
  const msg = JSON.parse(payload);
  switch (msg.type) {
    case "ready":
      setStatus("ready");
      setStage("idle", "idle");
      break;
    case "status":
      setStage(msg.stage, msg.text || msg.stage); break;
    case "transcript":
      finalizeUserPartial(msg.text || ""); break;
    case "stt_partial":
      if (msg.text) updateUserPartial(msg.text); break;
    case "agent_text":
      appendAssistantDelta(msg.delta); break;
    case "agent_turn_end":
      endAssistantStream(); break;
    case "tool_use":
      logToolLine("tool", "→ tool:", msg.body); break;
    case "tool_result":
      logToolLine("tool-result", "← result:", msg.body); break;
    case "show":
      logShow(msg.content, msg.kind); break;
    case "audio":
      pendingAudio = { sampleRate: msg.sample_rate }; break;
    case "latency":
      setLatency(msg.stage, msg.ms); break;
    case "error":
      logTranscript("error", msg.message); break;
  }
}

function handleWsBinary(buf) {
  if (!pendingAudio) return;
  const pcm = new Int16Array(buf);
  playQueue.push({ sampleRate: pendingAudio.sampleRate, pcm });
  pendingAudio = null;
  playNext();
}

// ── pickers ────────────────────────────────────────────────────────────

function renderKnobs(container, spec, savedPrefix) {
  container.innerHTML = "";
  if (!spec.length) return;
  for (const k of spec) {
    const lbl = document.createElement("label");
    lbl.textContent = k.label + ":";
    const inp = document.createElement("input");
    inp.type = k.type || "text";
    if (k.step) inp.step = k.step;
    if (k.placeholder) inp.placeholder = k.placeholder;
    inp.dataset.key = k.key;
    const saved = localStorage.getItem(`${savedPrefix}.${k.key}`);
    if (saved !== null) inp.value = saved;
    inp.addEventListener("change", () => {
      localStorage.setItem(`${savedPrefix}.${k.key}`, inp.value);
    });
    container.appendChild(lbl);
    container.appendChild(inp);
  }
}

function collectKnobs(container) {
  const params = {};
  for (const inp of container.querySelectorAll("input")) {
    if (inp.value === "") continue;
    const v = inp.type === "number" ? Number(inp.value) : inp.value;
    params[inp.dataset.key] = v;
  }
  return params;
}

async function loadEngines() {
  const r = await fetch("/engines");
  if (!r.ok) throw new Error(`/engines → ${r.status}`);
  const data = await r.json();
  fillSelect(ui.sttEngine, data.stt, localStorage.getItem("picker.stt") || "whisper");
  fillSelect(ui.ttsEngine, data.tts, localStorage.getItem("picker.tts") || "piper");
  fillSelect(ui.llmEngine, data.llm, localStorage.getItem("picker.llm") || "claude");
  renderKnobs(ui.ttsKnobs, TTS_KNOBS[ui.ttsEngine.value] || [], `tts.${ui.ttsEngine.value}`);
  renderKnobs(ui.llmKnobs, LLM_KNOBS[ui.llmEngine.value] || [], `llm.${ui.llmEngine.value}`);
}

function fillSelect(sel, options, preferred) {
  sel.innerHTML = "";
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o;
    if (o === preferred) opt.selected = true;
    sel.appendChild(opt);
  }
}

ui.sttEngine.addEventListener("change", () => {
  localStorage.setItem("picker.stt", ui.sttEngine.value);
});
ui.ttsEngine.addEventListener("change", () => {
  localStorage.setItem("picker.tts", ui.ttsEngine.value);
  renderKnobs(ui.ttsKnobs, TTS_KNOBS[ui.ttsEngine.value] || [], `tts.${ui.ttsEngine.value}`);
});
ui.llmEngine.addEventListener("change", () => {
  localStorage.setItem("picker.llm", ui.llmEngine.value);
  renderKnobs(ui.llmKnobs, LLM_KNOBS[ui.llmEngine.value] || [], `llm.${ui.llmEngine.value}`);
});
ui.applyBtn.addEventListener("click", () => { applyAndReconnect(); });

// ── /call/start + WS lifecycle ────────────────────────────────────────

async function applyAndReconnect() {
  setApplyStatus("starting…");
  // Tear down any existing call before opening a new one.
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  if (currentCallId) {
    fetch(`/call/${currentCallId}/end`, { method: "POST" }).catch(() => {});
    currentCallId = null;
  }

  const body = {
    config: {
      stt: { engine: ui.sttEngine.value, params: {} },
      tts: { engine: ui.ttsEngine.value, params: collectKnobs(ui.ttsKnobs) },
      llm: { engine: ui.llmEngine.value, params: collectKnobs(ui.llmKnobs) },
    },
    llm_binding: {
      kind: "inline",
      engine: ui.llmEngine.value,
      params: collectKnobs(ui.llmKnobs),
    },
  };

  let resp;
  try {
    resp = await fetch("/call/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    setApplyStatus(`fetch failed: ${err.message}`);
    return;
  }
  if (!resp.ok) {
    const txt = await resp.text();
    setApplyStatus(`/call/start ${resp.status}: ${txt.slice(0, 200)}`);
    return;
  }
  const data = await resp.json();
  currentCallId = data.call_id;
  setApplyStatus(`call ${data.call_id} connecting…`);

  // /call/start returns an absolute ws_url derived from request host.
  const wsUrl = data.ws_url;
  ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setStatus("connected");
    setApplyStatus(`call ${data.call_id} live`);
  };
  ws.onclose = () => { setStatus("disconnected"); };
  ws.onerror = () => { setStatus("error"); };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") handleWsText(e.data);
    else handleWsBinary(e.data);
  };
}

async function pttDown() {
  if (recording) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setApplyStatus("not connected — click apply & reconnect");
    return;
  }
  await startMicCapture();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  flushPlayback();
  recording = true;
  ui.ptt.classList.add("active");
  ws.send(JSON.stringify({ type: "start" }));
}

function pttUp() {
  if (!recording) return;
  recording = false;
  ui.ptt.classList.remove("active");
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "end" }));
  }
}

window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && document.activeElement === document.body) {
    e.preventDefault(); pttDown();
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space") { e.preventDefault(); pttUp(); }
});
ui.ptt.addEventListener("mousedown", pttDown);
ui.ptt.addEventListener("mouseup", pttUp);
ui.ptt.addEventListener("mouseleave", () => recording && pttUp());
ui.ptt.addEventListener("touchstart", (e) => { e.preventDefault(); pttDown(); });
ui.ptt.addEventListener("touchend", (e) => { e.preventDefault(); pttUp(); });

// ── text input (STT bypass) ──────────────────────────────────────────
// Sends a typed phrase as if STT had finalized it. Same LLM/TTS path,
// just deterministic input — useful for A/B'ing TTS voices on one phrase.

async function sendText() {
  const text = (ui.textInput.value || "").trim();
  if (!text) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setApplyStatus("not connected — click apply & reconnect");
    return;
  }
  await ensureAudio();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  flushPlayback();
  ws.send(JSON.stringify({ type: "text", text }));
  ui.textInput.value = "";
}

ui.textSendBtn.addEventListener("click", () => { sendText(); });
ui.textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendText(); }
});

// Bootstrap: load engines, render pickers, auto-connect with saved selections.
loadEngines().then(() => applyAndReconnect()).catch(err => {
  setApplyStatus(`engines load failed: ${err.message}`);
});
