// Voice side panel — always-visible in the control center.
//
// Connects to /voice/ws using the bearer token from the URL hash, runs
// push-to-talk capture via mic-worklet, plays back streaming audio,
// and renders transcript + show() boxes. Cross-window state (joined
// rooms, transcripts, audio) is broadcast by the server so multiple
// tabs of the same user stay in sync.

(function () {
  "use strict";

  // ---- Identity (display name only — auth lives in the HttpOnly cookie) ---

  function readCookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  const cfg = {
    as: "user:" + (readCookie("awm_as") || "operator"),
  };
  // Auth lives in the `awm_session` HttpOnly cookie set by /auth/bootstrap
  // (entered via the Discord bot's /login or `awm login`). fetch sends it
  // with `credentials: include`; the WS handshake sends cookies natively.

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
      credentials: "include",
      headers: { "X-Awm-As": cfg.as },
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

  // Tiny helper: bind a listener only if the element actually exists.
  // Lets us delete DOM nodes from index.html without crashing the module.
  function on(el, evt, fn, opts) {
    if (el) el.addEventListener(evt, fn, opts);
  }

  function readVol() {
    return ui.vol ? (parseInt(ui.vol.value, 10) || 0) / 100 : 1.0;
  }
  on(ui.vol, "input", () => {
    const v = readVol();
    if (ui.volVal) ui.volVal.textContent = `${Math.round(v * 100)}%`;
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
      if (ui.meterVal) ui.meterVal.textContent = `${rms.toFixed(3)} ${bar}`;
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
    if (!ui.conn) return;
    ui.conn.className = "vp-conn " + (state || "");
    ui.conn.textContent = label;
  }

  function setStage(stage, text) {
    if (!ui.stage) return;
    ui.stage.className = "vp-stage " + (stage || "");
    ui.stage.textContent = text || stage || "idle";
  }

  function appendLine(role, text) {
    if (!ui.transcript) return;
    const div = document.createElement("div");
    div.className = "vp-line " + role;
    div.textContent = `${role}: ${text}`;
    ui.transcript.appendChild(div);
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
  }

  function appendAssistantDelta(text) {
    if (!ui.transcript) return;
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
    if (!ui.transcript) return;
    const last = ui.transcript.lastElementChild;
    if (last && last.classList.contains("assistant-stream")) {
      last.classList.remove("assistant-stream");
    }
  }

  function renderShow(content, kind) {
    if (!ui.transcript) return;
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
    if (!ui.latency) return;
    ui.latency.dataset[stage] = ms;
    ui.latency.textContent =
      `STT ${ui.latency.dataset.stt ?? "—"}ms · ` +
      `first-token ${ui.latency.dataset.first_token ?? "—"}ms · ` +
      `first-audio ${ui.latency.dataset.first_audio ?? "—"}ms`;
  }

  let joinedRoomIds = [];
  const agentPollers = new Map(); // room_id -> { card, agentsBox }

  function renderJoinedRooms(rooms) {
    joinedRoomIds = Array.isArray(rooms) ? rooms.slice() : [];
    if (!ui.roomsList) return;  // DOM was removed (M7); state already tracked above.
    if (!joinedRoomIds.length) {
      ui.roomsList.innerHTML = "<em>none</em>";
      agentPollers.clear();
      return;
    }
    ui.roomsList.innerHTML = "";
    agentPollers.clear();
    for (const r of joinedRoomIds) {
      const card = document.createElement("div");
      card.className = "vp-room-card";
      const header = document.createElement("div");
      header.className = "vp-room-card-header";
      const label = document.createElement("span");
      label.className = "vp-chip";
      label.textContent = r;
      const leave = document.createElement("button");
      leave.type = "button";
      leave.textContent = "×";
      leave.title = `leave ${r}`;
      leave.addEventListener("click", () => leaveRoom(r));
      label.appendChild(leave);
      header.appendChild(label);
      card.appendChild(header);
      const agentsBox = document.createElement("div");
      agentsBox.className = "vp-agents";
      agentsBox.textContent = "…";
      card.appendChild(agentsBox);
      ui.roomsList.appendChild(card);
      agentPollers.set(r, { card, agentsBox });
    }
    refreshAgentCards();
  }

  async function refreshAgentCards() {
    for (const [room_id, slot] of agentPollers.entries()) {
      try {
        const r = await api("GET", `/rooms/${encodeURIComponent(room_id)}/agents`);
        renderAgentsForRoom(room_id, slot.agentsBox, r.agents || []);
      } catch (e) {
        slot.agentsBox.innerHTML = `<div class="vp-agent-err">${escapeHtml(e.message)}</div>`;
      }
    }
  }

  function renderAgentsForRoom(room_id, box, agents) {
    box.innerHTML = "";
    const scopeAgents = agents.filter(a => a.kind === "scope");
    if (!scopeAgents.length) {
      box.innerHTML = `<div class="vp-agent-empty">no live agents</div>`;
      return;
    }
    for (const a of scopeAgents) {
      const row = document.createElement("div");
      row.className = "vp-agent-row";
      const scope = document.createElement("div");
      scope.className = "vp-agent-scope";
      scope.textContent = a.scope;
      row.appendChild(scope);

      const live = a.live;
      if (live && typeof live.context_max === "number" && live.context_max > 0) {
        const bar = document.createElement("div");
        bar.className = "vp-agent-bar";
        const fill = document.createElement("div");
        fill.className = "vp-agent-bar-fill";
        const used = typeof live.context_used === "number" ? live.context_used : 0;
        const pct = Math.min(100, Math.max(0, (used / live.context_max) * 100));
        fill.style.width = `${pct.toFixed(1)}%`;
        if (pct >= 90) fill.classList.add("hot");
        else if (pct >= 70) fill.classList.add("warm");
        bar.appendChild(fill);
        bar.title = `${used.toLocaleString()} / ${live.context_max.toLocaleString()} tokens`;
        row.appendChild(bar);
      }

      const actions = document.createElement("div");
      actions.className = "vp-agent-actions";
      const liveOk = live && live.status === "running";
      for (const [label, cmd] of [["compact", "/compact"], ["restart", "/restart"], ["yolo", "/yolo"]]) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = label;
        btn.disabled = !liveOk;
        btn.title = liveOk ? `send ${cmd} to ${a.scope}` : `no live session`;
        btn.addEventListener("click", () => sendAgentSlash(room_id, a.scope, cmd, btn));
        actions.appendChild(btn);
      }
      row.appendChild(actions);
      box.appendChild(row);
    }
  }

  async function sendAgentSlash(room_id, scope, cmd, btn) {
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const r = await api(
        "POST",
        `/rooms/${encodeURIComponent(room_id)}/agents/${encodeURIComponent(scope)}/slash`,
        { cmd },
      );
      btn.textContent = r.handled ? "ok" : "sent";
      setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 1200);
      refreshAgentCards();
    } catch (e) {
      btn.textContent = "err";
      btn.title = e.message;
      setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 2000);
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  setInterval(() => {
    if (agentPollers.size > 0) refreshAgentCards();
  }, 5000);

  // ---- WebSocket ----------------------------------------------------------

  let ws = null;
  let wsBackoff = 1000;

  function currentRoomFromHash() {
    // Hash-routed SPA: `#/room/{id}` → return `{id}`, else null.
    const h = location.hash || "";
    const m = h.match(/^#\/?room\/([^/?#]+)/);
    return m ? m[1] : null;
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // Cookie carries auth; no subprotocol bearer needed.
    // If the SPA is in a room, bind voice turns to that room's transcript.
    const roomId = currentRoomFromHash();
    const qs = roomId ? `?room_id=${encodeURIComponent(roomId)}` : "";
    ws = new WebSocket(`${proto}//${location.host}/voice/ws${qs}`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      setConn("ok", "connected");
      wsBackoff = 1000;
      // Re-establish focus context after any reconnect.
      if (lastFocusFrame) {
        try { ws.send(JSON.stringify({ type: "focus", ...lastFocusFrame })); }
        catch {}
      }
    };
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
      case "joined_rooms": {
        const rooms = msg.rooms || [];
        renderJoinedRooms(rooms);
        // Broadcast for other widgets (focus rail, reconcile loop) so they
        // can act on push-updated voice state without polling /voice/state.
        try {
          window.dispatchEvent(new CustomEvent("awm-voice-joined-rooms",
                                               { detail: { rooms } }));
        } catch {}
        break;
      }
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
    if (ui.ptt) ui.ptt.classList.add("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "start" }));
    }
  }

  function pttUp() {
    if (!recording) return;
    recording = false;
    if (ui.ptt) ui.ptt.classList.remove("active");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "end" }));
    }
  }

  on(ui.ptt, "mousedown", pttDown);
  on(ui.ptt, "mouseup", pttUp);
  on(ui.ptt, "mouseleave", () => recording && pttUp());
  on(ui.ptt, "touchstart", (e) => { e.preventDefault(); pttDown(); });
  on(ui.ptt, "touchend", (e) => { e.preventDefault(); pttUp(); });

  // Spacebar PTT — but only when no text input is focused and the focus
  // tab is actually visible (the PTT button lives there now; firing it
  // on hidden tabs would mutate a non-rendered element).
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

  // ---- Typed input --------------------------------------------------------

  async function sendText() {
    if (!ui.textInput) return;
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
  on(ui.textSend, "click", sendText);
  on(ui.textInput, "keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendText(); }
  });

  // ---- Room join/leave (legacy in-panel picker; M3 deletes its DOM in
  // the focus tab. Handlers stay guarded for any other surfaces.) -----------

  on(ui.joinToggle, "click", async () => {
    if (ui.roomPicker) ui.roomPicker.hidden = false;
    ui.joinToggle.disabled = true;
    try {
      const r = await api("GET", "/rooms?status=active");
      const sel = ui.roomSelect;
      if (!sel) return;
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

  on(ui.roomCancel, "click", () => {
    if (ui.roomPicker) ui.roomPicker.hidden = true;
  });

  on(ui.roomJoin, "click", async () => {
    const roomId = ui.roomSelect && ui.roomSelect.value;
    if (!roomId) return;
    try {
      await api("POST", `/voice/rooms/${encodeURIComponent(roomId)}/join`);
      if (ui.roomPicker) ui.roomPicker.hidden = true;
    } catch (e) {
      appendLine("error", e.message);
    }
  });

  async function leaveRoom(roomId) {
    try { await api("POST", `/voice/rooms/${encodeURIComponent(roomId)}/leave`); }
    catch (e) { appendLine("error", e.message); }
  }

  // ---- Silent voice-join reconciliation -----------------------------------
  //
  // The focus rail's source of truth is the vagrant scope's room
  // participation (see refreshFocusRail in index.html). Whenever the rail
  // refreshes we ensure the voice agent has joined every room shown so
  // its room_post tool calls actually reach those transcripts. Join-only
  // — we never auto-leave; voice membership is a superset of rail rooms.
  // Authoritative "already joined" state comes from joinedRoomIds, which
  // tracks the server's last `joined_rooms` WS broadcast.

  const voiceJoinAttempted = new Set();   // permanent-fail dedupe per room
  const voiceJoinInFlight = new Set();    // in-flight dedupe per room

  async function reconcileVoiceJoins(rooms) {
    if (!Array.isArray(rooms)) return;
    const already = new Set(joinedRoomIds);
    for (const rid of rooms) {
      if (!rid) continue;
      if (already.has(rid)) continue;
      if (voiceJoinAttempted.has(rid)) continue;
      if (voiceJoinInFlight.has(rid)) continue;
      voiceJoinInFlight.add(rid);
      try {
        await api("POST", `/voice/rooms/${encodeURIComponent(rid)}/join`);
      } catch (e) {
        // Don't surface as banner noise; common cases (closing room, 409
        // already-joined races) shouldn't loop. Log once for debuggability.
        voiceJoinAttempted.add(rid);
        try { console.warn(`[voice] silent join failed for ${rid}:`, e.message); }
        catch {}
      } finally {
        voiceJoinInFlight.delete(rid);
      }
    }
  }

  window.addEventListener("awm-focus-rail-changed", (e) => {
    const rooms = (e && e.detail && e.detail.rooms) || [];
    reconcileVoiceJoins(rooms);
  });

  // ---- Focus frame (forwarded from the focus tab) -------------------------
  //
  // The control-center focus tab dispatches `awm-focus-change` whenever
  // the focused room or recipient changes. We forward that to the voice
  // session over /voice/ws so the voice agent knows where to direct
  // its room_post tool calls (manager / specific worker / all workers).
  // We also resend the last known focus on every reconnect so the
  // agent's context isn't lost after a server restart or network blip.

  let lastFocusFrame = null;

  function sendFocusFrame(frame) {
    lastFocusFrame = frame;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "focus", ...frame })); }
      catch {}
    }
  }

  window.addEventListener("awm-focus-change", (e) => {
    if (!e || !e.detail) return;
    sendFocusFrame({
      room_id: e.detail.room_id || null,
      recipient: e.detail.recipient || null,
    });
  });

  // ---- Boot ---------------------------------------------------------------

  setConn("", "connecting…");
  connectWs();
})();
