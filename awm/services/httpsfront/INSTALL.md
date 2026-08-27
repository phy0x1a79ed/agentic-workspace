# Installing the `httpsfront` service (whole-gateway HTTPS front)

A Python feature service in the `awm.httpsfront` namespace. It makes **all of
awm reachable over HTTPS**: it terminates TLS on an off-host `0.0.0.0:<port>`
listener (default **8443**) and transparently reverse-proxies every request —
HTTP *and* WebSocket — to the loopback awm gateway (`AWM_HUB_URL`, normally
`http://127.0.0.1:7819`).

Why it exists: the gateway binds loopback-only plain HTTP by design, but browser
APIs like `getUserMedia` (the notes-page dictation, the mic page) require a
*secure context*, which off-localhost means HTTPS. Rather than re-architect the
loopback gateway, this rides its own off-host HTTPS listener, and the gateway
registration provides supervision plus an `httpsfront_status` verb only.

`fileviewer` and `mic` each used to serve their own surface off-hub the same
way; both have since been folded in. httpsfront is now the **only** off-host
listener in awm, and everything else rides the gateway behind it — which is also
why it is the only holder of the CA.

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
the service. The **root CA is shared with remote-audio** at
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
| `AWM_EDGE_PROFILE` | — | `public`: only the paths in `policy.py` exist, no CA/link/landing routes, `SameSite=Strict` |
| `AWM_EDGE_TLS` | `1` | `0`: plain HTTP on `127.0.0.1:$AWM_HTTPS_PORT` behind a TLS-terminating nginx |

On every profile the edge overwrites `X-Awm-As` with the session's verified subject (`user:<name>`, or `peer` for a bearer), so a downstream service may trust that header. `/__auth/login` takes `{username, password}`; a blank username is the shared password. The readable `awm_as` cookie is the username for the pages' user chip and carries no authority.

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

Fronting the gateway wholesale means the **entire awm surface** — every page and
every service, including the mic's audio session — is reachable from the
LAN/ZeroTier on `:8443` behind this one password. The gateway remains
loopback-only for plain HTTP; this is the single TLS door in, and there is no
longer any other.

## Reuse: fronting something that is not the gateway

`proxy.serve(upstream=…, landing=False, extra_routes=…, rewrite_origin=…)` is the
whole reuse surface. `claude-science` and `dsh` both front their own loopback
binaries with it rather than reimplementing TLS, the shared CA and the
`awm_session` gate.

`rewrite_origin` arbitrates two upstreams that want opposite things, which is why
it exists as a flag rather than a decision. Note first that `host` is always
dropped, so httpx derives it from the upstream URL and a wrapped app sees a
loopback `Host` — that is deliberate and load-bearing, because it is what opens
an app's loopback-pinned privileged plane to a remote browser. `Origin` is then
forwarded verbatim by default, which is what `claude-science` needs: it
allowlists the exact browser origins its WebSocket upgrades may come from, and
rewriting the header would reject every handshake. `dsh` needs the reverse — its
`/api` fence compares `Origin` against `Host` and demands they match, so the
real browser origin can never satisfy it. `rewrite_origin=True` replaces a
*present* `Origin` with the upstream's own scheme and authority, on the HTTP and
WebSocket paths alike. It never mints one where the browser sent none: that would
turn a same-origin navigation into a cross-origin request at the upstream.
Default off, so the gateway front stays byte-identical.
