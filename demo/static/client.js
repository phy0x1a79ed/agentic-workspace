// Browser client: push-to-talk mic capture + WS protocol + WebAudio playback queue.

const ui = {
  status: document.getElementById("status"),
  stage: document.getElementById("stage"),
  ptt: document.getElementById("ptt"),
  transcript: document.getElementById("transcript"),
  latency: document.getElementById("latency"),
  meterVal: document.getElementById("meter-val"),
  vol: document.getElementById("vol"),
  volVal: document.getElementById("vol-val"),
  rvc: document.getElementById("rvc"),
  rvcStatus: document.getElementById("rvc-status"),
};

let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let micConnected = false;
let recording = false;
let masterGain = null;  // TTS playback volume

function readVol() {
  return (parseInt(ui.vol.value, 10) || 0) / 100;
}
ui.vol.addEventListener("input", () => {
  const v = readVol();
  ui.volVal.textContent = `${Math.round(v * 100)}%`;
  if (masterGain) masterGain.gain.value = v;
});

ui.rvc.addEventListener("change", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "config", rvc: ui.rvc.checked }));
  }
});

// Playback queue: each entry is {sampleRate, pcm: Int16Array}
const playQueue = [];
let playing = false;
let currentSource = null;
let currentGain = null;

function setStatus(s) {
  ui.status.textContent = s;
}

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
  // Master gain for TTS playback.
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
  // Log track info — helps diagnose silent-mic issues.
  const track = micStream.getAudioTracks()[0];
  console.log("mic track:", track && track.label, "settings:", track && track.getSettings && track.getSettings(), "enabled:", track && track.enabled, "muted:", track && track.muted);

  const src = audioCtx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(audioCtx, "mic-downsampler");
  workletNode.port.onmessage = (e) => {
    if (!recording || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(e.data);
  };
  src.connect(workletNode);
  // Connect worklet to destination through a muted gain to keep it alive.
  const silent = audioCtx.createGain();
  silent.gain.value = 0;
  workletNode.connect(silent).connect(audioCtx.destination);

  // Live mic meter — independent of the worklet path, so a flat meter
  // means the browser isn't getting audio at all.
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
  if (currentSource) {
    try {
      currentSource.stop();
    } catch (e) {}
    currentSource = null;
  }
  if (currentGain) {
    currentGain.disconnect();
    currentGain = null;
  }
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
  currentSource = src;
  currentGain = gain;
  src.onended = () => {
    currentSource = null;
    currentGain = null;
    playing = false;
    playNext();
  };
  src.start();
}

let pendingAudio = null; // {sampleRate}

function handleWsText(payload) {
  const msg = JSON.parse(payload);
  switch (msg.type) {
    case "ready":
      setStatus("ready");
      setStage("idle", "idle");
      if (msg.rvc_available) {
        ui.rvc.disabled = false;
        ui.rvcStatus.textContent = "(available — toggle to apply Chelly_Egoist voice)";
      } else {
        ui.rvc.disabled = true;
        ui.rvcStatus.textContent = "(sidecar not running — see README to start awm-rvc service)";
      }
      break;
    case "config_ack":
      ui.rvc.checked = !!msg.rvc;
      ui.rvc.disabled = !msg.rvc_available;
      ui.rvcStatus.textContent = msg.rvc
        ? "(on)"
        : (msg.rvc_available ? "(off)" : "(sidecar not running)");
      break;
    case "status":
      setStage(msg.stage, msg.text || msg.stage);
      break;
    case "transcript":
      finalizeUserPartial(msg.text || "");
      break;
    case "stt_partial":
      if (msg.text) updateUserPartial(msg.text);
      break;
    case "agent_text":
      appendAssistantDelta(msg.delta);
      break;
    case "agent_turn_end":
      endAssistantStream();
      break;
    case "tool_use":
      logToolLine("tool", "→ tool:", msg.body);
      break;
    case "tool_result":
      logToolLine("tool-result", "← result:", msg.body);
      break;
    case "show":
      logShow(msg.content, msg.kind);
      break;
    case "audio":
      pendingAudio = { sampleRate: msg.sample_rate };
      break;
    case "latency":
      setLatency(msg.stage, msg.ms);
      break;
    case "error":
      logTranscript("error", msg.message);
      break;
  }
}

function handleWsBinary(buf) {
  if (!pendingAudio) return;
  const pcm = new Int16Array(buf);
  playQueue.push({ sampleRate: pendingAudio.sampleRate, pcm });
  pendingAudio = null;
  playNext();
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => setStatus("connected");
  ws.onclose = () => setStatus("disconnected");
  ws.onerror = () => setStatus("error");
  ws.onmessage = (e) => {
    if (typeof e.data === "string") {
      handleWsText(e.data);
    } else {
      handleWsBinary(e.data);
    }
  };
}

async function pttDown() {
  if (recording) return;
  await startMicCapture();
  if (audioCtx.state === "suspended") {
    await audioCtx.resume();
  }
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

window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && document.activeElement === document.body) {
    e.preventDefault();
    pttDown();
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space") {
    e.preventDefault();
    pttUp();
  }
});
ui.ptt.addEventListener("mousedown", pttDown);
ui.ptt.addEventListener("mouseup", pttUp);
ui.ptt.addEventListener("mouseleave", () => recording && pttUp());
ui.ptt.addEventListener("touchstart", (e) => { e.preventDefault(); pttDown(); });
ui.ptt.addEventListener("touchend", (e) => { e.preventDefault(); pttUp(); });

connectWs();
