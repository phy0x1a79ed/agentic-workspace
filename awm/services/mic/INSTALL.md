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
supervision + a `mic_status` surface only.

**mic does not own the audio plumbing.** PulseAudio and the `virtmic` null-sink
belong to the [`virtmic`](../virtmic/INSTALL.md) service, which keeps them alive
across daemon restarts. mic is a consumer: it pipes PCM in with `pacat` and
calls `virtmic_ensure` immediately before starting a stream (the gateway
guarantees no start order between services, so the dependency is explicit rather
than temporal). `mic_ensure_sink` still exists as a deprecated alias that
forwards to `virtmic_ensure`.

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

The bridge transport is **pure stdlib** — no `whisper`/`numpy` of its own; the
only non-stdlib call is the lazy `gatewayclient.call_sync("virtmic", "ensure")`
made just before a stream starts.

## System dependencies (NOT pip-installable)

| Tool | Package | Why |
|---|---|---|
| `pacat` | `pulseaudio-utils` | pipe browser PCM into the `virtmic` sink (the sink itself is provisioned by the `virtmic` service) |
| `openssl` | `openssl` | mint the local root CA + leaf server cert for HTTPS |
| `hostname` | `hostname` / coreutils | enumerate the host IPs baked into the cert SAN |

    sudo apt-get install -y pulseaudio pulseaudio-utils openssl

They live in `/usr/bin`, on the minimal systemd PATH the supervisor uses.

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
without re-touching the device. The mic page also surfaces a one-tap **Install
certificate** panel whenever the audio socket can't open (the usual symptom of
an untrusted CA — browsers silently refuse to click-through a cert error for
`wss://`, even after you accept it for the page).

### SAN must include the address the phone actually dials

The leaf's SAN is auto-enumerated from IPs visible **inside WSL** (`hostname -I`)
— which does *not* include the **Windows host's ZeroTier IP** that the phone
connects to (the bridge is port-forwarded into WSL from there). If that address
isn't in the SAN, the phone's WebSocket fails a hostname check even with the CA
trusted. WSL can't discover that IP itself, so declare it explicitly — either a
gitignored `.sans` file beside the service (one token per line, `#` comments) or
the `MIC_EXTRA_SANS` env var (comma/space separated). Bare IPs/hostnames are
fine; `IP:` / `DNS:` prefixes are optional:

    echo 10.147.20.5 > .sans        # the Windows ZeroTier IP the phone browses to

The leaf re-mints on the next service start (watch the `certs ready (SAN=…)`
log line). ZeroTier member IPs are stable, so this is a one-time step.

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
| `MIC_EXTRA_SANS` | (none) | extra cert SANs WSL can't self-enumerate — e.g. the Windows ZeroTier IP the phone dials; comma/space list, `IP:`/`DNS:` optional. Also read from a `.sans` file beside the service. |
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
