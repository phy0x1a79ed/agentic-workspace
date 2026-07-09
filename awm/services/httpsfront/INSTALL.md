# Installing the `httpsfront` service (whole-gateway HTTPS front)

A Python feature service in the `awm.httpsfront` namespace. It makes **all of
awm reachable over HTTPS**: it terminates TLS on an off-host `0.0.0.0:<port>`
listener (default **8443**) and transparently reverse-proxies every request —
HTTP *and* WebSocket — to the loopback awm gateway (`AWM_HUB_URL`, normally
`http://127.0.0.1:7819`).

Why it exists: the gateway binds loopback-only plain HTTP by design, but browser
APIs like `getUserMedia` (the notes-page dictation) require a *secure context*,
which off-localhost means HTTPS. Rather than re-architect the loopback gateway,
this rides its own off-host HTTPS listener — exactly as `mic` and `fileviewer`
serve their surfaces off-hub — and the gateway registration provides supervision
plus an `httpsfront_status` verb only.

Because every awm page makes *same-origin* relative calls (`/svc/*`, same-origin
WebSockets), fronting the gateway wholesale is what makes those calls work under
HTTPS with no per-path allowlist to maintain.

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
| `starlette`, `uvicorn` | the TLS-terminating ASGI listener |
| `httpx` | async HTTP reverse-proxy client |
| `websockets` | WebSocket reverse-proxy client (browser ⇄ gateway) |

All four are already dependencies of the gateway, so in the `awm` env they
resolve as already-satisfied.

## System dependencies (NOT pip-installable)

| Tool | Package | Why |
|---|---|---|
| `openssl` | `openssl` | mint the local root CA + leaf server cert for HTTPS |
| `hostname` | `hostname` / coreutils | enumerate the host IPs baked into the cert SAN |

    sudo apt-get install -y openssl hostname

Both live in `/usr/bin`, on the minimal systemd PATH the supervisor uses.

## TLS — reuses the remote-audio CA

Certs are minted by `awm.httpsfront.certs` into a gitignored `.certs/` next to
the service. The **root CA is shared with remote-audio / the `mic` service** at
`~/.config/remote-audio/ca` (override with `REMOTE_AUDIO_CA_DIR`), so a device
that already trusts that root needs no new setup. The root is minted only the
first time it's missing; only the short-lived leaf rotates (re-minted whenever
the host's IP/SAN set changes).

Install the root on a new device once by visiting `https://<host-ip>:8443/ca.crt`
and trusting it (Android/iOS offer to install the downloaded
`application/x-x509-ca-cert`).

### SANs the host can't see for itself

The leaf's SAN set is auto-enumerated from the host's non-loopback IPv4
addresses plus `127.0.0.1`/`localhost`. Addresses the host can't enumerate — e.g.
the Windows ZeroTier IP a phone reaches a WSL host through — are declared
explicitly and merged in:

- `AWM_TLS_EXTRA_SANS` env var (comma/space separated), or
- a gitignored `.sans` file next to the service (one token per line, `#`
  comments). Tokens may be bare (`10.147.0.5`) or prefixed (`IP:10.147.0.5` /
  `DNS:notes.zt`).

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AWM_HTTPS_PORT` | `8443` | the off-host TLS listener port |
| `AWM_HUB_URL` | injected by gateway | the loopback gateway it fronts |
| `AWM_TLS_EXTRA_SANS` | — | extra cert SANs (see above) |
| `REMOTE_AUDIO_CA_DIR` | `~/.config/remote-audio/ca` | shared root-CA location |

`AWM_HTTPS_PORT` is a clean one-line port knob: set it in the workspace's
gitignored `$AWM_WORKSPACE/.awm/env` (merged into the gateway env at startup,
before any service spawns — see README § *Per-workspace env file*), so a port
change is that single edit plus a `systemctl restart awm.service`, not a
re-architecture.

**On the production ZeroTier host** this is set to `AWM_HTTPS_PORT=12100` — the
port Windows forwards into WSL over ZeroTier (a phone reaching `10.74.81.110`
lands on 12100). So the HTTPS front is the *sole* exposure of awm on that host;
there is no plain-HTTP relay. A future port change is one line in `.awm/env`.

## Exposure note

Fronting the gateway wholesale means the **entire unauthenticated awm surface**
is reachable from the LAN/ZeroTier on `:8443` — the same exposure posture as the
`mic` bridge on `:12200`. The gateway remains loopback-only for plain HTTP; this
is the single TLS door in.
