# AWM Federation v1

Cross-node networking so a second always-on host (**mira**) is a full peer AWM
node. Two nodes use each other's services **on demand** — defaulting to local,
**never syncing databases, never relaying peer traffic through a gateway** —
behind a **single authentication system** while the loopback interior stays open.

This is deliberately **not** the retired federation (cr-sqlite replication,
leader election, a peers/replication registry). It is point-to-point service
composition: each node is authoritative for its own state; peers are visible on
demand; there is no single identity brain (per-node state, cross-reads on demand).

## The shape

```
   agent / CLI / awm-mcp                      agent / CLI / awm-mcp
          │ loopback (open, no auth)                 │
   ┌──────▼───────┐   peer_resolve(name)      ┌──────▼───────┐
   │  gateway A   │◄───(directory only)       │  gateway B   │
   │  :7819 loop  │                           │  :7819 loop  │
   └──────┬───────┘                           └──────┬───────┘
          │ fronts                                   │ fronts
   ┌──────▼───────┐   TLS + Bearer (peer cred) ┌─────▼────────┐
   │ httpsfront A │──────────────────────────► │ httpsfront B │  (edge auth)
   │  :12100 edge │   direct, CA-verified      │  :12100 edge │
   └──────────────┘                            └──────────────┘
```

- **Service calls default to the local gateway.** Peer services are visible on
  demand, namespaced `<svc>@<peer>`.
- **The gateway is a resolver, never a relay.** A cross-peer call asks its *own*
  gateway to resolve the peer's edge address (`peer_resolve`), then talks to the
  **peer's edge directly** — no peer bytes traverse a gateway.
- **Auth is edge-only.** The loopback gateway (`:7819`) stays open and
  auth-unaware; local CLI / `awm-mcp` never authenticate. The `httpsfront` edge
  (`:12100`) is the single authenticated door.

## Components (v1)

| Piece | Where | Role |
|---|---|---|
| `auth` service | `awm/services/auth/` | credential **authority**: mints paired (login-password, peer-credential) generations, signs sliding session tokens, pushes the day's password to Discord, mirrors the peer credential to `$AWM_PEER_CRED` |
| `httpsfront` edge | `awm/services/httpsfront/` | **enforces** auth at the network edge: session cookie (sliding) OR peer bearer, CA-verified TLS, login page / 401; serves the landing page at `/` |
| peer directory | gateway (`awm/gateway/awm/gateway/peers.py`) | the address book: `peer_join`/`peer_list`/`peer_resolve`/`peer_forget` |
| cross-peer calls | `gatewayclient.call_peer` + the `awm-mcp` proxy | reach a peer's edge directly (client-side federation) |

## Authentication

**Paired, co-rotated, distinct values.** Every ~12 h the `auth` service mints a
fresh pair — a `login-password` (human, typed once a day) and a `peer-credential`
(machine, distinct value, never pushed to Discord) — each valid ~24 h. With a 12 h
cadence and 24 h validity, **two generations are valid at once**, so a client
never has to re-authenticate across a rotation. On startup it mints if none is
valid or the newest is older than the cadence (no dependency on `events`).

Each mint: a **loud** log line, a best-effort push of the *login password* to
Discord `#notifications`, and a rewrite of the `$AWM_PEER_CRED` file.

- **Human login:** `POST /__auth/login` at the edge → `auth.verify` → a
  signed session cookie. The cookie is **slid** (re-issued) on each authenticated
  request within its window, capped by a hard maximum session age. Get the day's
  password on the daemon host with `awm auth password` (it is also in Discord).
- **Peer auth:** a peer sends `Authorization: Bearer <peer-credential>`; the edge
  checks it against the currently-valid peer credentials.

The edge validates cookies **offline** using a signing secret it fetches once
from `auth.edge_material` (refreshed every ~30 s to pick up rotated peer creds),
so there is no auth RPC on the hot path. Fail-closed: if `auth` is unreachable
and no material is cached, the edge authenticates nothing.

## The SSH peer-auth channel

Peers do **not** exchange tokens (a join handshake would be an attack surface —
an imposter could solicit everyone's tokens). Instead **SSH is the authentication
channel**: host-key + `authorized_keys` provide mutual auth, and a peer fetches
the *current* credential on demand:

```
ssh <peer> 'cat "$AWM_PEER_CRED"'
```

`$AWM_PEER_CRED` on each node points at the file the `auth` service keeps current
(`$AWM_DIR/services/auth/peer_cred.current`). The fetch is implemented in
`gatewayclient.fetch_peer_cred(ssh_alias)` (`ssh -o BatchMode=yes <alias> 'cat
"$AWM_PEER_CRED"'`), cached, and re-fetched on a 401 (credential rotated). It is
only ever called with a **peer** alias.

### Setup (per node) — the one trap that silently breaks this

`ssh host 'cmd'` runs a **non-interactive, non-login** shell, which does **not**
source the interactive part of `~/.bashrc`. So the export must be placed **above**
the interactive guard, or in `~/.ssh/environment`:

```bash
# ~/.bashrc — ABOVE the `case $- in *i*) ... esac` interactive guard:
export AWM_PEER_CRED="$HOME/agentic_workspace/.awm/services/auth/peer_cred.current"
```

(Path is host-specific — it is `$AWM_DIR/services/auth/peer_cred.current` for that
node's workspace. Confirm with `awm auth peer-credential`, which prints the path.)

Alternative: `~/.ssh/environment` with `PermitUserEnvironment yes` in `sshd_config`.

### ControlMaster

Mesh peers have no Duo, so a plain `ssh <alias>` is fast; if the host's ssh config
has `ControlMaster auto` + a `ControlPath`, the fetch transparently reuses the
existing master socket. No federation code manages the master — it is standard ssh
multiplexing, configured at standup.

## Peer directory — usage

```
awm peer join mira mira:12100 --ssh-alias mira   # record a peer (run on BOTH nodes)
awm peer list
awm peer resolve mira
awm peer forget mira
```

`peer_join` records the peer locally; run it on **both** nodes to make the link
mutual (nothing is synced). `edge_url` may be bare `host:port` (coerced to
`https://`).

## Cross-peer calls

- **From an agent (MCP):** each registered peer's collapsed catalog is merged
  into the `awm-mcp` surface namespaced `<domain>@<peer>` (e.g. `2fa@mira`); a
  call to it is routed to the peer's edge directly. A down peer contributes no
  tools and never blocks the local surface.
- **From a service (service→service):** `gatewayclient.call_peer(peer, service,
  fn, args)` — e.g. `ssh` → `2fa@mira`. Resolves the peer via the local gateway,
  fetches the bearer over SSH, POSTs `{edge}/svc/{service}/fn/{fn}` over
  CA-verified TLS. `RefCache` keys include the peer so `2fa` and `2fa@mira` never
  collide.

**v1 scope is fn-only** (request/reply). Live sessions, emitters, and bridges
stay node-local.

## TLS

Peer edges present certs signed by the shared **remote-audio root CA** (the same
root `mic`/`httpsfront` already use). Cross-peer TLS is **CA-verified, never
`verify=False`** — a bearer over an unverified connection could be captured. The
CA path is `AWM_PEER_CA` / `REMOTE_AUDIO_CA_DIR` / `~/.config/remote-audio/ca/
ca.pem`; if it is absent the call raises loudly rather than falling back to
insecure.

## Singletons vs per-node services

- **Singletons** (front a single external resource): `2fa` and `social`/the mira
  daemon are **canonical on mira** (always-on). Only `ssh` consumes 2FA, and it
  reaches `2fa@mira` via **per-service config** (no general auto-resolve).
- **Everything else is per-node** (local resource or node-owned state): visible
  on both, calls default local, nothing synced.

## Deploy

Land on `dev` and promote `feat → dev → release`; do **not** deploy from
`feat-federation`. `httpsfront` currently exists on `release` only — this branch
carries its own copy under `awm/services/httpsfront/`, so the eventual
`dev → release` merge must reconcile the two (they are the same service).

## Deferred (not in v1)

- Live SSH wiring/verification of the peer-auth channel (fir is lockout-sensitive
  — done carefully in a dedicated session).
- Migrating the singleton services onto mira: mira is stood up with `2fa` and
  `social` **disabled**; they stay canonical on Capella until the dedicated
  re-home session (which must prove prod `social` reaches the re-homed daemon
  before the mira monolith is retired).
- The ecspr HPC worked example (T5).
