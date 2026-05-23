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
        `${location.protocol}//${location.hostname}:12103`;

    let audioCtx = null;       // lazily created on first click
    let nextStartTime = 0;     // schedule time for the next chunk
    let activeAbort = null;    // current AbortController, if any

    const $ = (sel) => document.querySelector(sel);

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
        if (activeAbort) { activeAbort.abort(); activeAbort = null; }
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        else if (audioCtx.state === "suspended") await audioCtx.resume();
        nextStartTime = audioCtx.currentTime;

        const text = $("#syn-text").value.trim();
        if (!text) return;

        const body = {
            text,
            tts_voice: $("#syn-tts").value,
            rvc_label: $("#syn-rvc").value || null,
            pitch: parseInt($("#syn-pitch").value, 10) || 0,
        };
        const ac = new AbortController();
        activeAbort = ac;
        const t0 = performance.now();
        $("#syn-status").textContent = "streaming…";
        $("#syn-btn").disabled = true;

        let resp;
        try {
            resp = await fetch(`${SIDECAR_URL}/stream`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
                signal: ac.signal,
            });
        } catch (err) {
            $("#syn-status").textContent = `fetch failed: ${err.message}`;
            $("#syn-btn").disabled = false;
            return;
        }
        if (!resp.ok) {
            const err = await resp.text();
            $("#syn-status").textContent = `${resp.status}: ${err.slice(0, 200)}`;
            $("#syn-btn").disabled = false;
            return;
        }
        const sr = parseInt(resp.headers.get("X-Sample-Rate") || "24000", 10);

        const reader = resp.body.getReader();
        let buf = new Uint8Array(0);
        let firstAudioMs = null;
        let chunkCount = 0;
        let totalSamples = 0;

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
                const lenBytes = await readN(4);
                if (!lenBytes) break;
                const len = new DataView(lenBytes.buffer, lenBytes.byteOffset, 4)
                    .getUint32(0, true);
                if (len === 0) continue;
                const payload = await readN(len);
                if (!payload) break;

                // int16 LE → float32
                const i16 = new Int16Array(payload.buffer, payload.byteOffset, len / 2);
                const f32 = new Float32Array(i16.length);
                for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;

                const ab = audioCtx.createBuffer(1, f32.length, sr);
                ab.copyToChannel(f32, 0);
                const src = audioCtx.createBufferSource();
                src.buffer = ab;
                src.connect(audioCtx.destination);
                const startAt = Math.max(nextStartTime, audioCtx.currentTime);
                src.start(startAt);
                nextStartTime = startAt + ab.duration;

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
            $("#syn-btn").disabled = false;
            activeAbort = null;
        }
    }

    // --- wiring ---------------------------------------------------------

    function init() {
        $("#syn-btn").addEventListener("click", streamSynth);
        $("#syn-pitch").addEventListener("input", e => {
            $("#syn-pitch-val").textContent = `${e.target.value} st`;
        });
        $("#syn-text").addEventListener("keydown", e => {
            // Ctrl/Cmd+Enter triggers synthesis
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") streamSynth();
        });
        loadVoices();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else { init(); }
})();
