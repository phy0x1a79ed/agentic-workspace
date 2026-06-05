# Agentic Workspace Manager (AWM)

*Human setup + usage guide for awm. Agents operating in this workspace load [`WORKSPACE.md`](WORKSPACE.md) at session start via the harness's native mechanism (see [`awm/skills/awm/harness-setup.md`](awm/skills/awm/harness-setup.md)) and search skills via `mcp__awm__skills_search` — not this file. **Do not merge this README into AGENTS.md or WORKSPACE.md** — their audience is agents in scope worktrees; this one's audience is humans installing, networking, and operating the system.*

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/scope management, file locking with crash recovery, skills catalog, session logging, experience tracking, artifact registration, inter-agent messaging, autonomous agent spawning, and an MCP server for direct tool use by Claude Code / OpenCode / other MCP clients.

For agent-facing structural docs (paths, MCP tools, scope lifecycle), see [`WORKSPACE.md`](WORKSPACE.md). For **awm-internal architecture** — modifying the hub, registry, supervisor, RPC layer, manifest generator — see [`AGENTS.md`](AGENTS.md). This README covers install and the *usage* side of the package model (authoring components / services / pages); AGENTS.md covers the *implementation* side.

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

- How Claude Code and OpenCode each pick up the 3-tier orientation (workspace `WORKSPACE.md` + repo `AGENTS.md` + scope `.awm/context.md`) — CC via instructions in `~/.claude/CLAUDE.md` that direct the agent to Read each tier; OC via native `AGENTS.md` walk-up plus per-scope `mcp-opencode.json` `instructions` array for the other two.
- The MCP exporter framework that fans `<workspace>/.mcp.json` out to backend-specific configs (`spawn-mcp.json` for claude, `mcp-opencode.json` for opencode) — registered services are advertised even when their upstream is down.
- Per-session harness selection via the `agent_cli` column on `agent_sessions`.
- Healing existing scopes that pre-date the wiring: `awm scope heal`.

## Developing a package

Packages live under `packages/{components,services,pages}/<name>/`. The
subfolder *is* the kind — there's no `kind` field anywhere; `awm packages
sync` walks `packages/{services,pages}/` and registers each with the
matching hub kind. Authors write source only — `package.json` and
`vite.config.ts` are generated from layout + import scan.

### Three package kinds, by folder

```
packages/
  components/<name>/   ← library; importable across the workspace; no URL
  services/<name>/     ← long-running backend; reached at /svc/<name>/…
  pages/<name>/        ← static bundle; served at /ui/<name>/
  _shared/             ← hand-maintained shared Vite config base
  dev.sh               ← saved dev-shadow templates (see Iterating below)
```

### What you author on disk

| Kind | Files you write |
|---|---|
| Component | `src/index.ts` (barrel re-exporting each public component) plus `.svelte` / `.ts` / `.css` files it ships. That's it. |
| Page | `src/main.ts` (entry; calls `mount(App, …)`), `src/App.svelte` (root component), `index.html`, plus any per-page components in `src/components/`. Prefix defaults to `/ui/<dirname>`; an alternative prefix lives in an optional one-line `prefix.txt` at the package root. |
| Service | `start.sh` (`exec`s the runtime), plus whatever source the service needs (`backend/` Python, `server.js` Node, etc.). |

`package.json` and `vite.config.ts` are generated, not authored. The generator
scans `src/` for `from '@awm/<x>'` imports and writes the inferred
`dependencies`; the per-page Vite config is a one-line re-export of
`packages/_shared/vite.config.base.ts`. It's idempotent. CI gates on
`git diff --quiet` after a fresh run.

### Building a page

Boot sequence (driven by `dev/run.sh _build_packages`):

```
awm packages gen $REPO_ROOT            # write per-package package.json + vite.config.ts
npm install --no-audit --no-fund       # symlink workspace deps
npm run build --workspaces --if-present # vite build per page → packages/pages/<name>/dist/
awm packages sync $REPO_ROOT           # register pages + spawn services
```

### Authoring a service

`start.sh` is the only entry point the hub knows about. The hub injects:

| Var | Purpose |
|---|---|
| `AWM_HUB_URL` | base URL of the hub control plane (e.g. `http://127.0.0.1:7819/`) |
| `AWM_SERVICE_NAME` | the service name (= directory name) |
| `AWM_SERVICE_ID` | empty on first spawn; populated by the hub on reconnect after restart |

A typical `start.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec mamba run -n awm --no-capture-output python -m my_service.hub_adapter
```

The adapter POSTs `/hub/service/register` (sending its PID + start_cmd + cwd),
then opens `WS /hub/service/control/<service_id>` and sends a `ready` frame
carrying the api manifest:

```jsonc
{
  "kind": "ready",
  "api": {
    "functions": [
      {"name": "listEngines"},
      {"name": "savePreset"},
      {"name": "delPreset", "no_response": true}
    ],
    "emitters": [{"topic": "engine.loaded"}],
    "sessions": [{"kind": "call", "transport": "direct"}]
  }
}
```

Declare `transport: "direct"` on any session or emitter where you want a
raw-frame bridge instead of `session.frame` envelopes. PCM audio (TTS, PTT)
is the canonical case — see `packages/services/tts/backend/hub_adapter.py`
and `packages/services/ptt/backend/hub_adapter.py` for worked examples.

### Composing components into a page

Just `import` from `@awm/<name>` in the page's `src/`. The generator's next
run scans for it and adds the dep to the page's `package.json`. Vite +
`@sveltejs/vite-plugin-svelte` compiles the source in at build time.

```svelte
<!-- packages/pages/dashboard/src/App.svelte -->
<script>
  import { Button, Card } from '@awm/primitives';
  import '@awm/primitives/style.css';
  import { apiFetch, svc, AuthError } from '@awm/client';
</script>
```

Global CSS doesn't ride the component barrel — `import '@awm/primitives/style.css'`
explicitly. The generator picks up subpath imports too.

### Talking to the hub from a page — `@awm/client`

Pages and components never hand-roll `fetch` against `/rooms`, `/auth/*`,
or `/svc/<name>/…`. Use `@awm/client`:

- `apiFetch(path, init?)` — sets `credentials: 'include'`, attaches the
  `X-Awm-As: user:<n>` identity header derived from the `awm_as`
  cookie, JSON-encodes object bodies, and turns non-2xx responses into
  `AuthError` (401/403) or `HttpError` so callers can branch on auth
  failure without regex-parsing the message.
- `svc('tts').fn('listEngines')` / `svc('tts').session('call', {…})` /
  `svc('tts').ws(sessionId)` — wraps the `/svc/<name>/{fn,session}/…`
  surface every service exposes.
- `whoami()` — `GET /auth/whoami`; throws `AuthError` if not signed in.

Skipping `apiFetch` means skipping `X-Awm-As`, which silently
misattributes writes on a multi-operator hub.

### Iterating on a package in a scope

**Use the running `dev` sandbox. Do NOT start your own.** Only the `dev`
scope runs `./dev/run.sh start` — every other scope (`comp-*`, `svc-*`,
`web-*`, etc.) shadows against the already-running hub at
`http://127.0.0.1:7821/`. Spinning up a second sandbox in your own
worktree gives you a hub at a different port with none of `dev`'s seeded
state and is almost never what you want. If `./dev/run.sh status` shows
nothing running, ask the `dev`-scope agent to start it — don't start one
yourself.

To live-test a local change against the running dev sandbox without
evicting the dev copy, push it as a shadow overlay:

```bash
cd /home/tony/agentic_workspace/projects/awm/<scope>   # NOT projects/awm/dev
awm dev shadow services/tts pages/tts
# (lease blocks; Ctrl-C pops the overlay; dev's base traffic resumes instantly)
# Visit http://127.0.0.1:7821/ui/... — same origin as the dev sandbox.
```

For named templates, edit `packages/dev.sh` and call `./packages/dev.sh <name>`:

```bash
./packages/dev.sh agent-stack    # shadow ptt + tts + test-agent in one shot
./packages/dev.sh tts-only       # just tts service + page
```

Shadows of `components/` are an error — components are build-time deps.
Edit the component locally, rebuild the *page* that imports it
(`npm run build -w @awm/<page-name>`), and shadow the page instead.

### Hub-service control plane

Three endpoints frame the service surface:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/hub/service/register` | Body `{name, prefix, pid, start, cwd}`. Returns `{service_id, control_ws_path, bridge_ws_base, auth_token}`. |
| `WS` | `/hub/service/control/{service_id}` | Persistent control channel; closing it evicts the service. |
| `WS` | `/hub/service/bridge/{service_id}/{bridge_id}` | Upstream side of a direct session/emitter bridge; hub byte-relays frames between this and the browser-side WS. |

Envelopes on the control WS:

| `kind` | Direction | Fields | Use |
|---|---|---|---|
| `ready` | service → hub | `{api}` | Handshake; api manifest. |
| `call` | hub → service | `{id, fn, args, as?}` | RPC request expecting a reply. |
| `reply` | service → hub | `{id, ok, result?, error?}` | Reply to a `call`. |
| `notify` | hub → service | `{fn, args, as?}` | Fire-and-forget. |
| `sub` / `unsub` | hub → service | `{topic, sub_id, as?}` | Browser subscriber bookkeeping. |
| `emit` | service → hub | `{topic, payload}` | Event for a non-direct emitter. |
| `session.open` | hub → service | `{session_id, session_kind, init, as?, bridge_id?}` | Browser opened a session. |
| `session.opened` | service → hub | `{session_id, ok, error?}` | Service acknowledges. |
| `session.frame` | both ways | `{session_id, payload}` | One frame on a non-direct session. |
| `session.close` | both ways | `{session_id, code?, reason?}` | Session ended. |

Browser-side, the hub exposes:

| Browser | Hub action |
|---|---|
| `POST /svc/<name>/fn/<fn>` | `call` (or `notify`); reply returned as HTTP JSON |
| `POST /svc/<name>/session/<kind>` | Allocates `session_id`, returns `{ws_path}` |
| `WS /svc/<name>/session/<id>` | Direct → byte relay through bridge; non-direct → `session.frame` envelopes |
| `WS /svc/<name>/emit/<topic>` | Non-direct → JSON fan-out; direct → bridged WS |

### Gotchas

- **Bridge auth inheritance.** The service authenticated by opening the control WS; the bridge WS at `/hub/service/bridge/{sid}/{bid}` inherits that trust and is not re-checked.
- **10s reconnect window.** On hub restart, services have 10 seconds to re-open their control WS. After that the hub `SIGTERM`s the last-known PID (from `<AWM_DIR>/state/services.json`) and respawns from `start_cmd`.
- **Shadow can't displace a component.** Edit the component, rebuild the page that imports it, then shadow the page.
- **AWM_HUB_URL injection.** Python services using `mamba run` inherit env automatically. Node services launched through wrappers may need explicit `--env AWM_HUB_URL=$AWM_HUB_URL`.
- **Generator stale imports.** Adding a `from '@awm/foo'` import without rerunning `awm packages gen` leaves `dependencies` missing the entry; npm install won't symlink it. The CI guard catches this; `./dev/run.sh restart` runs `gen` first so the loop self-heals.

See `AGENTS.md` for **awm-internal** architecture — the registry overlay, supervisor PID journal, `rpc.py` envelope schemas, and how to modify the hub itself.

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

## Destructive operations

`POST /projects` and `DELETE /scopes/{p}/{s}` are 403'd by default. Set `AWM_ALLOW_DESTRUCTIVE=1` in the environment to permit them. No restart needed — re-read each request.

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
  server.py               # FastAPI + uvicorn (loopback listener)
  mcp_server.py           # MCP stdio server
  config.py               # Paths and settings
  db.py                   # SQLite (WAL mode) + migrations
  models.py               # Pydantic models
  services/               # Core service layer (scopes, rooms, …)
.awm/                     # Runtime state (gitignored)
  state.db                # SQLite database
  awm.pid
  awm.log
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
