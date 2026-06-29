# Installing the `mic` service (remote microphone bridge)

A Python feature service in the `awm.mic` namespace. It gives WSL a microphone
fed from a phone's browser over ZeroTier: the browser captures its mic and
streams s16le PCM over an HTTPS WebSocket into a PulseAudio `virtmic` null-sink
whose `.monitor` is WSL's default capture source — so `/voice`, `arecord`, and
the awm `stt` stack record the phone.

Unlike most services, the audio + page do **not** ride the awm hub: the gateway
binds loopback-only plain HTTP, and `getUserMedia` needs a secure context
(HTTPS off-localhost). So the bridge runs its own off-host HTTPS listener on a
fixed port (default **12200**), and the gateway registration provides
supervision + a `mic_status` / `mic_ensure_sink` surface only.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries (`config`,
`gatewayclient`) and this service into the `awm` env (override with
`AWM_ENV=<name>`) and writes a gitignored `.runtime-env` sidecar baking
`AWM_PYTHON` = the env's absolute interpreter, so the gateway can respawn the
service under systemd's minimal PATH (where `mamba` is not present).

## Python dependencies

| Dep | Why |
|---|---|
| `awm-config`, `awm-gatewayclient` | component libs (ServiceAdapter register/control loop) |

The bridge itself is **pure stdlib** — no `whisper`/`numpy`/`httpx` of its own.

## System dependencies (NOT pip-installable)

| Tool | Package | Why |
|---|---|---|
| `pactl`, `pacat` | `pulseaudio-utils` | provision the `virtmic` null-sink; pipe browser PCM into it |
| `pulseaudio` | `pulseaudio` | the userspace audio daemon (no WSLg / Windows audio needed) |
| `openssl` | `openssl` | mint the local root CA + leaf server cert for HTTPS |
| `hostname` | `hostname` / coreutils | enumerate the host IPs baked into the cert SAN |

    sudo apt-get install -y pulseaudio pulseaudio-utils openssl

All four live in `/usr/bin`, on the minimal systemd PATH the supervisor uses.

## TLS — reuses the remote-audio CA

Certs are minted by `awm.mic.certs` into a gitignored `.certs/` next to the
service. The **root CA is shared with remote-audio** at
`~/.config/remote-audio/ca` (override with `REMOTE_AUDIO_CA_DIR`), so a phone
that already trusts that root needs no new setup. If the phone has never trusted
it, install the root once — browse to `https://<host>:12200/ca.crt` and install
the downloaded certificate (Android: Settings → Security → Encryption &
credentials → Install a certificate → CA certificate; iOS: install the profile,
then General → About → Certificate Trust Settings → enable). The leaf rotates
automatically (≤397-day mobile cap; re-minted when the host's IP set changes)
without re-touching the device.

## Off-host reachability (ZeroTier)

The listener binds `0.0.0.0:12200` inside WSL. Windows forwards the port into
the VM via `netsh interface portproxy` — the permanent entry is already in
`/mnt/a/linux/ssh_settings.ps1` (port **12200**, kept out of the reserved
`12100-12150` ops block). Re-run that script elevated on Windows after a WSL IP
change. If the ZeroTier interface isn't already trusted by Windows Defender, add
an inbound allow for TCP 12200.

## Always-on gateway

The mic is permanent WSL infrastructure, so the host gateway must not idle out.
Set `AWM_IDLE_SHUTDOWN=0` in the gateway's `$AWM_WORKSPACE/.awm/env` and restart
`awm.service`.

## Env overrides

| Var | Default | Effect |
|---|---|---|
| `MIC_PORT` | `12200` | HTTPS listener port (must match the portproxy entry) |
| `MIC_SINK` | `virtmic` | PulseAudio null-sink name |
| `REMOTE_AUDIO_CA_DIR` | `~/.config/remote-audio/ca` | shared root CA location |

## Verify

    awm services list            # mic → running
    awm mic status               # sink, default_source, listener_port=12200, tls=true
    curl -k https://127.0.0.1:12200/        # the mic page
    curl -k https://127.0.0.1:12200/ca.crt  # the root CA
    pactl get-default-source                # virtmic.monitor

Then open `https://<host-zerotier-ip>:12200/` on the phone, tap **Start**, and
on WSL run `parec -d virtmic.monitor | …` (or Claude Code `/voice`) to confirm
it hears the phone.
