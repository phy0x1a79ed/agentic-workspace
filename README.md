# Agentic Workspace Manager (AWM)

*Human setup + usage guide for awm. Agents operating in this workspace load [`WORKSPACE.md`](WORKSPACE.md) at session start via the harness's native mechanism (see the `harness-setup` writeup on disk at `.awm/skills/awm/harness-setup.md`) — not this file. **Do not merge this README into AGENTS.md or WORKSPACE.md** — their audience is agents in scope worktrees; this one's audience is humans installing, networking, and operating the system.*

A lightweight Python service + CLI for coordinating multiple AI agents working in parallel on shared resources. Provides project/scope management, a skills catalog, the scope channel (per-scope journal + messages), artifact registration, autonomous agent spawning, and an MCP server for direct tool use by Claude Code / OpenCode / other MCP clients.

For agent-facing structural docs (paths, MCP tools, scope lifecycle), see [`WORKSPACE.md`](WORKSPACE.md). For **awm-internal architecture** — modifying the hub, registry, supervisor, RPC layer, manifest generator — see [`AGENTS.md`](AGENTS.md). This README covers install and the *usage* side of the package model (authoring components / services / pages); AGENTS.md covers the *implementation* side.

## Quick Install

```bash
awm/gateway/setup.sh
```

This creates an `awm` mamba environment, installs the gateway plus every
discovered feature service, initializes runtime state, and adds `awm` and
`awm-mcp` to your PATH.

## Manual Install

```bash
mamba env create -f awm/gateway/environment.yml
bash awm/gateway/install.sh
mkdir -p .awm
mamba run -n awm python -m awm.gateway init
```

`awm/gateway/install.sh` is the single composition root: it installs the
shared component libs (`awm.config`, `awm.persistence`, …), then loops every
`awm/services/*/install.sh` to install each discovered feature service, then
the gateway itself (resolving third-party deps). There is no hardcoded service
list — adding a folder under `awm/services/` is enough. To install or
reinstall a single service on its own, run `bash awm/services/<name>/install.sh`;
that yields a service the gateway can respawn standalone. Override the target
env with `AWM_ENV`.

## Harness Integration

AWM drives **Claude Code** and **OpenCode** as first-class harnesses. The `harness-setup` writeup (on disk at `.awm/skills/awm/harness-setup.md` inside any scope) covers:

- How Claude Code and OpenCode each pick up the 3-tier orientation (workspace `WORKSPACE.md` + repo `AGENTS.md` + scope `.awm/context.md`) — CC via instructions in `~/.claude/CLAUDE.md` that direct the agent to Read each tier; OC via native `AGENTS.md` walk-up plus per-scope `mcp-opencode.json` `instructions` array for the other two.
- The MCP exporter framework that fans `<workspace>/.mcp.json` out to backend-specific configs (`spawn-mcp.json` for claude, `mcp-opencode.json` for opencode) — registered services are advertised even when their upstream is down.
- Per-session harness selection via the `agent_cli` column on `agent_sessions`.
- Healing existing scopes that pre-date the wiring: `awm scope heal`.

## Authoring a service

A **service** is a folder under `awm/services/<name>/` that the gateway can
run — in any language (a Python process, a compiled binary, a thin proxy to a
remote API like OpenRouter). The folder name *is* the service name, and it is
the only contract: drop a folder in and the gateway discovers, installs, and
runs it. There is **no central registry to edit, no `kind` field, and no
auth — ever.**

### What the folder ships

| File | Required? | Purpose |
|---|---|---|
| `run.sh` | **yes**, executable | The *only* thing the gateway ever runs to start the service: `bash run.sh`. Self-contained — it must launch the process with no extra arguments and no inherited assumptions. |
| `INSTALL.md` | **yes** | Human install notes: deps, env, anything an operator needs. |
| `install.sh` | optional | Editable-installs the service's deps and writes the `.runtime-env` sidecar (see below). Picked up automatically by the install loop. |

### The three injected env vars (and *only* these)

The gateway hands a starting service exactly three environment variables.
There is no token, no key file, no bearer — the control plane is loopback and
unauthenticated.

| Var | Purpose |
|---|---|
| `AWM_HUB_URL` | base URL of the running gateway (e.g. `http://127.0.0.1:7819/`) |
| `AWM_SERVICE_NAME` | the service name (= folder name) |
| `AWM_SERVICE_ID` | assigned by the gateway on respawn so a reconnect targets the same control URL |

### Writing `run.sh`

For a **Python service**, branch on the dev signal so the same script works
both from the uninstalled worktree and from an installed env:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${DEV_PYTHONPATH:-}" ]]; then
  # Dev: run the uninstalled worktree code via the env interpreter.
  exec mamba run -n awm --no-capture-output python -m awm.myservice.hub_adapter
else
  # Prod: source the sidecar install.sh wrote, exec the baked interpreter.
  source ./.runtime-env          # sets AWM_PYTHON = the env's python
  exec "${AWM_PYTHON:-python}" -m awm.myservice.hub_adapter
fi
```

A **non-Python service** just execs its binary — same three env vars, no
branch needed:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./bin/myservice          # reads AWM_HUB_URL / AWM_SERVICE_NAME / AWM_SERVICE_ID from env
```

`install.sh` (optional) editable-installs the service and writes the
`.runtime-env` sidecar that bakes `AWM_PYTHON` (the absolute path to the env's
interpreter). That sidecar is what lets `run.sh` exec the right interpreter
under systemd, where `mamba` is not on `PATH`.

### How the gateway picks it up

- **Discovery** — the gateway scans `awm/services/*` for a `run.sh` (a plain
  filesystem scan; no hardcoded name list). Any folder with one is a service.
- **Bootstrap + reconcile** — on boot the gateway reconciles the services it
  has journaled, then **bootstraps** any discovered-but-unjournaled ones. So a
  fresh clone or a wiped `.awm/state/services.json` comes up with every
  *enabled* service running — no manual kick needed.
- **Enable / disable** — state lives in `.awm/services/enabled.json` (a service
  absent from the file is enabled). A disabled service stays down across
  restarts.
- **Respawn durability** — services are journaled with their PID; the control
  WS lease is liveness. If a service dies (or the gateway restarts), the
  supervisor respawns it from its `run.sh`.

### Controlling a service

```bash
awm services list                 # discovered services + enabled/running state
awm services start <name>         # start a stopped service (idempotent)
awm services start --all          # start every enabled service
awm services stop <name>          # evict + kill + drop its journal entry
awm services restart <name>
awm services enable <name>        # clear the disable flag (won't auto-start)
awm services disable <name>       # stop it and keep it down across restarts
awm services reap                 # kill orphaned hub_adapters targeting this hub
                                  #   that hold no live lease (--dry-run lists only)
```

### Iterating with `awm dev shadow`

To live-test local changes without evicting the running bases, bring your
worktree's pages **and** services up against a hub in one command:

```bash
awm dev shadow --port 7821 pages/agent \
  awm/services/agents awm/services/tts awm/services/stt
```

`--port` selects the hub (**default `7821`, the dev sandbox**) — the CLI
otherwise targets `AWM_PORT` (prod `7819`), so always pass `--port` to keep off
prod. Each target comes up in **one foreground process**; a single Ctrl-C tears
the whole stack down (overlays popped, services we spawned SIGTERM'd, dev's bases
resume).

Each target auto-selects base-vs-overlay against what the hub already serves:

- **Service** (a path under `awm/services/`): execs the folder's `run.sh` with
  the dev `PYTHONPATH` so it runs *this* worktree's code. If `/svc/<name>` already
  has a base it registers as an overlay (`AWM_SERVICE_OVERLAY=1`, one process /
  one identity, its control WS as the lease); if not, the adapter self-registers
  as a fresh base. A base created this way is **not journaled** — it won't respawn
  if the hub restarts mid-session. You can pass another worktree's path to shadow
  its copy.
- **Page** (`pages/<name>`): serves `awm/pages/<name>/dist` at `/ui/<name>` —
  overlay if a page base already serves that prefix, else a fresh page base. The
  hub redirects the bare `/ui/<name>` to `/ui/<name>/` so the bundle's relative
  asset refs resolve; you can hand out either URL form.

`--name <override>` renames a single target (single-target invocations only).

Shadows of `ui_components/` are an error — components are build-time deps. Edit
the component locally, rebuild (`npm run build`, from `awm/`), and shadow the
*page* that imports it instead.

### Installing

`bash awm/gateway/install.sh` is the single composition root — it installs the
component libs, loops every discovered `awm/services/*/install.sh`, then the
gateway. To install one service standalone (respawnable on its own), run
`bash awm/services/<name>/install.sh`.

## Authoring a page

The frontend follows the same "just source" model as the Python components: a
shared component is a source folder imported by name, with **no per-unit
`package.json` or `vite.config.ts` and no hand-maintained dependency list.**

```
awm/ui_components/<name>/   ← shared Svelte library; imported as @awm/<name>; no URL
awm/pages/<name>/           ← static bundle; built to dist/ and served at /ui/<name>/
```

### What you author on disk

| Kind | Files you write |
|---|---|
| Component | `src/index.ts` (barrel re-exporting each public component) plus the `.svelte` / `.ts` / `.css` files it ships. That's it. |
| Page | `index.html`, `src/main.ts` (entry; calls `mount(App, …)`), `src/App.svelte` (root component), plus any page-local code under `src/lib/`. Prefix defaults to `/ui/<dirname>`; override it with an optional one-line `prefix.txt` at the page root. |

There is nothing else to write — no manifest, no per-page build config. The
single root `awm/vite.config.ts` resolves `@awm/<name>` (and subpaths like
`@awm/primitives/style.css`) to component source by path alias, and the root
build loop builds every page with it. Third-party deps (svelte, vite, `bits-ui`,
…) are declared once in the root `awm/package.json`, not per component.

### Building a page

Build from the `awm/` directory:

```
npm install        # once per machine — installs the central third-party deps
npm run build      # builds every page → awm/pages/<name>/dist/
```

`npm run build` runs `scripts/build.sh`, which builds each `pages/*/` that has
an `index.html` (source-only placeholder dirs are skipped). To serve a freshly
built page against a running hub, overlay its bundle with `awm dev shadow`:

```bash
awm dev shadow --port 7821 pages/<name>   # serve awm/pages/<name>/dist at /ui/<name>
```

Build first — the shadow (and prod) serve the built `dist/` directory, not the
source. (See `AGENTS.md` § *Frontend component system* for the canonical
build/shadow SOP and how resolution works.)

### Composing components into a page

Just `import` from `@awm/<name>` in the page's `src/` — no dependency entry to
add anywhere. Vite + `@sveltejs/vite-plugin-svelte` resolves the alias and
compiles the source in at build time.

```svelte
<!-- awm/pages/dashboard/src/App.svelte -->
<script>
  import { Button, Card } from '@awm/primitives';
  import '@awm/primitives/style.css';
  import { apiFetch, svc, AuthError } from '@awm/client';
</script>
```

Global CSS doesn't ride the component barrel — `import '@awm/primitives/style.css'`
explicitly. A page bundles only the components it actually imports.

### Talking to the hub from a page — `@awm/client`

Pages and components never hand-roll `fetch` against `/svc/<name>/…`. Use
`@awm/client`:

- `apiFetch(path, init?)` — JSON-encodes object bodies and turns non-2xx
  responses into a typed `HttpError` so callers can branch on failure without
  regex-parsing the message. The hub is loopback and unauthenticated — there
  is no cookie, identity header, or login flow to wire up.
- `svc('tts').fn('listEngines')` / `svc('tts').session('call', {…})` /
  `svc('tts').ws(sessionId)` — wraps the `/svc/<name>/{fn,session}/…`
  surface every service exposes.

### Hub-service control plane

Three endpoints frame the service surface:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/hub/service/register` | Body `{name, prefix, pid, start, cwd}`. Returns `{service_id, control_ws_path, bridge_ws_base}` — no token; the control plane is loopback and unauthenticated. |
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

- **No auth on the bridge.** The control plane is loopback and unauthenticated; the bridge WS at `/hub/service/bridge/{sid}/{bid}` is plain loopback like everything else — no token is checked anywhere.
- **10s reconnect window.** On gateway restart, services have 10 seconds to re-open their control WS. After that the supervisor `SIGTERM`s the last-known PID (from `<AWM_DIR>/state/services.json`) and respawns from the service's `run.sh`.
- **Shadow can't displace a component.** Edit the component, rebuild the page that imports it, then shadow the page.
- **`run.sh` must be self-contained.** The gateway runs `bash run.sh` with only the three injected env vars on a minimal systemd `PATH`. A Python `run.sh` that relies on `mamba` being on `PATH` breaks on respawn — source the `.runtime-env` sidecar and exec the baked `AWM_PYTHON` interpreter (see *Authoring a service*).

See `AGENTS.md` for **awm-internal** architecture — the registry overlay, supervisor PID journal, `rpc.py` envelope schemas, and how to modify the hub itself.

## Server Lifecycle

The server auto-starts when you run any CLI command. To run manually:

```bash
awm gateway serve     # foreground, local-only listener on 127.0.0.1:7819
awm gateway status    # health check (auto-starts if needed)
awm gateway stop      # stop the server
awm gateway restart   # drain services, restart via systemd, wait for healthy
                      #   (synchronous — verifies PID changed + uptime reset)
                      #   HTTP/MCP endpoint (awm_restart) is fire-and-forget
awm gateway refresh   # restart server to pick up source changes (dev mode)
```

The server auto-shuts down after 30 minutes of inactivity (configurable via `AWM_IDLE_SHUTDOWN` env var; set to `0` to disable).

`awm <command> --help` lists every subcommand. Beyond the gateway-control groups, the CLI generates an `awm <domain> <verb>` command for every registered feature-service tool (`awm scope create`, `awm agent list`, …) from the same live catalog the MCP surface reads. Note the surfaces are projected differently from one shared catalog: the **CLI and HTTP** stay fully expanded (one `awm <domain> <verb>` command and one `POST /invoke {name:"<domain>_<verb>"}` route per verb), while the **MCP** surface collapses to one generic `{verb,args}` tool per domain (`GET /tools?view=domains`, with a `describe` verb for parameter schemas) to keep the tool count small for agents. So shell usage is unchanged; only what an MCP client sees is collapsed. For agent-facing usage (scopes, the scope channel, artifacts, skills), see `WORKSPACE.md` — those workflows are typically driven from inside an MCP-equipped agent, not the shell.

### Per-workspace env file

`awm gateway serve` and `awm-mcp` read `$AWM_WORKSPACE/.awm/env` at startup
(if present) and merge its `KEY=VAL` lines into the process env before
doing anything else. Use it to plumb things into the daemon that
systemd doesn't forward by default — the canonical case is
`SSH_AUTH_SOCK` so `project_create --clone git@github.com:…` reaches
your user ssh-agent. Format is dumb `KEY=VAL` per line, `#` for
comments, leading `export ` tolerated, single- or double-quoted values
are unquoted. Existing env vars are overridden; missing file is a no-op.

Example `.awm/env`:

```
SSH_AUTH_SOCK=/run/user/1000/keyring/ssh
```

Restart the daemon (`sudo systemctl restart awm.service` or
`systemctl --user restart awm.service`, depending on which unit is
live on your host) to pick up changes.

## Project data — versioning and concurrency

Project data lives at `<workspace>/data/<project>/` and every scope reaches it
at `.awm/data/`. Both of those paths are permanent; what changed is what sits
behind them.

By default `.awm/data` is a symlink and every scope in a project writes the same
files — no history, no isolation, and two scopes writing the same path destroy
each other's work. Converting a project's data to **git-annex** gives it the
same model code already has from worktrees:

```bash
awm project data-init <project> --dry-run   # preview: file count, vendored checkouts found
awm project data-init <project>             # convert (refuses while any scope is active)
awm scope heal --project <project>          # give existing scopes their clones
```

After conversion each scope's `.awm/data` is a clone on its own branch
`scope/<scope>`, and `<workspace>/data/<project>/` stays a normal checked-out
working tree on `main` — so every absolute path and symlink that pointed there
still resolves.

```bash
awm scope data-status  <project> <scope>    # mode, branch, revision, drift, dirt
awm scope data-snapshot <project> <scope>   # commit what this scope wrote
awm scope data-promote  <project> <scope>   # publish into the project's canonical branch
awm scope gather <project> <hub> --peripherals a --peripherals b --data
```

`data-promote` is all-or-nothing. It snapshots, publishes content and the
location log, reconciles against the canonical branch, then **fast-forwards**
it — so two scopes promoting at once produce one winner and one clean
rejection, never a clobber. A content conflict is reported as a conflict and
rolls back to the exact pre-merge revision; nothing is auto-merged into
`file.variant-<key>` behind your back.

Operational notes:

- **Storage.** Content is hardlinked from the canonical store, so N scopes
  holding the same dataset cost one copy. This requires `data/` and `projects/`
  on the same filesystem; if they aren't, git-annex silently falls back to real
  copies.
- **Read-only files.** Anything above ~100 KB becomes a symlink into the content
  store and is not writable in place. Small text stays an ordinary git file.
- **Secrets are excluded, never annexed.** `secrets/` paths, `.env*`,
  `.credentials.json` and `.nextflow/secrets` are gitignored in the canonical
  repo, so they are never committed and never reach the off-site mirror. They
  stay on local disk untouched.
- **Vendored git checkouts are pinned, not annexed.** Conversion finds nested
  repos, excludes their trees, and records URL + commit in `VENDORED.tsv`.
- **git-annex is optional.** It is resolved from `AWM_ANNEX_BIN`, then PATH,
  then the known mamba envs. When it isn't found every path degrades to the
  legacy shared symlink rather than failing. `AWM_DATA_ANNEX=0` forces that
  globally.
- **Off-site backup.** The `dvc` service mirrors the whole workspace to the
  chinook Globus collection daily (`awm dvc mirror`). It runs with
  `delete_destination_extra`, so it is **disaster recovery, not an archive** — a
  local deletion propagates on the next run. DVC cache-checkouts are excluded as
  rebuildable, which is only true because `data/.dvc_cache` itself is *not*
  excluded; keep that pairing intact. `awm dvc pull --scope <s>` is the selective
  inverse — it resolves one scope's `.dvc` pins to their objects and fetches back
  only those, since the mirror alone offers no way to restore less than
  everything. See `awm/services/dvc/INSTALL.md`.
  (`awm/gateway/scripts/data-backup.sh` is the retired git-annex-era predecessor
  and points at an endpoint that no longer exists.)

## Destructive operations

`POST /projects` and `DELETE /scopes/{p}/{s}` are 403'd by default. Set `AWM_ALLOW_DESTRUCTIVE=1` in the environment to permit them. No restart needed — re-read each request.

## Architecture

awm is a modular **gateway** plus a set of out-of-process **feature services**.
The gateway is the sole interface (CLI, HTTP, MCP stdio) and the coordination
hub; it owns no tables of its own. Each feature service is its own pip dist
with its own SQLite DB, discovered and run from a folder under `awm/services/`.
Shared component libs (`awm.config`, `awm.persistence`, the gateway client) are
imported source.

```
        ┌─────────┐  ┌──────────┐  ┌───────────┐
        │ Typer   │  │ FastAPI  │  │ MCP stdio │
        │ CLI     │  │ HTTP     │  │ Server    │
        └────┬────┘  └────┬─────┘  └─────┬─────┘
             │            │              │
             └────────────┼──────────────┘
                          │
                 ┌────────▼─────────┐
                 │  awm.gateway     │   sole interface + coordination hub
                 │  (de-DB'd)       │   discovers + supervises services
                 └────────┬─────────┘
                          │  register / ready / call / emit (loopback, no auth)
        ┌─────────────────┼──────────────────┐
        │                 │                  │
   ┌────▼─────┐     ┌──────▼──────┐    ┌──────▼──────┐
   │ scopes   │     │ agents      │    │ artifacts / │   …each an out-of-proc
   │ service  │     │ service     │    │ skills /    │    feature service with
   │ (+own DB)│     │ (+own DB)   │    │ discord     │    its own DB + run.sh
   └──────────┘     └─────────────┘    └─────────────┘
```

```
awm/                          # nested tree of pip dists (PEP 420 namespace layout)
  gateway/                    # the gateway dist (package awm.gateway)
    install.sh                # composition root: installs components + every service + gateway
    setup.sh  environment.yml # env bootstrap
    deploy/awm.service        # systemd unit
    scripts/run-tests.sh      # runs each dist's own tests/
    awm/gateway/              # the namespace package (the nesting is intentional)
      server.py               # FastAPI app: awm.gateway.server:app
      cli.py                  # Typer CLI
      hub/discovery.py        # filesystem scan of awm/services/* for run.sh
  service_components/         # shared Python imported source (no install.sh)
    config/  persistence/  gatewayclient/  agentcore/
  services/                   # one folder per feature service (discovered)
    scopes/  agents/  artifacts/  skills/  discord/
      run.sh                  # the only entry the gateway runs (bash run.sh)
      INSTALL.md  install.sh
  ui_components/<name>/       # shared Svelte libraries, imported as @awm/<name>
  pages/<name>/               # static front-end bundles built to dist/, served at /ui/<name>/
  vite.config.ts              # one root config: @awm/* alias + per-page build
  scripts/build.sh            # build loop: each page → pages/<name>/dist/
  package.json                # central third-party frontend deps + `npm run build`
.awm/                         # runtime state (gitignored)
  services/<svc>/<svc>.db     # per-service SQLite DBs
  services/enabled.json       # per-service enable/disable state
  state/services.json         # PID journal for respawn
  awm.pid  awm.log
  spawn-mcp.json / mcp-opencode.json   # MCP exporter fan-out
.mcp.json                     # MCP server registration
```

For the awm-internal architecture in detail (registry, supervisor, RPC
envelope layer, manifest generator), see [`AGENTS.md`](AGENTS.md).

## Troubleshooting

**Port in use**: `awm gateway stop` then retry, or `lsof -i :7819` to find the process.

**Server won't start**: Check `.awm/awm.log` for errors. Ensure port 7819 is free.

**Database issues**: Each feature service owns its own DB under `.awm/services/<svc>/<svc>.db`. Delete the offending one and let the service re-seed on next boot, or re-run `awm/gateway/install.sh` and `awm.gateway init`.

**Services stuck at "down"**: `awm services list` shows each service's enabled/running state. `awm services start --all` brings up every enabled service; a fresh clone or wiped `.awm/state/services.json` self-heals on the next gateway boot (reconcile-then-bootstrap).

**MCP not connecting**: Verify `.mcp.json` exists at workspace root. Check that `awm-mcp` is on PATH (`mamba run -n awm which awm-mcp`). Restart Claude Code to pick up changes.

## What goes in this file

README.md is the human-facing setup + usage guide for awm: how to install it, wire up harness integration, and the *usage* side of the package model — the day-to-day workflows for authoring a service, a page, or a component, controlling and shadowing services, talking to the hub from a page, and operating the server. Agent-facing structural orientation goes in `WORKSPACE.md`; awm-internal architecture and implementation detail go in `AGENTS.md`.
