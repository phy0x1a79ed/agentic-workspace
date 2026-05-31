# Agentic Workspace Manager (AWM)

*Human setup + usage guide for awm. Agents operating in this workspace should read [`WORKSPACE.md`](WORKSPACE.md) (auto-injected at session start) and search skills via `mcp__awm__skills_search` — not this file. **Do not merge this README into AGENTS.md or WORKSPACE.md** — their audience is agents in scope worktrees; this one's audience is humans installing, networking, and operating the system.*

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/scope management, file locking with crash recovery, skills catalog, session logging, experience tracking, artifact registration, inter-agent messaging, autonomous agent spawning, and an MCP server for direct tool use by Claude Code / OpenCode / other MCP clients.

For agent-facing structural docs (paths, MCP tools, scope lifecycle), see [`WORKSPACE.md`](WORKSPACE.md). For awm-internal architecture (Service Hub, vertical-stripe component dev), see [`AGENTS.md`](AGENTS.md).

## Quick Install

```bash
./setup.sh
```

This creates an `awm` mamba environment, installs the package, initializes the database, and adds `awm` and `awm-mcp` to your PATH.

## Manual Install

```bash
mamba env create -f environment.yml
mamba run -n awm pip install -e . --no-deps
mkdir -p .awm
mamba run -n awm python -m awm init
```

## Harness Integration

AWM drives **Claude Code** and **OpenCode** as first-class harnesses. The setup skill at [`awm/skills/awm/harness-setup.md`](awm/skills/awm/harness-setup.md) (also discoverable from inside an agent via `skills_get path="awm/harness-setup.md"`) covers:

- The `SessionStart` hook that runs `awm context emit` and injects `<workspace-context>` + `<agents-context>` + `<scope-context>` blocks into Claude Code sessions.
- The MCP exporter framework that fans `<workspace>/.mcp.json` out to backend-specific configs (`spawn-mcp.json` for claude, `mcp-opencode.json` for opencode) — registered services are advertised even when their upstream is down.
- Per-session harness selection via the `agent_cli` column on `agent_sessions`.
- Healing existing scopes that pre-date the wiring: `awm scope heal`.

## Server Lifecycle

The server auto-starts when you run any CLI command. To run manually:

```bash
awm serve          # foreground, local-only listener on 127.0.0.1:7819
awm status         # health check (auto-starts if needed)
awm stop           # stop the server
awm restart        # restart core via systemd (transparent to MCP clients)
awm refresh        # restart server to pick up source changes (dev mode)
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var; set to `0` to disable).

`awm <command> --help` lists every subcommand. For agent-facing usage (scopes, sessions, locks, messaging, rooms), see `WORKSPACE.md` — those workflows are typically driven from inside an MCP-equipped agent, not the shell.

## Network-Exposed Listener

The default `awm serve` listener is local-only (`127.0.0.1:7819`, no auth). A separate `awm serve-exposed` listener exposes a bearer-auth'd REST + WebSocket surface for everything that needs to leave the local process — federation, the rooms surface, and the browser UI.

Inter-peer traffic flows over **SSH tunnels** (see [Federation](#federation-networked-workspaces) below), so the exposed listener defaults to **plain HTTP bound to `127.0.0.1`**. TLS is optional (`AWM_TLS_CERT`/`AWM_TLS_KEY`) for the rare case where a browser sits on a different host than awm-exposed.

### Enable on a host

```bash
# 1. Generate a bearer token (chmod 600, prints once)
awm exposed init-token

# 2. Install the systemd unit (one-time)
cp deploy/awm-exposed.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now awm-exposed.service

# 3. Verify
awm exposed status
```

### Auth

A bearer token is **a key — proof of possession, nothing more**. Auth is not identity: any caller who can present a valid bearer is authenticated; *who* they are is a separate claim carried by `X-Awm-As` (user) and `X-Awm-From` (peer-origin) headers.

Every request needs `Authorization: Bearer <token>`. Token sources, in order: `$AWM_AUTH_TOKEN` env var, file at `$WORKSPACE_ROOT/.awm/auth.token`. Rotation is `echo newtoken > $WORKSPACE_ROOT/.awm/auth.token` — the listener re-reads the file on every request, no restart and no in-memory cache to flush.

Valid bearers come from two sources, both pure files on disk:

- The local-daemon token at `$WORKSPACE_ROOT/.awm/auth.token` (the operator key on this host).
- Per-peer tokens at `$WORKSPACE_ROOT/.awm/peers/<peer_id>.token` (installed by `awm peer add --bootstrap-via-ssh ...`, which fetches the remote's `~/.awm/auth.token` over SSH).

`/peer/*` routes additionally cross-check: the presented bearer must match the *specific* peer's token file matching the `X-Awm-From` header. This prevents peer A from impersonating peer B.

> **Note on the legacy `~/.awm/` path.** Earlier deploys (and the `deploy/install.sh` script) placed the token at `~/.awm/auth.token` so it survived workspace re-inits. The canonical location is now `$WORKSPACE_ROOT/.awm/auth.token` — the listener reads only the workspace path. If a stale `~/.awm/auth.token` is still present and a client uses it, `verify_bearer` logs a one-shot WARNING ("bearer matches stale ~/.awm/auth.token …") so the misconfiguration is visible. Either delete the home-dir file or symlink it to the workspace one.

**Browser bootstrap.** The web UI does not accept a bearer in the URL hash. Instead, the operator triggers a one-shot login flow:

- **`/login` slash command** on the per-peer Discord bot (if `[discord]` section configured) — the bot DMs a URL that sets the bearer as an HttpOnly cookie on click. Operators eligible for `/login` are listed in the `discord_operators` table (`awm discord add-operator <discord_user_id> <awm_user>`). The bot depends on `discord.py>=2.3` (declared in `environment.yml` / `pyproject.toml`); for existing deploys, refresh via `mamba env update -n awm -f environment.yml --prune` — without it, `awm/services/discord/bot.py` falls back to a no-op import and the operator must use the CLI-based `awm login` instead.
- **`awm login [--as <user>]`** on the daemon host — prints the same URL for the operator to open in a browser.

Either path mints a single-use nonce (60s TTL, in-memory), redeemed at `/auth/bootstrap?ot=<nonce>` which sets `awm_session=<bearer>; HttpOnly; Secure; SameSite=Strict` and redirects to `/ui/`. The long-lived bearer never leaves the daemon.

**TLS.** The daemon binds a self-signed cert (CN=awm-daemon) auto-bootstrapped at `$WORKSPACE_ROOT/.awm/tls/{cert,key}.pem`. Internal httpx callers pass `verify=False` to disable TLS server-cert verification (the chain is unknown to public CAs) — not to disable auth. The trust boundary is the transport (loopback for CLI/MCP, SSH tunnel for peer-to-peer), not the cert chain.

**Discovery.** `serve-exposed` writes `$WORKSPACE_ROOT/.awm/exposed.json` at startup with `{scheme, host, port, token_file, pid}`. CLI and MCP read this as the source of truth for the live listener, eliminating config drift previously seen across `awm exposed status`, `awm peer ping`, etc.

WebSocket clients authenticate via the `Sec-WebSocket-Protocol: bearer.<token>` subprotocol (browsers can't set arbitrary headers on the WS handshake), the `awm_session` HttpOnly cookie, or a `?token=<token>` query string.

### Destructive operations

`POST /projects` and `DELETE /scopes/{p}/{s}` are 403'd by default. Set `AWM_ALLOW_DESTRUCTIVE=1` in the environment to permit them. No restart needed — re-read each request.

### Audit log

Every authenticated mutating request to the mounted core surface appends one JSON line to `~/.awm/access.log`: `{ts, ip, method, path, status, latency_ms, peer_id}`. Prompts and message bodies are deliberately not logged.

### Co-existence with the local core

Both listeners can run simultaneously. They share the SQLite database and the same `awm/services/*` layer, but each has its own PID file (`awm.pid` / `awm-exposed.pid`), log file, and systemd unit. If the exposed listener crashes, the local IPC path keeps working.

## Federation: Networked Workspaces

Two or more awm instances can be linked so cross-machine messaging, read-only searches, and rooms work without leaving the awm shell. Each peer keeps its own SQLite database — federation is an explicit-registry routing layer, not a shared database.

**Transport: SSH tunnels.** awm opens a ControlMaster-pooled `ssh -fN -L <ephemeral>:127.0.0.1:7820 <alias>` to each peer on demand (socket at `$AWM_DIR/ssh/peer-<id>.sock`, persisted for 10m of idle). All HTTP and WebSocket traffic to a peer runs through this tunnel — no TLS certs, no public ports.

### Set up a peer pair

On each host (assumes both have run `deploy/install.sh`):

```bash
# 1. Give this instance a stable identity
awm peer init capella

# 2. Register the other side. The bearer token file is the REMOTE host's
#    auth.token (so this host can authenticate to it). It's copied into a
#    canonical location at $AWM_DIR/peers/<id>.token.
awm peer add crux --ssh-alias crux --remote-port 7820 \
    --token-file /path/to/crux-auth.token

# 3. Verify (opens the tunnel, echoes peer_id, updates last_seen)
awm peer ping crux
awm peer list
awm peer whoami
```

`--ssh-alias` is whatever you'd `ssh <alias>` to reach the peer (uses `~/.ssh/config`). If one peer can't ssh to the other directly (e.g. WSL2 NAT), register that side with an empty `--ssh-alias` and a `--remote-port` pointing at an out-of-band reverse forward (e.g. `ssh -fN -R 7821:127.0.0.1:7820 <other-host>` maintained from the reachable side).

### Cross-host messaging

Append `@<peer-id>` to any scope address to route the operation to that peer:

```bash
# capella → crux
awm inbox send scope:awm/remote-api@crux \
    --sender opal --subject "ping" --body "hello crux"

# On crux, the message lands locally with sender = peer:capella/opal
ssh crux awm inbox fetch scope:awm/remote-api
```

The outbound peer's bearer (read from the registered `token_path`) is sent in `Authorization`; the local instance's `peer_id` is sent in `X-Awm-From`. The receiving end re-writes `sender` to `peer:<from-peer-id>/<original-sender>` before storing.

`inbox fetch` and `inbox search` reject `@<peer-id>` scope addresses — fetching from a remote peer goes through `ssh <peer> awm inbox fetch ...` for now. Federated read fan-out is implemented for skills (see below); inbox fan-out is on the roadmap.

### Federated find

The `--peer` flag on read commands queries across the peer set and tags each result with its origin:

```bash
# Search just one remote peer
awm skill search "annotation" --peer crux

# Fan out to every registered peer
awm skill list --peer all
```

A peer that times out (default 5s) lands in a `degraded: [...]` field in the response; the command still exits 0 with whatever did succeed.

### Cross-peer rooms

```bash
# Search rooms across all registered peers
awm room search "topic" --peer all

# Operate on a remote-hosted room (read, post, close) over the tunnel
awm room get <name>@crux
awm room post <name>@crux "hello from here"
awm room join <name>@crux      # WS attached via the tunnel
```

Posts forwarded to a remote room are tagged `user:<as>@<origin-peer-id>` on the host side. The cross-peer agent-input path (room hosted on peer A, agent process on peer B) is wired via `forward_agent_input` + `/rooms/internal/agent-input`.

### Inbound peer auth

The exposed listener trusts whatever bearer it accepts for that host; `X-Awm-From` is the origin claim used for audit + sender rewriting. The receiver verifies the claimed peer-id is in its registry — unknown peer-ids get a 400. Audit lines (`~/.awm/access.log`) tag federated calls with `peer_id`, operator calls with `peer_id: null`.

### Leadership / failover

The exposed listener does application-layer leader election so only one peer at a time mounts the operator-facing UI and holds the Discord bot gateway. Each peer has an integer `peer_priority` (lower wins; ties broken by `peer_id`); the lowest-priority reachable peer is ACTIVE, others are STANDBY.

```bash
# Set priorities — lower number = higher precedence.
awm peer set-priority self 10                       # this peer is the primary
awm peer set-priority xps 20                        # xps is the fallback
```

In STANDBY, `/ui/*`, `/auth/mint`, and `/auth/bootstrap` return `503 + Location: <leader>/...` so operators land on whichever peer is currently ACTIVE. `/peer/*` federation, `/status`, and `/auth/whoami` stay reachable on every peer. The Discord bot (Shape A: single shared token in each peer's `$AWM_DIR/discord.toml`) only connects from the ACTIVE peer; Discord's one-gateway-per-token rule means failover is clean — the new ACTIVE peer's reconnect succeeds within ~30s.

State machine: K=3 consecutive `/status` failures from every higher-priority peer to promote (~9s @ 3s interval); a single success from any of them demotes immediately. Current leader is gossiped via `/status` and surfaced in `$AWM_DIR/exposed.json` and the control-center status-tab badge.

### DB replication (cr-sqlite)

Replicable tables (`rooms`, `room_participants`, `peers`, `discord_operators` today; the rest in follow-up PRs as each INTEGER→UUID migration lands) are marked CRR via the vendored cr-sqlite extension at `awm/_native/crsqlite.so`. The exposed listener runs a pull loop every 5s that asks each remote peer `GET /peer/db-sync?since=<cursor>` and applies the binary changeset locally. Per-peer cursor lives in `peer_sync_state` (local-only). Locks and agent_sessions stay per-peer-local.

The extension is loaded automatically in `db.get_connection()`; if the binary is missing the daemon logs a warning and runs in single-peer mode (replication is a no-op, the rest of AWM works fine).

### Sync source between peers during development

`deploy/sync-to-peer.sh <host>` rsyncs the package + `pip install -e` on the remote. `--restart` adds a `systemctl --user restart` on the peer. Used as the inner loop while iterating on the federation code.

### End-to-end validation

`deploy/v0_rooms.sh` runs the rooms + tunnel validation against a paired `xaw` peer: tunnel up + ping both directions, rooms CRUD, cross-peer search/get/post, multi-subscriber WS attach with backlog replay, localhost binding (negative), schema/scope-unique guarantees.

`deploy/v0_e2e.sh` is the legacy federation-only suite (messaging + skill search) — kept for regression checks.

## Architecture

```
                  ┌─────────┐  ┌──────────┐  ┌───────────┐
                  │ Typer   │  │ FastAPI  │  │ MCP stdio │
                  │ CLI     │  │ HTTP     │  │ Server    │
                  └────┬────┘  └────┬─────┘  └─────┬─────┘
                       │            │              │
                       └────────────┼──────────────┘
                                    │
                           ┌────────▼────────┐
                           │  awm/services/  │
                           │  (shared core)  │
                           └────────┬────────┘
                                    │
                        ┌───────────┼───────────┐
                        │           │           │
                   ┌────▼───┐ ┌────▼────┐ ┌────▼────┐
                   │ SQLite │ │  Files  │ │  Git    │
                   │ (index)│ │(content)│ │(history)│
                   └────────┘ └─────────┘ └─────────┘
```

```
awm/                      # Git-tracked Python package
  __init__.py
  __main__.py             # Entry point (python -m awm)
  cli.py                  # Typer CLI
  server.py               # FastAPI + uvicorn
  exposed.py              # Bearer-auth'd network listener
  mcp_server.py           # MCP stdio server
  config.py               # Paths and settings
  db.py                   # SQLite (WAL mode) + migrations
  models.py               # Pydantic models
  services/               # Core service layer (scopes, rooms, peers, …)
.awm/                     # Runtime state (gitignored)
  state.db                # SQLite database
  awm.pid / awm-exposed.pid
  awm.log / awm-exposed.log
  spawn-mcp.json / mcp-opencode.json   # MCP exporter fan-out
.mcp.json                 # MCP server registration
```

For the service-layer architecture in detail, see [`AGENTS.md`](AGENTS.md).

## Troubleshooting

**Port in use**: `awm stop` then retry, or `lsof -i :7819` to find the process.

**Stale locks**: `awm lock reap` forces cleanup. The reaper also runs automatically every 30s.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Delete `.awm/state.db` and run `awm init` to recreate.

**MCP not connecting**: Verify `.mcp.json` exists at workspace root. Check that `awm-mcp` is on PATH (`mamba run -n awm which awm-mcp`). Restart Claude Code to pick up changes.
