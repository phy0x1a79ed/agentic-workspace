# Installing the `stt` service (STT-only)

A Python feature service in the `awm.stt` namespace. It needs the `awm` conda
env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`).

This service is **STT-only**: it exposes faster-whisper transcription
(`transcribe` HTTP-fallback function) and one direct `stream` session — the
per-user PTT / continuous-dictation loop, including the convo cleanup inner
loop. The old mock `chat` conversational partner was dropped in the modular
migration; the real agent (the `agents` service) replaces it. The duplicate STT
loop that used to live in `tts` was pruned too — **`stt` owns STT now**.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-persistence`, `awm-gatewayclient` | component libs (ServiceAdapter, per-service state) |
| `faster-whisper` | the STT engine (CTranslate2 Whisper) |
| `numpy` | PCM ↔ float32 conversion + segment splicing for rolling partials |
| `httpx` | transitively (ServiceAdapter register/control) |

The convo dictation-cleanup loop drives the shared **`awm.agentcore`** harness
layer in **opencode one-shot** mode (a fresh `opencode run` per silence-cut), so
it has no warm-server dependency of its own. `agentcore` is pure imported source
(no `install.sh`); in the dev sandbox it is resolved on `DEV_PYTHONPATH`, and in
prod it is installed alongside the gateway. The cleanup loop also needs the
**`opencode` CLI** on PATH and a configured provider (see below); without it the
loop degrades gracefully to the raw transcript (no auto-submit).

### faster-whisper model weights — first-run download

`faster-whisper` downloads its model weights on **first transcription**, not at
install time. The default model is `small.en` (override with `WHISPER_MODEL`;
compute type with `WHISPER_COMPUTE`, default `int8`). The very first PTT/STT
request after a fresh install pays a one-time download (tens to hundreds of MB,
cached under `~/.cache/huggingface`). Subsequent runs load from cache. Plan for
the first request to be slow, or pre-warm by running one transcription after
install.

### opencode auth (for the convo dictation-cleanup loop)

The cleanup loop uses opencode's free Zen `deepseek-v4-flash-free` model by
default (override with `CONVO_PROVIDER` / `CONVO_MODEL`). `opencode` reads its
credentials from `~/.local/share/opencode/auth.json` automatically — add one
once:

    opencode auth login        # choose "opencode" (Zen); paste the key
    opencode run --model opencode/deepseek-v4-flash-free "say hi"   # verify

Without a configured provider the cleanup calls fail and the convo loop falls
back to showing the raw transcript with no auto-submit — STT itself (PTT mode
and the `transcribe` function) works regardless.

## Surface

| Kind | Name | Notes |
|---|---|---|
| function | `transcribe` | `{"pcm_b64": <base64 int16 LE 16 kHz mono>}` → `{"text": ...}` |
| session | `stream` (direct) | per-user PTT / continuous-dictation WS; binary PCM + JSON control frames in, `status`/`partial`/`stt_result`/`composer`/`submit` frames back |

## Controlling the service

    awm services list                 # discovered services + enabled/running state
    awm services start stt            # start (idempotent)
    awm services restart stt
    awm services enable stt           # clear the disable flag

Or iterate against a running dev hub without evicting the base:

    awm dev shadow awm/services/stt   # overlay this worktree's copy; Ctrl-C pops
