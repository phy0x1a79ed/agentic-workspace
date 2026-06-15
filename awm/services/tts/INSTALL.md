# Installing the `tts` service (TTS-only)

A Python feature service in the `awm.tts` namespace. It needs the `awm` conda
env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`).

This service is **TTS-only**: it exposes exactly **two** user-facing TTS engines
— `piper` (fast, fully-local, the default) and `sbv2` (Style-Bert-VITS2 sidecar)
— plus loadable-but-unexposed `kokoro_rvc` / `f5tts` / `gptsovits`. It also owns
named-preset + keyed-state storage and one direct `call` playback session that
streams synthesized PCM back to the browser. The old duplicate STT / orchestrator
conversation loop was pruned in the modular migration — `stt` owns STT now.

**RVC is not a peer engine.** Retrieval-based Voice Conversion is an audio
*changer*, not a synthesizer, so it folds into `piper` as an optional post-stage:
piper's config has an `rvc_voice` dropdown that defaults to `off` (raw local
piper, lowest latency); selecting any other voice routes piper's PCM through the
RVC stage on the kokoro-rvc sidecar. `kokoro_rvc` is therefore no longer surfaced
as its own engine.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

### Import-time dependencies (light — installed by `install.sh`)

The service *process* itself is light. To register, hold the control WS, and
answer `listEngines` / `listPresets` / `getAllState` etc. it needs only:

| Dep | Why |
|---|---|
| `awm-config`, `awm-persistence`, `awm-gatewayclient` | component libs (ServiceAdapter, per-service DB) |
| `httpx` | engine modules are thin HTTP clients to their sidecars |
| `numpy` | `f5tts` WAV→PCM conversion (`_wav_to_pcm`, reused by `sbv2`/`gptsovits`) |
| `pydantic>=2.0` | engine `CONFIG_SCHEMA` validation |

All three of `httpx` / `numpy` / `pydantic` are already in the `awm` env (the
gateway pulls them). **No heavy install is required to bring the service up.**

### Synthesis dependencies

**piper runs fully in-process and is the working default.** It needs the
installed `piper` package (`piper-tts`, imports as module `piper` with
`PiperVoice` / `SynthesisConfig` — **not** the fictional `piper_tts`) plus at
least one `.onnx` voice model. Models live under
`<AWM_DATA_DIR>/tts-models/piper/*.onnx` (reachable from any scope as
`.awm/data/tts-models/piper/`), mirroring the `sbv2` layout. Provision one with:

    python -m piper.download_voices --download-dir \
      "$AWM_DATA_DIR/tts-models/piper" en_US-lessac-medium

The `base_voice` dropdown enum is filled at `listEngines` time by scanning that
dir; `PIPER_VOICE` (a backend env override) overrides the path. `piper-tts`,
`torch`, `faster-whisper`, `onnxruntime`, `soundfile`, `numpy`, `httpx` are all
already present in the `awm` env.

The **RVC post-stage** and the other engines run through **out-of-process
sidecars** — `torch`, model weights, GPU — and are **not** started by
`install.sh`. The service registers and answers metadata calls without any of
them; piper-with-`rvc_voice=off` synthesizes with no sidecar at all. Selecting an
RVC voice, or the `sbv2` engine, fails gracefully per-call if its sidecar is
absent (the session ends; other engines/paths are unaffected).

| Path / Engine | Backend | What the operator provisions | Selecting env vars |
|---|---|---|---|
| `piper` (default) | in-process `piper` package + `.onnx` voice | download a voice into the piper models dir (above) | `PIPER_VOICE` (path override) |
| piper `rvc_voice` ≠ `off` | HTTP sidecar (kokoro-rvc RVC voice-conversion server) | run the sidecar; the `rvc_voice` enum is its `/voices` list with `off` prepended | `TTS_RVC_URL` (default `https://127.0.0.1:12123`), `TTS_RVC_VERIFY_SSL` |
| `sbv2` | HTTP sidecar (Style-Bert-VITS2 server, torch) | run the sbv2 server; first run loads the SBV2 model | `SBV2_URL`, `SBV2_MODEL_DIR` |
| `f5tts` | HTTP sidecar (F5-TTS server) — loadable, not UI-exposed | run the f5tts server; needs a reference WAV + transcript | `F5TTS_URL` |
| `gptsovits` | HTTP sidecar (GPT-SoVITS server) — loadable, not UI-exposed | run the GPT-SoVITS server | `GPTSOVITS_URL` |
| `kokoro_rvc` | kokoro→RVC sidecar — **retired as a peer engine** (RVC now folds into piper) | — | `TTS_RVC_URL` |

UI-exposed engines are exactly `piper` / `sbv2` (`app.EXPOSED_TTS_ENGINES`);
`kokoro_rvc` / `f5tts` / `gptsovits` stay loadable from code but aren't surfaced.

**Caveat:** the piper→RVC-*on* path assumes the kokoro-rvc sidecar accepts a
PCM-in RVC-only route; if the sidecar only does kokoro→RVC end-to-end today,
enabling a real RVC voice needs a sidecar change. The `off` (raw piper) default
works regardless and is the verified path.

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token. The sidecar env vars
above are NOT injected by the gateway; set them in the gateway's environment
(or `.awm/env`) if the defaults don't match your sidecar deployment.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/tts`; it execs this same `run.sh` as an overlay.
