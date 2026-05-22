# Voice demo — mic → claude CLI → speakers

A standalone spike that proves the audio half of the eventual voice-bridged
representative agent. Push-to-talk in the browser, faster-whisper STT,
persistent `claude` subprocess, kyutai pocket-tts. No awm-rooms
integration yet.

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
| TTS voice | `VOICE_NAME` env | `jean` | Any name from the pocket-tts catalog (`alba`, `eve`, `michael`, `paul`, etc.), an absolute path to a `.wav`/`.safetensors`, or an `hf://kyutai/tts-voices/...` URI for voice cloning. |
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
| `claude_session.py` | Persistent `claude --stream-json` subprocess; same argv as `awm/services/sessions_live.py:160-172`, plus `--append-system-prompt voice-agent.md`, `--mcp-config mcp-config.json`, `--allowed-tools mcp__show__show`. |
| `voice-agent.md` | Persona for the claude subprocess — "speak in prose, use `show()` for visual asides". |
| `show_mcp.py` | Tiny stdio MCP server exposing the no-op `show(content, kind)` tool. |
| `mcp-config.json` | MCP config registering `show` for the claude subprocess. |
| `stt.py` | faster-whisper wrapper (lazy load, configurable model/compute). |
| `tts.py` | pocket-tts wrapper. Voice from `VOICE_NAME`. |
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
