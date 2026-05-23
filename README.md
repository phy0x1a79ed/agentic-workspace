# Agentic Workspace Manager (AWM)

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/scope management, file locking with crash recovery, skills catalog, session logging, experience tracking, artifact registration, inter-agent messaging, autonomous agent spawning, and an MCP server for direct tool use by Claude Code.

AWM supports a **two-level agent hierarchy** — a workspace agent that triages and delegates, and scope agents that do the actual work. See [Agent System](#agent-system) for details.

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

## Usage

### Server

The server auto-starts when you run any CLI command. To run manually:

```bash
awm serve          # foreground
awm status         # health check (auto-starts if needed)
awm stop           # stop the server
awm restart        # restart core via systemd (transparent to MCP clients)
awm refresh        # restart server to pick up source changes (dev mode)
```

### Projects

```bash
awm project create myproject
awm project create myproject --clone https://github.com/org/repo.git
awm project create myproject --fork https://github.com/org/repo.git
```

### Scopes

Scopes are isolated git worktrees where agents do their work.

```bash
awm scope create myproject analysis-v1
awm scope create myproject analysis-v1 --from develop
awm scope list
awm scope list --status active --project myproject
awm scope complete myproject analysis-v1
awm scope complete myproject analysis-v1 --merge
awm scope delete myproject analysis-v1
```

### Skills

```bash
awm skill list                              # list all skills with metadata
awm skill list --type protocol              # filter by type
awm skill list --tags git,workflow           # filter by tags
awm skill get tools/git.md                  # read a skill file
awm skill search "HPC annotation"           # hybrid keyword + semantic search
awm skill reindex                           # regenerate index + embeddings
```

### Session Logging

```bash
# Log a session
awm session log myproject analysis-v1 \
  --summary "Completed normalization pipeline" \
  --decision "Used quantile normalization for cross-sample comparability" \
  --issue "Missing values in batch 3 required imputation" \
  --next-step "Validate with PCA plot" \
  --agent agent1

# Query sessions
awm session list --project myproject
awm session get 42
```

### Locking

```bash
# Acquire a file lock
awm lock acquire data/myproject/raw/sample.csv --holder agent1

# Acquire a folder lock (trailing slash)
awm lock acquire data/myproject/ --holder agent1 --type exclusive

# Shared lock (multiple readers)
awm lock acquire data/reference/genome.fa --holder agent1 --type shared
awm lock acquire data/reference/genome.fa --holder agent2 --type shared

# Release
awm lock release data/myproject/ --holder agent1

# List and reap
awm lock list
awm lock list --holder agent1
awm lock reap
```

### Shared Resource Edits

For editing tracked files in the outer repo (AGENTS.md, awm/ package files):

```bash
awm shared edit --name update-git-sop --by agent1
# Work in the shared edit worktree, commit changes
awm shared merge --name update-git-sop
awm shared list
```

## Agent System

AWM implements a two-level agent hierarchy defined by AGENTS.md persona content. The workspace agent orchestrates while scope agents execute.

### Levels

| Level | Directory | Role |
|-------|-----------|------|
| **Workspace** | Workspace root | Triage, route, delegate, monitor — never does implementation work |
| **Scope** | `projects/{project}/{scope}/` | Executes work in an isolated git worktree |

### Messaging

Agents communicate via scoped message queues stored in SQLite. Four MCP tools, each with a distinct job:

| Tool | Purpose |
|---|---|
| `inbox_send` | Post a message to a scope |
| `inbox_search` | **Browse previews** (no body) across scopes — for triage/discovery |
| `inbox_fetch` | **Read full messages** for a specific scope — the "read my inbox" primitive |
| `inbox_mark_read` | Ack a single message by id (rarely needed; `inbox_fetch mark_read=true` covers bulk) |

```bash
# Send a message
inbox_send scope=project:myproject sender=workspace msg_type=scope_assignment subject="New work" body="..."

# Read your inbox (full bodies, scope required). Add mark_read=true to consume.
inbox_fetch scope=scope:myproject/analysis-v1
inbox_fetch scope=workspace mark_read=true

# Browse/triage across scopes — returns previews only (cheap)
inbox_search status=unread
inbox_search msg_type=reflection
inbox_search query="deployment"

# Ack a single message by id (for fine-grained cases)
inbox_mark_read id=42

# List valid recipients
inbox_recipients
```

**Message types**: `scope_assignment`, `reflection`, `status_update`, `notification`, `plan`

### Rooms (agent orchestration)

Agents are driven via **rooms** — multi-participant conversations that
serialize input into a scope's claude session and broadcast its output
back to all participants. A scope can be in multiple rooms; one process
per `(project, scope)` is enforced. Use `awm room` CLI or `room_*` MCP
tools (see `## Rooms` below for the full surface).

```bash
# One-off: spawn an agent, post a prompt, auto-close when it exits
awm room one-off --scope awm/my-scope --prompt "summarize objectives.md"

# Long-running room with a specific topic
awm room create --topic "research" --scope awm/my-scope --prompt "start"
awm room post <name> "another message"
awm room join <name>            # terminal-attached WS subscriber
awm room close <name> --kill-agents
```

Only `claude` is supported as the agent CLI (it's the only one with
duplex stream-json). Concurrent rooms feeding the same scope serialize
inputs in FIFO order with `[room:X from:Y]` framing on stdin.

The 3-level agent hierarchy (workspace / project / scope) is documented
in the workspace-level `AGENTS.md`.

## MCP Server

AWM includes an MCP (Model Context Protocol) server for direct integration with Claude Code and other MCP clients. It exposes tools covering skills, sessions, scopes, artifacts, projects, locks, messaging, agents, lifecycle, and status.

### Setup

The `.mcp.json` at the workspace root registers the server:

```json
{
  "mcpServers": {
    "awm": {
      "command": "mamba",
      "args": ["run", "-n", "awm", "awm-mcp"],
      "env": { "AWM_WORKSPACE": "/home/tony/agentic_workspace" }
    }
  }
}
```

Claude Code automatically discovers this file and connects to the MCP server.

### Running Manually

```bash
awm-mcp    # starts stdio MCP server (used by MCP clients, not interactive)
```

### Tools

| Category | Tools |
|----------|-------|
| Skills | `skills_list`, `skills_get`, `skills_search`, `skills_sync` |
| Sessions | `session_log`, `session_list`, `session_get` |
| Scopes | `scope_create`, `scope_list`, `scope_complete`, `scope_delete` |
| Artifacts | `artifact_register`, `artifact_search`, `artifacts_sync` |
| Projects | `project_create` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Messaging | `inbox_send`, `inbox_search`, `inbox_fetch`, `inbox_mark_read`, `inbox_recipients` |
| Rooms | `room_create`, `room_list`, `room_get`, `room_history`, `room_search`, `room_post`, `room_invite`, `room_remove`, `room_close` |
| Lifecycle | `awm_status`, `awm_restart`, `awm_refresh` |

## REST API

The server listens on `127.0.0.1:7819`. Key endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | Health + summary |
| POST | `/restart` | Restart core via systemd (async, returns immediately) |
| POST | `/projects` | Create project |
| GET | `/scopes` | List scopes |
| POST | `/scopes` | Create scope |
| PATCH | `/scopes/{project}/{scope}` | Update scope (complete) |
| DELETE | `/scopes/{project}/{scope}` | Delete scope |
| POST | `/locks` | Acquire lock |
| DELETE | `/locks` | Release lock |
| GET | `/locks` | List locks |
| POST | `/locks/heartbeat` | Renew heartbeat |
| POST | `/locks/reap` | Force reap stale locks |
| POST | `/shared` | Start shared edit |
| POST | `/shared/{name}/merge` | Merge shared edit |
| GET | `/shared` | List shared edits |
| GET | `/skills` | List skills (query: `type`, `tags`) |
| GET | `/skills/search` | Search skills (query: `q`) |
| GET | `/skills/{path}` | Get skill content |
| POST | `/sessions` | Log a session entry |
| GET | `/sessions` | List session logs (query: `project`, `scope`, `limit`) |
| GET | `/sessions/{id}` | Get session with full content |
| GET | `/peers` | List registered remote peers (control-center) |
| GET | `/peers/{id}/ping` | Probe peer via SSH tunnel, report rtt |
| GET | `/projects` | List projects with per-status scope counts |
| GET | `/voice/state` | Current user's voice agent status (joined rooms, connected tabs) |
| POST | `/voice/text` | Send typed input to the voice agent (no-mic alternative) |
| POST | `/voice/rooms/{id}/join` | Voice agent joins a room as a `voice:<user>` participant |
| POST | `/voice/rooms/{id}/leave` | Voice agent leaves a room |
| WS | `/voice/ws` | Per-user voice WebSocket (PTT audio, transcripts, TTS playback) |

## Network-Exposed Listener

The default `awm serve` listener is local-only (`127.0.0.1:7819`, no
auth). A separate `awm serve-exposed` listener exposes a bearer-auth'd
REST + WebSocket surface for everything that needs to leave the local
process — federation, the rooms surface, and the browser UI.

Inter-peer traffic flows over **SSH tunnels** (see `## Federation`
below), so the exposed listener defaults to **plain HTTP bound to
`127.0.0.1`**. TLS is optional (`AWM_TLS_CERT`/`AWM_TLS_KEY`) for the
rare case where a browser sits on a different host than awm-exposed.

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

A bearer token is **a key — proof of possession, nothing more**. Auth is
not identity: any caller who can present a valid bearer is authenticated;
*who* they are is a separate claim carried by `X-Awm-As` (user) and
`X-Awm-From` (peer-origin) headers.

Every request needs `Authorization: Bearer <token>`. Token sources, in
order: `$AWM_AUTH_TOKEN` env var, file at `$WORKSPACE_ROOT/.awm/auth.token`.
Rotation is `echo newtoken > $WORKSPACE_ROOT/.awm/auth.token` — the
listener re-reads the file on every request, no restart and no in-memory
cache to flush.

Valid bearers come from two sources, both pure files on disk:

- The local-daemon token at `$WORKSPACE_ROOT/.awm/auth.token` (the
  operator key on this host).
- Per-peer tokens at `$WORKSPACE_ROOT/.awm/peers/<peer_id>.token`
  (installed by `awm peer add --bootstrap-via-ssh ...`, which fetches
  the remote's `~/.awm/auth.token` over SSH).

`/peer/*` routes additionally cross-check: the presented bearer must
match the *specific* peer's token file matching the `X-Awm-From` header.
This prevents peer A from impersonating peer B.

**Browser bootstrap.** The web UI does not accept a bearer in the URL
hash. Instead, the operator triggers a one-shot login flow:

- **`/login` slash command** on the per-peer Discord bot (if
  `[discord]` section configured) — the bot DMs a URL that sets the
  bearer as an HttpOnly cookie on click. Operators eligible for `/login`
  are listed in the `discord_operators` table (`awm discord
  add-operator <discord_user_id> <awm_user>`).
- **`awm login [--as <user>]`** on the daemon host — prints the same
  URL for the operator to open in a browser.

Either path mints a single-use nonce (60s TTL, in-memory), redeemed at
`/auth/bootstrap?ot=<nonce>` which sets `awm_session=<bearer>;
HttpOnly; Secure; SameSite=Strict` and redirects to `/ui/`. The
long-lived bearer never leaves the daemon.

**TLS.** The daemon binds a self-signed cert (CN=awm-daemon) auto-
bootstrapped at `$WORKSPACE_ROOT/.awm/tls/{cert,key}.pem`. Internal
httpx callers pass `verify=False` to disable TLS server-cert
verification (the chain is unknown to public CAs) — not to disable auth.
The trust boundary is the transport (loopback for CLI/MCP, SSH tunnel
for peer-to-peer), not the cert chain.

**Discovery.** `serve-exposed` writes `$WORKSPACE_ROOT/.awm/exposed.json`
at startup with `{scheme, host, port, token_file, pid}`. CLI and MCP
read this as the source of truth for the live listener, eliminating the
config drift previously seen across `awm exposed status`, `awm peer
ping`, etc.

WebSocket clients authenticate via the
`Sec-WebSocket-Protocol: bearer.<token>` subprotocol (browsers can't set
arbitrary headers on the WS handshake), the `awm_session` HttpOnly
cookie, or a `?token=<token>` query string.

### Destructive operations

`POST /projects` and `DELETE /scopes/{p}/{s}` are 403'd by default. Set
`AWM_ALLOW_DESTRUCTIVE=1` in the environment to permit them. No restart
needed — re-read each request.

### Audit log

Every authenticated mutating request to the mounted core surface
appends one JSON line to `~/.awm/access.log`:
`{ts, ip, method, path, status, latency_ms, peer_id}`. Prompts and
message bodies are deliberately not logged.

### Co-existence with the local core

Both listeners can run simultaneously. They share the SQLite database
and the same `awm/services/*` layer, but each has its own PID file
(`awm.pid` / `awm-exposed.pid`), log file, and systemd unit. If the
exposed listener crashes, the local IPC path keeps working.

## Rooms

Rooms are awm's multi-participant conversation primitive — humans,
agents (scopes), and other peers can all be participants. Output from
an agent fans out to every room the scope is in; input from any
participant is serialized into the agent's stdin with
`[room:<name> from:<author>]` framing.

| Method | Path                            | Notes                                  |
|--------|---------------------------------|----------------------------------------|
| POST   | `/rooms`                        | Create a room (optional scopes/prompts/close_on_exit) |
| GET    | `/rooms`                        | List rooms (`?status`, `?participating_scope`, `?peer=all\|<id>`) |
| GET    | `/rooms/{id}`                   | Room + participants + recent transcript |
| GET    | `/rooms/{id}/history`           | Longer transcript slice (`?limit_chars`, `?before_ts`) |
| GET    | `/rooms/search`                 | Search by topic / id / transcript (`?q`, `?peer=all\|<id>`) |
| POST   | `/rooms/{id}/posts`             | Post a message (`{body, kind?, to?}`)  |
| POST   | `/rooms/{id}/invite`            | Add a scope (spawns agent if needed)   |
| POST   | `/rooms/{id}/remove`            | Remove a scope (agent keeps running)   |
| POST   | `/rooms/{id}/close`             | Close (optionally `--kill-agents`)     |
| POST   | `/rooms/{id}/archive`           | Soft-archive (409 if active scope participants remain) |
| GET    | `/rooms/{id}/agents`            | Per-agent panel data (scope/shadow_peer + live session state) |
| WS     | `/rooms/{id}/attach`            | Subscriber WS — JSON envelope protocol |

Room names are auto-generated `verb-noun` pairs (`babbling-brook`,
`flowing-mountain`, …) from a ~2500-entry pool, with `-2`, `-3`, …
suffixes on collision.

The WS envelope is documented in code (`awm/ws_envelope.py`); inbound
shapes are `{type: post|control|ping}`, outbound are
`{type: history|post|participant_joined|participant_left|room_closed|upstream_disconnected|lagged|error|pong}`.

### CLI

```bash
awm room create [--topic T] [--scope SCOPE]... [--prompt SCOPE=TEXT]... [--close-on-exit]
awm room list   [--peer all|<id>] [--status active]
awm room get    <name>[@peer]
awm room history <name>[@peer] [--limit-chars N]
awm room search "<query>" [--peer all]
awm room post   <name>[@peer] <text> [--to <scope>]
awm room invite <name>[@peer] --scope <scope> [--prompt ...]
awm room remove <name>[@peer] --scope <scope>
awm room close  <name>[@peer] [--kill-agents]
awm room archive <name>           # 409 if active scope participants remain
awm room agents  <name>           # list participants + live session state
awm room join   <name>[@peer]   # terminal-attached WS
awm room one-off --scope <scope> --prompt "..."   # create + close-on-exit
```

`@peer` suffix routes to a remote peer via the SSH tunnel; queries
without `@` hit the local exposed listener.

### Browser UI

`http://<host>:7820/ui/index.html#token=<bearer>&as=<user>&peer=<id>` —
single-file vanilla-JS control center with hash-routed tabs:

- `#/status` — local peer, registered peers (ping), projects, scopes, core status
- `#/rooms` — search / create / archive; filter by status (active|closed|archived|all) or peer
- `#/room/<id>` — chat transcript + composer + collapsible per-agent panels (PID, status, agent_cli)

Token + identity go in the URL hash so they never reach server logs. The
legacy `/ui/room.html` URL redirects into `#/room/<id>` for backwards
compatibility with existing bookmarks.

### Voice side panel

The control center has an always-visible voice side panel on the right
of every tab. Push-to-talk (mouse on the PTT button or hold `Space`
outside text fields), faster-whisper STT, a persistent `claude`
subprocess as the voice agent, kyutai pocket-tts for playback, and a
`show()` MCP side channel that renders code / paths / links / text into
the transcript without speaking them.

- **Per-user singleton, cross-window sync.** One voice agent per
  identified user (keyed off the `X-Awm-As` header / hash). Multiple
  browser tabs of the same user share that agent — PTT in window A
  streams the transcript and TTS to every tab in real time. The agent
  persists across tab close/reload; idle for `VOICE_IDLE_SEC` (30 min
  default) with no connected clients releases it.
- **Joins rooms as a real participant.** Click **+ join** in the side
  panel to register the voice agent into an active room with kind
  `voice` and identifier `<user>`. The agent's claude has MCP access to
  the `awm` rooms tools (`room_list`, `room_get`, `room_post`,
  `room_history`, `room_invite`, `room_search`) and posts as
  `voice:<user>` in the joined room's transcript.
- **HTTPS / mic access.** Browsers gate `getUserMedia` to secure
  contexts. `localhost` is exempt; for ZeroTier / LAN access, configure
  `AWM_TLS_CERT`/`AWM_TLS_KEY` on the exposed listener.
- **First-run model download.** Whisper `small.en` + pocket-tts weights
  + the default voice (`VOICE_NAME=jean`) total ~700 MB on first launch
  (cached under `~/.cache/huggingface/`). The exposed listener starts
  immediately and warms the models in the background — first PTT may
  wait a few seconds while loading completes.

## Federation: Networked Workspaces

Two or more awm instances can be linked so cross-machine messaging,
read-only searches, and rooms work without leaving the awm shell.
Each peer keeps its own SQLite database — federation is an explicit-
registry routing layer, not a shared database.

**Transport: SSH tunnels.** awm opens a ControlMaster-pooled
`ssh -fN -L <ephemeral>:127.0.0.1:7820 <alias>` to each peer on demand
(socket at `$AWM_DIR/ssh/peer-<id>.sock`, persisted for 10m of idle).
All HTTP and WebSocket traffic to a peer runs through this tunnel —
no TLS certs, no public ports.

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

`--ssh-alias` is whatever you'd `ssh <alias>` to reach the peer (uses
`~/.ssh/config`). If one peer can't ssh to the other directly
(e.g. WSL2 NAT), register that side with an empty `--ssh-alias` and a
`--remote-port` pointing at an out-of-band reverse forward (e.g.
`ssh -fN -R 7821:127.0.0.1:7820 <other-host>` maintained from the
reachable side).

### Cross-host messaging

Append `@<peer-id>` to any scope address to route the operation to that
peer:

```bash
# bare → xaw
awm inbox send scope:awm/remote-api@crux \
    --sender opal --subject "ping" --body "hello xaw"

# On crux, the message lands locally with sender = peer:capella/opal
ssh xaw awm inbox fetch scope:awm/remote-api
```

The outbound peer's bearer (read from the registered `token_path`) is
sent in `Authorization`; the local instance's `peer_id` is sent in
`X-Awm-From`. The receiving end re-writes `sender` to
`peer:<from-peer-id>/<original-sender>` before storing.

`inbox fetch` and `inbox search` reject `@<peer-id>` scope addresses —
fetching from a remote peer goes through `ssh <peer> awm inbox fetch ...`
for now. Federated read fan-out is implemented for skills (see below);
inbox fan-out is on the roadmap.

### Federated find

The `--peer` flag on read commands queries across the peer set and tags
each result with its origin:

```bash
# Search just one remote peer
awm skill search "annotation" --peer crux

# Fan out to every registered peer
awm skill list --peer all
```

A peer that times out (default 5s) lands in a `degraded: [...]` field
in the response; the command still exits 0 with whatever did succeed.

### Cross-peer rooms

```bash
# Search rooms across all registered peers
awm room search "topic" --peer all

# Operate on a remote-hosted room (read, post, close) over the tunnel
awm room get <name>@crux
awm room post <name>@crux "hello from here"
awm room join <name>@crux      # WS attached via the tunnel
```

Posts forwarded to a remote room are tagged
`user:<as>@<origin-peer-id>` on the host side. The cross-peer agent-
input path (room hosted on peer A, agent process on peer B) is wired
via `forward_agent_input` + `/rooms/internal/agent-input`.

### Inbound peer auth

The exposed listener trusts whatever bearer it accepts for that host;
`X-Awm-From` is the origin claim used for audit + sender rewriting. The
receiver verifies the claimed peer-id is in its registry — unknown
peer-ids get a 400. Audit lines (`~/.awm/access.log`) tag federated
calls with `peer_id`, operator calls with `peer_id: null`.

### Leadership / failover

The exposed listener does application-layer leader election so only one
peer at a time mounts the operator-facing UI and holds the Discord bot
gateway. Each peer has an integer `peer_priority` (lower wins; ties
broken by `peer_id`); the lowest-priority reachable peer is ACTIVE,
others are STANDBY.

```bash
# Set priorities — lower number = higher precedence.
awm peer set-priority self 10                       # this peer is the primary
awm peer set-priority xps 20                        # xps is the fallback
```

In STANDBY, `/ui/*`, `/auth/mint`, and `/auth/bootstrap` return
`503 + Location: <leader>/...` so operators land on whichever peer is
currently ACTIVE. `/peer/*` federation, `/status`, and `/auth/whoami`
stay reachable on every peer. The Discord bot (Shape A: single shared
token in each peer's `$AWM_DIR/discord.toml`) only connects from the
ACTIVE peer; Discord's one-gateway-per-token rule means failover is
clean — the new ACTIVE peer's reconnect succeeds within ~30s.

State machine: K=3 consecutive `/status` failures from every
higher-priority peer to promote (~9s @ 3s interval); a single success
from any of them demotes immediately. Current leader is gossiped via
`/status` and surfaced in `$AWM_DIR/exposed.json` and the control-center
status-tab badge.

### DB replication (cr-sqlite)

Replicable tables (`rooms`, `room_participants`, `peers`,
`discord_operators` today; the rest in follow-up PRs as each
INTEGER→UUID migration lands) are marked CRR via the
vendored cr-sqlite extension at `awm/_native/crsqlite.so`. The exposed
listener runs a pull loop every 5s that asks each remote peer
`GET /peer/db-sync?since=<cursor>` and applies the binary changeset
locally. Per-peer cursor lives in `peer_sync_state` (local-only). Locks
and agent_sessions stay per-peer-local.

The extension is loaded automatically in `db.get_connection()`; if the
binary is missing the daemon logs a warning and runs in single-peer
mode (replication is a no-op, the rest of AWM works fine).

### Sync source between peers during development

`deploy/sync-to-peer.sh <host>` rsyncs the package + `pip install -e`
on the remote. `--restart` adds a `systemctl --user restart` on the
peer. Used as the inner loop while iterating on the federation code.

### End-to-end validation

`deploy/v0_rooms.sh` runs the rooms + tunnel validation against a
paired `xaw` peer: tunnel up + ping both directions, rooms CRUD,
cross-peer search/get/post, multi-subscriber WS attach with backlog
replay, localhost binding (negative), schema/scope-unique guarantees.

`deploy/v0_e2e.sh` is the legacy federation-only suite (messaging +
skill search) — kept for regression checks.

## Locking Protocol

1. **Acquire** before accessing shared resources
2. **Heartbeat** every 30 seconds (`POST /locks/heartbeat?holder=<id>`)
3. **Release** when done
4. **Stale locks** are automatically reaped after 120s without heartbeat, or immediately if the holder PID is gone
5. **Folder locks** (path ending with `/`) cover everything beneath that path
6. **Shared locks** allow multiple concurrent readers; exclusive locks block all others

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
  mcp_server.py           # MCP stdio server
  config.py               # Paths and settings
  db.py                   # SQLite (WAL mode) + migrations
  models.py               # Pydantic models
  services/
    core.py               # Core lifecycle (restart)
    projects.py           # Project CRUD
    scopes.py             # Scope CRUD (worktrees)
    locks.py              # Lock management
    skills.py             # Skills scanning + index generation
    sessions.py           # Session log CRUD (includes experience tracking)
    artifacts.py          # Artifact registration + search
    embeddings.py         # Sentence-transformer embeddings
    shared_resources.py   # Outer-repo worktree flow
    messaging.py          # Scoped message queues (inbox)
    agents.py             # Fire-and-forget agent spawning
    config_service.py     # Key-value config store
.awm/                     # Runtime state (gitignored)
  state.db                # SQLite database (schema v11)
  awm.pid                 # Server PID
  awm.log                 # Server log
.mcp.json                 # MCP server registration
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var).

## Troubleshooting

**Port in use**: `awm stop` then retry, or `lsof -i :7819` to find the process.

**Stale locks**: `awm lock reap` forces cleanup. The reaper also runs automatically every 30s.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Delete `.awm/state.db` and run `awm init` to recreate (schema v11).

**MCP not connecting**: Verify `.mcp.json` exists at workspace root. Check that `awm-mcp` is on PATH (`mamba run -n awm which awm-mcp`). Restart Claude Code to pick up changes.
