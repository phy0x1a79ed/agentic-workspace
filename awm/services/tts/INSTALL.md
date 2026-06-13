# Installing the `tts` service (TTS-only)

A Python feature service in the `awm.tts` namespace. It needs the `awm` conda
env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`).

This service is **TTS-only**: it exposes the production TTS engines
(`kokoro_rvc` / `piper` / `sbv2`, plus loadable `f5tts` / `gptsovits`),
named-preset + keyed-state storage, and one direct `call` playback session that
streams synthesized PCM back to the browser. The old duplicate STT /
orchestrator conversation loop was pruned in the modular migration — `ptt` owns
STT now.

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

### Synthesis dependencies (heavy — operator-provisioned, NOT installed here)

Actual speech synthesis runs through **out-of-process sidecars** (and one
optional in-process backend). These are the heaviest, least-certain part of the
voice stack — `torch`, model weights, GPU. They are **not** installed by
`install.sh` and are **unverified in the `awm` env**. The service registers and
answers metadata calls without any of them; only opening a `call` session
against an engine whose sidecar/binary is absent fails — and it fails
gracefully, per-call (the session ends; other engines are unaffected).

| Engine | Backend | What the operator provisions | Selecting env vars |
|---|---|---|---|
| `kokoro_rvc` | HTTP sidecar (kokoro TTS + RVC voice-conversion server) | run the sidecar; first run downloads the kokoro + RVC model weights | `TTS_RVC_URL` (default `https://127.0.0.1:12123`), `TTS_RVC_VERIFY_SSL` |
| `piper` | in-process `piper_tts` package + a `.onnx` voice model | `pip install piper_tts` into the env; download a piper voice; first synth lazy-loads the model | `PIPER_VOICE` (voice path override) |
| `sbv2` | HTTP sidecar (Style-Bert-VITS2 server, torch) | run the sbv2 server; first run loads the SBV2 model | `SBV2_URL`, `SBV2_MODEL_DIR` |
| `f5tts` | HTTP sidecar (F5-TTS server, torch) — loadable, not UI-exposed | run the f5tts server; needs a reference WAV + transcript | `F5TTS_URL` |
| `gptsovits` | HTTP sidecar (GPT-SoVITS server) — loadable, not UI-exposed | run the GPT-SoVITS server | `GPTSOVITS_URL` |

UI-exposed engines are `kokoro_rvc` / `piper` / `sbv2` (`app.EXPOSED_TTS_ENGINES`);
`f5tts` / `gptsovits` stay loadable from code but aren't surfaced in the form.

First-run weight downloads (kokoro, RVC, SBV2, piper voices) happen inside the
respective sidecars/backends, not in this service — budget for the download +
model-load latency on the first `call` session against each engine.

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
