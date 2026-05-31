// Streaming TTS+RVC client for the samples page.
// Talks to the tts_rvc_service sidecar (default port 7833).
// Reads length-prefixed int16 PCM frames from a streaming POST response,
// converts each to an AudioBuffer, and schedules them head-to-tail on a
// single AudioContext so playback is seamless.

(() => {
    // Sidecar matches the page's protocol so HTTPS pages don't hit mixed content.
    // Override via ?sidecar=https://host:port
    const params = new URLSearchParams(location.search);
    const SIDECAR_URL = params.get("sidecar") ||
        `${location.protocol}//${location.hostname}:12123`;

    let audioCtx = null;          // lazily created on first click
    let nextStartTime = 0;        // schedule time for the next chunk
    let activeSources = [];       // AudioBufferSourceNodes currently playing
    let activeAbort = null;       // AbortController for the current HTTP stream
    let streamGen = 0;            // incremented each new stream, guards stale callbacks

    const $ = (sel) => document.querySelector(sel);

    function stopPlayback() {
        if (activeAbort) {
            activeAbort.abort();
            activeAbort = null;
        }
        for (const src of activeSources) {
            try { src.stop(); } catch (_) {}
            try { src.disconnect(); } catch (_) {}
        }
        activeSources = [];
        nextStartTime = 0;
        $("#syn-stop-btn").classList.remove("active");
        $("#syn-status").textContent = "stopped";
    }

    // --- Voice loading --------------------------------------------------

    async function loadVoices() {
        const tts = $("#syn-tts");
        const rvc = $("#syn-rvc");
        try {
            const r = await fetch(`${SIDECAR_URL}/voices`);
            if (!r.ok) throw new Error(`/voices HTTP ${r.status}`);
            const data = await r.json();

            // TTS dropdown: group by accent+gender prefix.
            tts.innerHTML = "";
            const groups = {
                "American female": [], "American male": [],
                "British female": [],  "British male": [],
            };
            for (const v of data.tts) {
                const key = v.startsWith("af_") ? "American female"
                    : v.startsWith("am_") ? "American male"
                    : v.startsWith("bf_") ? "British female"
                    : v.startsWith("bm_") ? "British male" : null;
                if (key) groups[key].push(v);
            }
            for (const [label, vs] of Object.entries(groups)) {
                if (!vs.length) continue;
                const og = document.createElement("optgroup");
                og.label = label;
                for (const v of vs) {
                    const o = document.createElement("option");
                    o.value = v; o.textContent = v;
                    if (v === "bf_emma") o.selected = true;
                    og.appendChild(o);
                }
                tts.appendChild(og);
            }

            // RVC dropdown: "no RVC" + manifest entries.
            rvc.innerHTML = "";
            const none = document.createElement("option");
            none.value = ""; none.textContent = "— Raw Kokoro (no RVC) —";
            rvc.appendChild(none);
            for (const v of data.rvc) {
                const o = document.createElement("option");
                o.value = v.label;
                const tag = v.has_index ? "" : " (no index)";
                o.textContent = `${v.label}${tag}`;
                if (v.label === "chelly_egoist") o.selected = true;
                rvc.appendChild(o);
            }
            $("#syn-status").textContent =
                `ready · ${data.tts.length} TTS × ${data.rvc.length} RVC voices`;
        } catch (err) {
            const probeUrl = `${SIDECAR_URL}/health`;
            $("#syn-status").innerHTML =
                `Sidecar unreachable at <code>${SIDECAR_URL}</code>. ` +
                `If you haven't accepted the cert for this port yet, ` +
                `<a href="${probeUrl}" target="_blank" style="color:#8cf">open ${probeUrl}</a> ` +
                `once and click through the warning, then refresh this page.`;
            console.error(err);
        }
    }

    // --- Streaming player ------------------------------------------------

    async function streamSynth() {
        // Kill any previous playback.
        stopPlayback();

        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        else if (audioCtx.state === "suspended") await audioCtx.resume();
        nextStartTime = audioCtx.currentTime;

        const text = $("#syn-text").value.trim();
        if (!text) return;
        const myGen = ++streamGen;

        const body = {
            text,
            tts_voice: $("#syn-tts").value,
            rvc_label: $("#syn-rvc").value || null,
            pitch: parseInt($("#syn-pitch").value, 10) || 0,
            speed: parseFloat($("#syn-speed").value) || 1.0,
        };
        const ac = new AbortController();
        activeAbort = ac;
        const t0 = performance.now();
        $("#syn-status").textContent = "streaming…";
        $("#syn-stop-btn").classList.add("active");

        let resp;
        try {
            resp = await fetch(`${SIDECAR_URL}/stream`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
                signal: ac.signal,
            });
        } catch (err) {
            if (activeAbort === ac) activeAbort = null;
            if (err.name !== "AbortError") {
                $("#syn-status").textContent = `fetch failed: ${err.message}`;
            }
            $("#syn-stop-btn").classList.remove("active");
            return;
        }
        if (!resp.ok) {
            if (activeAbort === ac) activeAbort = null;
            const err = await resp.text();
            $("#syn-status").textContent = `${resp.status}: ${err.slice(0, 200)}`;
            $("#syn-stop-btn").classList.remove("active");
            return;
        }
        const sr = parseInt(resp.headers.get("X-Sample-Rate") || "24000", 10);

        const reader = resp.body.getReader();
        let buf = new Uint8Array(0);
        let firstAudioMs = null;
        let chunkCount = 0;
        let totalSamples = 0;
        let streamDone = false;  // becomes true when the HTTP stream ends

        // Helper: read exactly n bytes from the stream, returns null at EOF.
        async function readN(n) {
            while (buf.length < n) {
                const { value, done } = await reader.read();
                if (done) return null;
                const merged = new Uint8Array(buf.length + value.length);
                merged.set(buf); merged.set(value, buf.length);
                buf = merged;
            }
            const out = buf.subarray(0, n);
            buf = buf.subarray(n);
            return out;
        }

        try {
            while (true) {
                const hdrBytes = await readN(12);
                if (!hdrBytes) break;
                const hdrView = new DataView(hdrBytes.buffer, hdrBytes.byteOffset, 12);
                const len = hdrView.getUint32(0, true);
                const headOverlap = hdrView.getUint32(4, true);
                const tailOverlap = hdrView.getUint32(8, true);
                if (len === 0) continue;
                const payload = await readN(len);
                if (!payload) break;

                // int16 LE → float32
                const i16 = new Int16Array(payload.buffer, payload.byteOffset, len / 2);
                const f32 = new Float32Array(i16.length);
                for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;

                // Fade-in head_overlap samples (matches previous frame's
                // fade-out tail — they overlap in playback time and sum to 1).
                // Fade-out tail_overlap samples (matches next frame's fade-in).
                const headFade = Math.min(headOverlap, f32.length);
                for (let i = 0; i < headFade; i++) f32[i] *= i / headFade;
                const tailFade = Math.min(tailOverlap, f32.length - headFade);
                for (let i = 0; i < tailFade; i++) {
                    f32[f32.length - 1 - i] *= i / tailFade;
                }
                const tailFadeSec = tailFade / sr;

                const ab = audioCtx.createBuffer(1, f32.length, sr);
                ab.copyToChannel(f32, 0);
                const src = audioCtx.createBufferSource();
                src.buffer = ab;
                src.connect(audioCtx.destination);
                const startAt = Math.max(nextStartTime, audioCtx.currentTime);
                src.start(startAt);
                src.onended = () => {
                    const idx = activeSources.indexOf(src);
                    if (idx !== -1) activeSources.splice(idx, 1);
                    if (activeSources.length === 0 && streamGen === myGen && streamDone) {
                        stopPlayback();
                    }
                };
                activeSources.push(src);
                // Schedule next frame so its head_overlap fade-in region
                // overlaps this frame's tail_overlap fade-out region in time.
                nextStartTime = startAt + ab.duration - tailFadeSec;

                chunkCount++;
                totalSamples += f32.length;
                if (firstAudioMs === null) {
                    firstAudioMs = Math.round(performance.now() - t0);
                    $("#syn-status").textContent =
                        `TTFA ${firstAudioMs}ms · streaming…`;
                }
            }
            const totalMs = Math.round(performance.now() - t0);
            const audioSec = (totalSamples / sr).toFixed(1);
            $("#syn-status").textContent =
                `TTFA ${firstAudioMs ?? "—"}ms · ${chunkCount} chunks · ` +
                `${audioSec}s audio · ${totalMs}ms wall (sr=${sr})`;
        } catch (err) {
            if (err.name !== "AbortError") {
                $("#syn-status").textContent = `stream error: ${err.message}`;
                console.error(err);
            }
        } finally {
            streamDone = true;
            if (activeAbort === ac) activeAbort = null;
            if (activeSources.length === 0) {
                stopPlayback();
            }
        }
    }

    // --- persistence ----------------------------------------------------

    const KEYS = { tts: "syn_tts", rvc: "syn_rvc", pitch: "syn_pitch", speed: "syn_speed", text: "syn_text" };

    function saveSelections() {
        try {
            localStorage.setItem(KEYS.tts, $("#syn-tts").value);
            localStorage.setItem(KEYS.rvc, $("#syn-rvc").value);
            localStorage.setItem(KEYS.pitch, $("#syn-pitch").value);
            localStorage.setItem(KEYS.speed, $("#syn-speed").value);
            localStorage.setItem(KEYS.text, $("#syn-text").value);
        } catch (_) {}
    }

    function restoreSelections() {
        try {
            const tts = localStorage.getItem(KEYS.tts);
            const rvc = localStorage.getItem(KEYS.rvc);
            const speed = localStorage.getItem(KEYS.speed);
            const pitch = localStorage.getItem(KEYS.pitch);
            const text = localStorage.getItem(KEYS.text);
            if (tts && $("#syn-tts").querySelector(`option[value="${CSS.escape(tts)}"]`))
                $("#syn-tts").value = tts;
            if (rvc && $("#syn-rvc").querySelector(`option[value="${CSS.escape(rvc)}"]`))
                $("#syn-rvc").value = rvc;
            if (speed) {
                $("#syn-speed").value = speed;
                $("#syn-speed-val").textContent = `${speed}x`;
            }
            if (pitch) {
                $("#syn-pitch").value = pitch;
                $("#syn-pitch-val").textContent = `${pitch} st`;
            }
            if (text) $("#syn-text").value = text;
        } catch (_) {}
    }

    // Also save when the Stream button is clicked so the latest text is captured.
    const _origStream = streamSynth;
    streamSynth = function () {
        saveSelections();
        return _origStream.apply(this, arguments);
    };

    // --- wiring ---------------------------------------------------------

    function init() {
        $("#syn-stream-btn").addEventListener("click", streamSynth);
        $("#syn-stop-btn").addEventListener("click", stopPlayback);
        $("#syn-pitch").addEventListener("input", e => {
            $("#syn-pitch-val").textContent = `${e.target.value} st`;
            saveSelections();
        });
        $("#syn-speed").addEventListener("input", e => {
            $("#syn-speed-val").textContent = `${e.target.value}x`;
            saveSelections();
        });
        $("#syn-tts").addEventListener("change", saveSelections);
        $("#syn-rvc").addEventListener("change", saveSelections);
        $("#syn-text").addEventListener("keydown", e => {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") streamSynth();
        });
        loadVoices().then(restoreSelections);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else { init(); }
})();
