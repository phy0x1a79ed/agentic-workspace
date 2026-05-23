# Voice demo — mic → LLM → speakers

A standalone spike that proves the audio half of the eventual voice-bridged
representative agent. Push-to-talk in the browser, faster-whisper STT,
swappable LLM backend (OpenRouter free models by default, `claude` CLI
also available), Piper TTS (default) → optional RVC voice conversion →
speakers. No awm-rooms integration yet.

## Setup

```bash
# Python deps for the awm env (faster-whisper, pocket-tts, numpy)
cd /home/tony/agentic_workspace/projects/awm/voice
mamba env update -n awm -f environment.yml
```

Model downloads happen automatically on first launch:
- whisper `small.en` (~250 MB) → `~/.cache/huggingface/`
- pocket-tts weights (~440 MB) → `~/.cache/huggingface/`
- voice state for the default voice (`jean`) is fetched on demand

Total first-run download ≈ 700 MB. Subsequent starts use the cache.

## Run (local)

```bash
cd demo
VOICE_NAME=jean mamba run -n awm uvicorn server:app --host 127.0.0.1 --port 7830
```

Open <http://localhost:7830/> — `localhost` gets the browser's
secure-context exemption, so the mic works over plain HTTP here.

## Run (exposed over ZeroTier / LAN, HTTPS)

Any non-`localhost` origin requires HTTPS for `getUserMedia` to work in
the browser. One-time cert generation:

```bash
cd demo
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj '/CN=awm-voice-demo' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
  -keyout dev.key -out dev.crt
```

Launch on a port that's already routed through to this WSL VM (here
`12100`, in the user's pre-provisioned `12100-12150` exposed range):

```bash
cd demo
VOICE_NAME=jean mamba run -n awm uvicorn server:app \
  --host 0.0.0.0 --port 12100 \
  --ssl-keyfile dev.key --ssl-certfile dev.crt
```

Open `https://<host-ip>:12100/` from any ZeroTier peer. Chrome will
warn about the self-signed cert — click **Advanced → Proceed**. After
that the WebSocket rides on `wss://`, the mic prompt appears normally,
and the demo behaves the same as the local-only mode.

There's still **no auth** on the WS/REST surface. ZeroTier membership
is the trust boundary. For belt-and-suspenders, lift the bearer-token
pattern from `awm/middleware_auth.py`.

In either mode, grant mic permission on first load. If the live mic meter
stays flat while you speak, select the correct input device in Chrome's
site permissions for this origin.

## Configuration

Knobs you can tweak without code changes:

| Knob | Where | Default | What it does |
|---|---|---|---|
| LLM backend | `LLM_BACKEND` env | `openrouter` | `openrouter` (HTTP, free models, needs `OPENROUTER_API_KEY`) or `claude` (persistent `claude` CLI subprocess — original behavior). |
| OpenRouter model | `OPENROUTER_MODEL` env | `z-ai/glm-4.5-air:free` | Any model id from <https://openrouter.ai/models>. Free models with `:free` suffix are nominally free but heavily rate-limited; expect occasional 429s. The default is the fastest currently-responsive free model that supports tool use. |
| TTS backend | `TTS_BACKEND` env | `piper` | `piper` (fast, clean, ~20× realtime on CPU — also the cleanest RVC feedstock) or `pocket` (kyutai pocket-tts, more natural but slower). |
| Piper voice | `PIPER_VOICE` env | `demo/voices/piper/en_US-lessac-medium.onnx` | Path to any Piper `.onnx` (sibling `.onnx.json` required). |
| Pocket-TTS voice | `VOICE_NAME` env | `jean` | Only used when `TTS_BACKEND=pocket`. Any name from the pocket-tts catalog (`alba`, `eve`, `michael`, `paul`, etc.), an absolute path to a `.wav`/`.safetensors`, or an `hf://kyutai/tts-voices/...` URI. |
| Playback speed | `static/client.js` (`window.PLAYBACK_RATE`) | `1.1` | Multiplied onto `AudioBufferSourceNode.playbackRate`. Lifts pitch slightly; not pitch-preserved time-stretch. Override at runtime in DevTools: `window.PLAYBACK_RATE = 1.0`. |
| Playback volume | UI slider | `100%` | Master `GainNode` on the TTS path. 0–200%, live during a turn. |
| STT backend | `STT_BACKEND` env | `whisper` | `whisper` (faster-whisper, transcribe on PTT release; most accurate), `sherpa` (sherpa-onnx streaming zipformer; live partials, lower accuracy on conversational speech), `whisper-stream` (rolling-window faster-whisper; live partials, **slow on CPU — see caveat below**). |
| STT model | `WHISPER_MODEL` env | `small.en` | faster-whisper size used by the `whisper` backend: `tiny.en`, `base.en`, `small.en`, `medium.en`. |
| Streaming whisper model | `WHISPER_STREAM_MODEL` env | `base.en` | Model used by the `whisper-stream` backend. Smaller is faster but no streaming variant escapes the 30 s mel-padding cost. |
| STT compute | `WHISPER_COMPUTE` env | `int8` | `int8`, `int8_float16`, `float32`. |
| Sherpa model dir | `SHERPA_MODEL_DIR` env | `demo/models/sherpa-onnx-streaming-zipformer-en-2023-06-26` | Path to a sherpa-onnx streaming-zipformer model directory. |
| Sherpa threads | `SHERPA_NUM_THREADS` env | `2` | CPU threads for the sherpa decoder. |

### STT backend caveats

- `whisper-stream` works but is **slow on CPU**: faster-whisper pads every input to a fixed **30 s mel window** before the encoder runs, so a 0.4 s partial costs the same encoder time as a 28 s clip. On a CPU-only laptop the per-partial cost (~5–10 s of wall time) breaks the streaming UX. Use `whisper` for accuracy or `sherpa` for true streaming.
- `sherpa` uses a chunk-by-chunk encoder so partials are cheap (RTF ~0.09 on a CPU laptop), but the shipped 2023-06-26 English zipformer is LibriSpeech/GigaSpeech-trained and noticeably weaker than whisper on conversational speech. Server lowercases + sentence-cases the ALL-CAPS output before display.
- `whisper` (default before this scope) is non-streaming: the full clip is transcribed at PTT release. Most accurate, but release-to-final scales with clip length.
| Agent persona | `voice-agent.md` (or `VOICE_AGENT_PROMPT` env path) | bundled file | Markdown appended to claude's system prompt via `--append-system-prompt`. Edit and reload the page to apply. |
| Visual side channel | `mcp-config.json` + `show_mcp.py` | enabled | Registers the `show(content, kind)` MCP tool. Allowed-listed in claude's argv so it never prompts. |
| Markdown TTS scrubber | `text_clean.py` | always on | Defense-in-depth strip of stray bold/code/headings the agent might emit despite the persona. |

## Use

- **Hold SPACE** (or click the PTT button) to talk. Release to send.
- The **status pill** under the title cycles through `idle` → `listening`
  → `transcribing` → `thinking` → `responding` → `speaking` → `idle`.
- **Mic meter** shows live RMS — a flat zero while you speak means OS or
  browser hasn't routed the right input device.
- **Barge-in**: press SPACE again while the agent is talking. TTS stops
  immediately; the agent's text keeps streaming into the transcript so
  context isn't lost.
- **Tool calls** appear as dim transcript lines (`→ tool: …`,
  `← result: …`) but are never spoken.
- **Visual asides** from the agent appear as styled boxes (code,
  link, path, text) and are also never spoken.

## LLM backend

Two interchangeable backends share the same `(kind, body)` event protocol.

**OpenRouter** (default). Streams from OpenRouter's OpenAI-compatible
chat-completions endpoint. Needs `OPENROUTER_API_KEY` in the environment;
the conversation history lives in-process. Pick any model via
`OPENROUTER_MODEL`; default is `z-ai/glm-4.5-air:free` because it was the
fastest free model with tool support at last check (~2 s TTFT). Free
models are heavily rate-limited per IP — if you see 429s the demo will
surface the error in the transcript and the next utterance retries; you
can also swap models on the fly by restarting with a different env. Free
models that worked at last check: `z-ai/glm-4.5-air:free`,
`nvidia/nemotron-nano-9b-v2:free`. Models that returned 402/429:
`deepseek/deepseek-v4-flash:free` (paid provider under the `:free` skin),
`openai/gpt-oss-20b:free`, `meta-llama/llama-3.3-70b-instruct:free`,
`google/gemma-4-26b-a4b-it:free`.

If the chosen model advertises tool use, the `show()` aside is wired as
an OpenAI-format function tool so the visual side-channel keeps working.
Otherwise the model just produces prose.

**Claude CLI** (`LLM_BACKEND=claude`). The original wrapper — persistent
`claude --stream-json` subprocess with MCP-config-registered `show` tool.
Use this when you want claude-quality responses, native tool use, and
local subagent capability.

## RVC voice conversion (optional)

The demo can pipe TTS output through an [RVC v2](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
model to re-voice the speaker. Piper produces a clean monotone source;
RVC swaps the timbre to whatever target model you load. Currently wired
to the bundled `Chelly_Egoist` model (downloaded to `voices/rvc/` —
gitignored).

The RVC stack (CUDA torch + fairseq + torchcrepe + faiss) is heavy and
needs Python 3.10, so it lives in a separate `awm-rvc` mamba env and
runs as a small HTTP sidecar that the main demo POSTs PCM to.

### One-time setup

```bash
# Create the sidecar env (Python 3.10 + CUDA torch + rvc-python)
mamba create -n awm-rvc -c conda-forge -y python=3.10 pip
mamba run -n awm-rvc pip install 'pip<24.1'                         # legacy omegaconf metadata
mamba run -n awm-rvc pip install --extra-index-url \
    https://download.pytorch.org/whl/cu121 torch==2.2.2 torchaudio==2.2.2
mamba run -n awm-rvc pip install rvc-python==0.1.5 'setuptools<81' \
    fastapi 'uvicorn[standard]'

# Drop the RVC model + index in demo/voices/rvc/. Either copy your own
# .pth + .index pair, or fetch the bundled one:
mkdir -p demo/voices/rvc && cd demo/voices/rvc
curl -L -o m.zip "https://huggingface.co/Zerobeastskaiai/RVCmodel/resolve/main/Chelly_Egoist.zip?download=true"
unzip -o m.zip Chelly_Egoist.pth 'added_IVF515_Flat_nprobe_1_Chelly_Egoist_v2.index'
mv added_IVF515_Flat_nprobe_1_Chelly_Egoist_v2.index Chelly_Egoist.index
rm m.zip
```

The base models (`hubert_base.pt`, `rmvpe.pt`, `rmvpe.onnx` — ~700 MB)
auto-download into `demo/voices/rvc/base_model/` on first launch.

### Run the sidecar

```bash
cd /home/tony/agentic_workspace/projects/awm/voice
mamba run -n awm-rvc python demo/rvc_service.py \
    --model demo/voices/rvc/Chelly_Egoist.pth \
    --index demo/voices/rvc/Chelly_Egoist.index \
    --models-dir demo/voices/rvc \
    --port 7831
```

Once it logs `RVC ready in 0.6s (f0=rmvpe, ...)`, the main demo
(launched as before) will detect the sidecar at startup and unlock
the **RVC voice conversion** checkbox in the UI. Tick the box to toggle
voice conversion on/off live.

### Tuning knobs

| Knob | Where | Default | What it does |
|---|---|---|---|
| f0 method | `RVC_F0_METHOD` env (on the sidecar) | `rmvpe` | `rmvpe` (GPU, fast+accurate), `pm` (fastest, lower accuracy), `harvest` (CPU pyworld, very slow), `crepe`. |
| Index rate | `RVC_INDEX_RATE` env | `0.5` | FAISS retrieval ratio (0=no index, 1=full index). Higher = more target-speaker accent, slower. |
| Pitch shift | `pitch` query param on `/infer` | `0` | Semitones. Currently always 0 from the main demo. |
| Sidecar URL | `RVC_URL` env (on the main demo) | `http://127.0.0.1:7831` | If you run the sidecar on another host/port. |

### Latency expectations (RTX 3060 Ti)

Per sentence: Piper ~100 ms + RVC ~300–500 ms = ~400–600 ms first-audio,
output runs ~5–8× realtime. The pipeline is sentence-buffered, so the
listener hears the first sentence while later sentences are still being
converted.

### Use your own RVC voice

Drop any RVC v2 `.pth` (+ optional `.index`) into `demo/voices/rvc/`
and re-launch the sidecar with `--model` / `--index` pointing at it.
The sidecar serves one model per process; restart to swap.

## Comparison page + live synthesizer (port 12102 + 12103)

A separate static page (`demo/voices/rvc/samples/index.html`) holds an
interactive A/B board: 4 Kokoro-rendered source clips × 27 RVC target
voices = 108 pre-rendered conversions, plus a **live synthesizer** at the
top that streams arbitrary text through Kokoro → RVC with chunked
playback in the browser.

Two sidecars cooperate:

| Process | Port | Purpose |
|---|---|---|
| `serve_samples.py` | 12102 | Tiny HTTPS static server for the samples directory (page + WAVs + JS). |
| `tts_rvc_service.py` | 12103 | FastAPI sidecar in the `awm-rvc` env that holds **Kokoro + RVC in one process** and streams pipelined output. |

### One-time setup (in addition to the RVC env above)

```bash
mamba run -n awm-rvc pip install 'kokoro-onnx>=0.5.0' --no-deps
mamba run -n awm-rvc pip install colorlog espeakng-loader librosa numba phonemizer-fork
# Numpy must stay <=1.23.5 for rvc-python; if pip backtracks, force it:
mamba run -n awm-rvc pip install 'numpy<=1.23.5'

# Kokoro model + voices file (one-time download, ~350 MB total)
mkdir -p demo/voices/kokoro
curl -L -o demo/voices/kokoro/kokoro-v1.0.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o demo/voices/kokoro/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

### Launch

```bash
# 1. Combined TTS+RVC sidecar (Kokoro + lazy-loaded RVC LRU, port 12103)
mamba run -n awm-rvc python demo/tts_rvc_service.py
# defaults: --device cuda:0 --lru-cap 3 --host 0.0.0.0 --port 12103
#           --manifest demo/voices/rvc/manifest.json
# Uses dev.crt/dev.key for HTTPS so it can talk to the HTTPS samples page.

# 2. Static page server (port 12102) — same dev cert
mamba run -n awm python /tmp/serve_samples.py
```

Open `https://<host-ip>:12102/`. First load you'll need to accept the
self-signed cert for both ports (12102 and 12103) since browsers treat
host:port as a distinct origin — the synthesizer's status line shows a
direct link to `https://<host>:12103/health` if it can't reach the
sidecar.

### Live synthesizer features

- Free-form text input (default = a 2-sentence sample)
- TTS voice dropdown: all 28 English Kokoro voices, grouped by accent +
  gender (`af_*`, `am_*`, `bf_*`, `bm_*`)
- RVC voice dropdown: "Raw Kokoro (no RVC)" + every voice from
  `manifest.json` (27 by default)
- Pitch slider, ±12 semitones, applied at the RVC stage
- **Stream** button (or Ctrl/Cmd+Enter in the textarea) — fetches
  `/stream`, plays length-prefixed PCM frames via WebAudio
- Status line: TTFA, total wall time, chunk count, sample rate

### Pre-rendered samples board

The page also has a 4-source × 27-voice grid of pre-rendered WAVs (see
`batch_sample.py`). Switch between sources via the sticky radio at the
top; each voice card swaps its `<audio>` element accordingly. To
regenerate the grid (e.g. after adding voices to the manifest):

```bash
mamba run -n awm-rvc python demo/voices/rvc/batch_sample.py
# rebuilds demo/voices/rvc/samples/_index.json with new outputs
# then re-run build_index.py to refresh index.html
```

### Streaming architecture

```
browser ──HTTPS POST /stream──▶ tts_rvc_service.py
                                  │
                                  ├─ asyncio task: kokoro.create_stream(sentence_N)
                                  │      ↓  (asyncio.Queue, maxsize=4)
                                  ├─ asyncio task: thread-executor → RVCWrapper.infer_pcm
                                  │      ↓  (yielded to StreamingResponse)
                                  └─ length-prefixed int16 PCM frames: [u32 LE n][PCM…]…
```

Sentences are split server-side **before** Kokoro (its `create_stream`
batches up to 510 phonemes per chunk, which is too coarse for short
utterances). Each sentence becomes one pipeline chunk; Kokoro overlaps
the next sentence with RVC processing the previous one, and HTTP flushes
in parallel. Result: TTFA bounded by `first_sentence_kokoro_ms +
first_sentence_rvc_ms` (~1 s warm), not the full-utterance time.

### Sample-rate plumbing (don't break it)

Different RVC models have different native output sample rates — 32k,
40k, or 48k — read from `rvc.vc.tgt_sr` after `load_model`. The sidecar
propagates this via the `X-Sample-Rate` response header so the browser
can build the `AudioBuffer` at the right rate.

Two gotchas that bit us:
- **CORS hides custom response headers from JS unless explicitly
  exposed.** Without `Access-Control-Expose-Headers: X-Sample-Rate`
  in the response, `fetch.headers.get("X-Sample-Rate")` returns `null`
  and the JS falls back to its default 24 kHz — playback is then
  multiple octaves off. Fix lives in the CORS middleware in
  `tts_rvc_service.py`.
- **`rvc_service.RVCWrapper.__init__` wraps `index_path` in `Path(...)`,
  and `Path("")` becomes `"."`** — which rvc-python rejects when the
  model has no index. The TTS+RVC sidecar overrides
  `wrap.index_path = ""` after construction for index-less models.

### Tuning knobs (sidecar)

| Knob | Where | Default | What it does |
|---|---|---|---|
| LRU size | `--lru-cap` | `3` | Number of RVC models kept resident in VRAM at once. |
| f0 method | `RVC_F0_METHOD` env | `rmvpe` | Same as `rvc_service.py`. |
| Index rate | `RVC_INDEX_RATE` env | `0.5` | Same as `rvc_service.py`. |
| Sidecar URL override (browser) | `?sidecar=...` query string | derived from page origin + `:12103` | For ad-hoc tunneling. |

## Visual side channel — `show()` MCP tool

The agent has a single MCP tool, `show(content, kind)`. It's a no-op
server-side; the value is the tool-use event itself, which the demo
routes into a styled transcript box. The persona (`voice-agent.md`)
instructs the agent to use this any time it wants to put something on
screen that the voice channel can't carry well — code, file paths, URLs,
exact identifiers. Kinds: `code`, `link`, `path`, `text`.

To turn this off, delete `mcp-config.json` (or remove the
`--mcp-config` / `--allowed-tools` flags in `claude_session.py`).

## Logs and diagnostics

- `demo/claude.log` — claude subprocess stderr.
- `demo/server.log` — uvicorn output (set by `tee` in the run command);
  includes startup model-load timings, per-utterance RMS/peak, STT
  timings + transcript text.
- `demo/last_utterance.wav` — most recent captured PCM, dumped every PTT
  release. Useful for ground-truthing capture quality.

## Verification checklist

1. **Capture**: mic meter moves while speaking; `rms` in
   `server.log` after PTT release is > ~200 for a real utterance.
2. **Single turn**: ask "what is two plus two?" — transcript renders
   within ~1s of release, first TTS audio within ~2s.
3. **Multi-turn context**: 3-turn conversation. Confirms the
   stream-json subprocess is persistent.
4. **Barge-in**: ask "tell me a long story", interrupt mid-reply. TTS
   should cut, new utterance captured cleanly.
5. **Visual aside**: ask "show me a hello-world in python and explain
   it" — code block appears as a styled box, only the explanation is
   spoken.
6. **Latency readout**: STT < 1s short, first-token typically 0.5–1.5s
   (Claude-bound), first-audio < ~600 ms per sentence (pocket-tts is
   roughly 3× realtime on CPU).

## Files

| File | Role |
|---|---|
| `server.py` | FastAPI + WS, lifespan model preload, sentence-buffered TTS, barge-in, status events, tool routing. |
| `claude_session.py` | Persistent `claude --stream-json` subprocess (selected by `LLM_BACKEND=claude`). |
| `openrouter_session.py` | OpenRouter chat-completions client (default `LLM_BACKEND=openrouter`); streams deltas, in-memory message history, wires `show()` as an OpenAI-format tool when the model supports it. |
| `voice-agent.md` | Persona for the claude subprocess — "speak in prose, use `show()` for visual asides". |
| `show_mcp.py` | Tiny stdio MCP server exposing the no-op `show(content, kind)` tool. |
| `mcp-config.json` | MCP config registering `show` for the claude subprocess. |
| `stt.py` | faster-whisper wrapper (lazy load, configurable model/compute). |
| `tts.py` | pocket-tts wrapper. Voice from `VOICE_NAME`. Used when `TTS_BACKEND=pocket`. |
| `piper_tts.py` | Piper TTS wrapper. Voice from `PIPER_VOICE`. Default backend. |
| `rvc.py` | HTTP client for the RVC sidecar. PCM in → re-voiced PCM out. |
| `rvc_service.py` | Standalone FastAPI sidecar (runs in `awm-rvc` env) that loads the RVC model on GPU and exposes `/infer` + `/health`. Used by the live demo. |
| `tts_rvc_service.py` | Combined Kokoro + RVC sidecar (runs in `awm-rvc` env) holding both stacks in one process. Exposes `/voices`, `/health`, `/stream` (chunked PCM). Used by the comparison page. |
| `voices/rvc/samples/synth.js` | Browser streaming player for `tts_rvc_service` — reads length-prefixed PCM frames via fetch ReadableStream and queues them on a single AudioContext. |
| `voices/rvc/samples/index.html` | Generated comparison page with sticky source-picker, 4×27 pre-rendered grid, and the live synthesizer panel. Built by `/tmp/build_index.py`. |
| `voices/rvc/batch_sample.py` | Pre-renders the 4-source × N-voice comparison grid by iterating manifest + calling RVC. |
| `voices/rvc/manifest.json` | Per-voice metadata (label, repo, pth/index paths). Loaded by `tts_rvc_service.py` and `batch_sample.py`. |
| `dev.key` + `dev.crt` | Self-signed TLS material for HTTPS exposure (gitignored). |
| `text_clean.py` | Strip markdown / code / URLs before sending to TTS. |
| `static/index.html` + `client.js` | PTT UI, mic meter, volume slider, status pill, WebAudio playback queue, markdown-aware aside rendering. |
| `static/mic-worklet.js` | AudioWorklet: downsample mic to 16kHz int16 PCM. |

## What this does NOT do (yet)

- No awm-rooms integration. Next iteration replaces `claude_session.py`
  with a `room_post` + WS-attach against the existing rooms surface, and
  the "representative" agent gains room MCP tools (`room_join`,
  `room_post`, `inbox_send`, etc.).
- No VAD / wake-word — push-to-talk only.
- Single TTS voice; no per-agent voice mapping.
- Playback speed is not pitch-preserved (browser `playbackRate` shifts
  pitch). Phase-vocoder time-stretching would need an extra dep.
- No auth, packaging, or systemd unit.
