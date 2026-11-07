# Agentic Workspace Manager (AWM)

## Purpose & Contents

This is the human guide to awm: how to install it, wire up Claude Code, author a service or a page, run the gateway, and keep project data. Agents load [`AGENTS.md`](AGENTS.md) at session start, not this file. Do not merge this README into `AGENTS.md`, `PROTOCOLS.md` or `ARCHITECTURE.md`, because their audience is agents.

- [`AGENTS.md`](AGENTS.md) — agent orientation: paths, MCP tools, startup.
- [`PROTOCOLS.md`](PROTOCOLS.md) — workspace procedures: scope lifecycle, naming, hubs, data, backups.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — awm internals: the gateway, the catalog, the gates, the supervisor.
- [`FEDERATION.md`](FEDERATION.md) — everything that crosses nodes, and the deploy.

awm coordinates AI agents that work in parallel on shared resources. It provides project and scope management, the journal (per-scope posts, briefs, precedents and history), one ranked search over what the swarm knows, background Claude Code sessions (`reflection start_other`), calls between nodes over ssh edges, and one MCP server for Claude Code and other MCP clients.

## Overview

awm is one Rust binary, `awm`, plus a set of out-of-process plugins. The binary is the gateway: it serves the CLI, HTTP and MCP, and loads, starts and supervises the plugins. Each plugin is a folder with an `awm-plugin.toml`, its own services and its own SQLite DB. Shared Python libraries live in `awm/service_components/`.

```
   awm <tool> <verb>     POST /invoke      awm mcp (stdio)
            \                 |                 /
             +----------------+----------------+
                              |
                 +------------v-------------+
                 |  awm gateway serve       |   the only gateway, owns no tables
                 |  (core/crates)           |   loads, starts, supervises plugins
                 +------------+-------------+
                              |   register / ready / call / emit, with a service token
          +-------------------+--------------------+
          |                   |                    |
     +----v-----+       +-----v-----+       +------v------+
     | scopes   |       | cch       |       | journal     |   ...each a plugin with
     | (own DB) |       | (own DB)  |       | (own DB)    |   its own DB and run.sh
     +----------+       +-----------+       +-------------+
```

## Install

```bash
awm/setup.sh
systemctl --user enable --now awm
```

`setup.sh` creates or updates the `awm` mamba env, builds the static core binary in the Rust container (`scripts/rust-build.sh`), and runs `awm/install.sh`. That installs the shared libraries and runs every plugin's own `install.sh`, then places the binary at `~/.local/bin/awm`. `setup.sh` then writes `.awm/config.toml` with `awm bootstrap` and installs the `awm` user unit from `core/deploy/awm.service`. Set `AWM_BIN` to a built binary to skip the build.

awm needs no sudo. The unit and the workspace `.mcp.json` name `~/.local/bin/awm` by absolute path.

`setup.sh` refuses to run when `AWM_WORKSPACE` names a different checkout, so a second install cannot land in a home directory by accident. The cloud project installs awm on Tony's nodes with an install config of its own (`awm bootstrap --config`). It holds every host-specific step, because awm names no host.

After the first install, `awm deploy` brings a node in line with its checkout (`FEDERATION.md` § *Deploy*).

## Harness Integration

AWM drives Claude Code. A session loads three context tiers: the workspace `AGENTS.md`, the repo `AGENTS.md` at the scope worktree root, and the scope brief. Claude Code loads both `AGENTS.md` tiers by its native walk-up from the cwd. The brief lives in the journal (`journal brief_read`). cch's SessionStart hook adds it to a session that starts in a scope, and `reflection cd` carries it in the move's follow-up (`docs/redux/scopes.md` R143).

`<workspace>/.mcp.json` registers the `awm` MCP server, which runs `awm mcp`.

### Set up Claude Code

1. Put the `claude` CLI on `PATH`.
2. Run `awm/scripts/deploy-hooks.sh`. It writes awm's hooks into `~/.claude/settings.json`, the brief hook among them.

### Verify the setup

```bash
jq '.hooks.SessionStart' ~/.claude/settings.json   # expect awm-brief-hook.py
cd <scope> && claude -p "List the AGENTS.md files loaded into your context, by absolute path."
```

Run `awm scope heal` to bring scopes that predate this wiring to the current shape. Add `--dry-run` to preview the changes.

## Authoring a service

A **plugin** is a folder under `awm/services/` or `awm/plugins/` with an `awm-plugin.toml`. Each service it declares runs as its own process, in any language: a Python process, a compiled binary, a thin proxy to a remote API. Drop a folder in and the gateway loads, installs and runs it. There is no central registry to edit.

### What the folder ships

| File | Required? | Purpose |
|---|---|---|
| `awm-plugin.toml` | **yes** | The manifest: its services, the tools it publishes with major and tier (`on` or `off`), its pages, its `[needs]`, and `[[service.archive]]` tables that plug it into `search`. Every function a service registers is published as `<tool>_<verb>`. The format is `docs/redux/drafts/plugin-contract.md`. |
| `run.sh` | **yes**, executable | The only thing the gateway runs to start the service: `bash run.sh`. It must launch the process with no arguments. |
| `INSTALL.md` | **yes** | Install notes: deps, env, anything an operator needs. |
| `install.sh` | optional | Editable-installs the service's deps and writes the `.runtime-env` sidecar. `awm/install.sh` runs it. |

### What the gateway hands a service

The gateway starts `run.sh` with a cleared environment. It passes the install config's `[env]`, the secrets the manifest declares, and these:

| Var | Purpose |
|---|---|
| `AWM_HUB_URL` | base URL of the running gateway, such as `http://127.0.0.1:7819/` |
| `AWM_SERVICE_NAME` | the service name |
| `AWM_PLUGIN` | the plugin name |
| `AWM_HUB_TOKEN_FILE` | the service token for this spawn. Send it as `X-Awm-Service-Token` |
| `AWM_DIR`, `AWM_WORKSPACE`, `AWM_NODE_NAME`, `AWM_PORT` | where state and the workspace live, and which node this is |

The plugin's egress proxy rides in `HTTP_PROXY` and `HTTPS_PROXY`, and passes only the hosts `needs.network` lists. A variable the gateway does not pass is absent, so read everything else from `[env]`.

### Writing `run.sh`

For a **Python service**, exec the interpreter `AWM_PYTHON` names. `install.sh` writes it to `.runtime-env`, and the dev sandbox sets it, with `PYTHONPATH`, in its config's `[env]`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./.runtime-env ] && . ./.runtime-env      # sets AWM_PYTHON, the env's interpreter
exec "${AWM_PYTHON:-python}" -m awm.myservice.hub_adapter
```

`hub_adapter.py` builds on `awm.gatewayclient.ServiceAdapter`, which registers, sends `ready`, dispatches calls and reconnects. `awm/services/dev/` is the smallest complete example to copy.

A **non-Python service** execs its binary. The binary reads the variables above, registers at `POST /hub/service/register` with its token, opens the control WebSocket the reply names and sends `ready` with its API manifest.

**CAUTION** `run.sh` must be self-contained. The gateway runs it on a minimal `PATH` with no `mamba`. A Python `run.sh` sources the `.runtime-env` sidecar and execs the interpreter it names.

### Controlling a service

```bash
awm services list                 # each service: status, pid, enabled, tools
awm services start <name>         # start one, and clear its crash-loop breaker
awm services stop <name>
awm services restart <name>
awm services enable <name>        # writes .awm/enabled.json, then starts it
awm services disable <name>       # stops it and keeps it down across restarts
awm plugins                       # what loaded, what was refused and why
awm reload [--restart a,b]        # start added plugins, stop removed ones; restart only the named
awm restart <plugin> [--force]    # restart one plugin through the reload path
awm listen <service> <topic>      # print each payload a service emits
```

A service absent from `.awm/enabled.json` follows its profile. Keep the file explicit on a node, so a new plugin's services do not start by surprise.

### Iterating in a dev sandbox

`awm/services/dev/sandbox/run.sh` runs your worktree's gateway on its own port with its own state, so you test a change without touching prod. `ARCHITECTURE.md` § *Dev sandboxes* lists the ports. Point the CLI at it with `AWM_GATEWAY_URL=http://127.0.0.1:<port>`.

## Authoring a page

A page lives in its plugin, at `<plugin>/pages/<name>/`, and the manifest's `[[page]]` names it. The gateway serves its built `dist/` at `/p/<plugin>/<name>/`. A shared component is a source folder imported by name, with no per-unit `package.json` or `vite.config.ts`.

```
awm/ui_components/<name>/          shared Svelte library, imported as @awm/<name>
<plugin>/pages/<name>/             a page, built to dist/ and served at /p/<plugin>/<name>/
```

| Kind | Files you write |
|---|---|
| Component | `src/index.ts`, a barrel that re-exports each public component, plus the `.svelte`, `.ts` and `.css` files it ships. |
| Page | `index.html`, `src/main.ts` (the entry, which calls `mount(App, …)`), `src/App.svelte`, plus any page-local code under `src/lib/`. |

The single root `awm/vite.config.ts` resolves `@awm/<name>` and subpaths such as `@awm/primitives/style.css` to component source. Third-party deps such as svelte, vite and `bits-ui` go once in the root `awm/package.json`.

### Building a page

```bash
cd awm
npm install        # once per machine
npm run build      # builds every page
```

`npm run build` runs `awm/scripts/build.sh`, which builds each page that has an `index.html`. The gateway serves the built `dist/`, never the source, so build before you look. A page with no `dist/` is not served. A deploy builds nothing on a node: the deploy on mizar rebuilds a stale pages pin (`scripts/refresh-pins.sh`), and `scripts/awm-deploy.sh` unpacks the pages.

### Talking to the gateway from a page

Use `@awm/client`. Never hand-roll a `fetch`.

- `tool('mic').call('status', {...})` posts to `/invoke` and returns the parsed result. A refusal throws an `HttpError`.
- `openSession('mic', 'stream', {...})` opens the WebSocket `/hub/session/mic/stream` and sends the init as its first frame.
- `apiFetch(path, init?)` JSON-encodes object bodies and turns a non-2xx reply into a typed `HttpError`.

A page calls only the tools its plugin's manifest lists in `[[needs.tool]]`. The gateway knows the page's plugin by its `Referer`.

## Running the gateway

The `awm` user unit runs `awm gateway serve --state <workspace>/.awm` on `127.0.0.1:7819`. It logs to `.awm/awm.log`, and each service logs to `.awm/logs/<service>.log`.

```bash
systemctl --user restart awm      # restart the gateway and every service
awm status                        # gateway and service health
```

The gateway reads its configuration from `.awm/config.toml` only. Put an environment variable that the services need, such as `SSH_AUTH_SOCK` for `scope create --clone-url git@github.com:…`, in its `[env]` table, then run `awm reload --restart <plugin>` for each plugin that needs it. `awm reload` reads the changed `[env]` but a running service keeps its old one until it restarts, and the reload result lists these under `stale_env`.

### The CLI

`awm --help` lists the core commands. Any other first word names a tool: `awm <tool> <verb> [--key value]… [--json '{…}'] [--peer <node>]` runs one verb through the same catalog as MCP, hidden verbs included. `awm <tool> --help` lists a tool's verbs, and `awm <tool> <verb> --help` shows one verb's parameters. MCP folds each tool into one `{verb, args, peer}` call, and leaves out the tools whose tier is `off`.

## Project data — versioning and concurrency

Data is versioned by the same commit that versions the code. A project opts in
by being a DVC repo — a tracked `.dvc/config` in its checkout — and from then on:

```
projects/<project>/<scope>/data/<chunk>        the files (hardlinks into the cache)
projects/<project>/<scope>/data/<chunk>.dvc    the pin — TRACKED, ~110 bytes
<workspace>/data/.dvc_cache/                   the bytes, once, for every project
```

There is **one lever**. `dvc add data/<chunk>` writes the pin; committing it
alongside your code records both together. Merging a branch brings its pins with
it and a post-merge hook checks the files out, so a code merge *is* the data
promote. Isolation is branch isolation — there is no data branch, no promote
verb, and no separate `--data` leg on gather/scatter.

```bash
awm dvc scope-status --project <p> --scope <s>       # mode, pinning commit, what is materialised
awm dvc mount --project <p> --scope <s> --chunks ... # what materialises on disk here
awm dvc gc --projects a --projects b                 # reclaim cache space (dry run by default)
```

awm wires; DVC operates. Wiring is the absolute cache path in the untracked
`.dvc/config.local`, `cache.type = hardlink,symlink` in the tracked config, the
`dvc` merge driver and the post-merge/post-commit hooks in the common git dir,
and the per-scope mount list. Everything else is `dvc add` / `dvc checkout` /
`git commit` / `git merge`, unwrapped.

Operational notes:

- **Storage.** One physical copy per machine. `data/` and `projects/` must be on
  the same filesystem or the hardlinks degrade to real copies.
- **Materialised files are read-only** — they share an inode with the cache
  object every other scope and every historical commit reads through. Write a new
  file, or `dvc unprotect <path>` first. Never `chmod +w` one.
- **Never run a bare `dvc gc`.** It collects against one worktree's view of a
  cache the whole workspace shares. `awm dvc gc` makes you name every
  project to keep, defaults to dry-run, and refuses `all-branches`.
- **A project without a tracked `.dvc/config` gets no data wiring.** Its scopes
  report mode `none`. A project is wired the first time a scope is created or
  healed after its checkout carries a tracked `.dvc/config`.
- **dvc is optional.** It is resolved from `AWM_DVC_BIN`, then PATH, then the
  known mamba envs. Absent, every scope reports mode `none` with the reason,
  rather than failing. `AWM_DATA_DVC=0` provisions no project's data.

## Off-site backup

Chinook is the remote for the cache the way GitHub is the remote for the code.
A daily timer runs `awm-dvc-sync`, which pushes `data/.dvc_cache` to the chinook
Globus collection and blocks until the transfer reaches a terminal state. It is
**append-only**: nothing this service does deletes on the remote, which is what
makes a local `dvc gc` recoverable rather than permanent.

`awm dvc pull --scope <path>` is the selective inverse — it resolves one scope's
pins to their exact objects and fetches only those, then `dvc checkout`
materialises them. See `awm/services/dvc/INSTALL.md`.

**What is not backed up.** Only the cache travels. Code is covered by GitHub;
anything in a worktree that is neither committed-and-pushed nor DVC-pinned —
scratch directories, run outputs, `.awm/` service state — is covered by nothing,
deliberately. `awm dvc coverage` lists exactly what that is, per scope, so the
decision stays visible.

## Layout

```
core/                         # the Rust core: cargo workspace of crates, built into ~/.local/bin/awm
  deploy/awm.service          # the user unit
awm/
  install.sh  setup.sh  environment.yml
  service_components/         # shared Python libraries (config, persistence, gatewayclient, ...)
  services/<name>/            # plugins: awm-plugin.toml, run.sh, INSTALL.md, pages/
  plugins/<name>/             # plugins, same shape
  ui_components/<name>/       # shared Svelte libraries, imported as @awm/<name>
  vite.config.ts  package.json  scripts/build.sh
scripts/                      # run-tests.sh, rust-build.sh, refresh-pins.sh, release-core.sh, release-pages.sh, awm-deploy.sh, refresh-checkout.sh
test/                         # contract/ (manifest and call checks), install/ (container install)
.awm/                         # node state, gitignored
  config.toml                 # the gateway's configuration
  enabled.json                # per-service enable state
  services/<svc>/<svc>.db     # per-service SQLite DBs
  logs/  usage/  tokens/
.mcp.json                     # MCP server registration
```

## Troubleshooting

**Port in use**: `systemctl --user stop awm`, then `lsof -i :7819` to find the process that holds it.

**Gateway won't start**: Read `.awm/awm.log`. Run `awm plugins` once it is up, for any plugin the loader refused.

**A service is down**: `awm services list` shows its status. A `failed` service tripped its crash-loop breaker. Read `.awm/logs/<service>.log`, fix the cause, then run `awm services start <name>`.

**Database issues**: Each plugin owns its DB under `.awm/services/<svc>/<svc>.db`. Delete the offending one and restart the service, which seeds it again.

**MCP not connecting**: Check that `.mcp.json` at the workspace root runs `~/.local/bin/awm mcp`, and that the binary exists. Restart Claude Code to pick up a change.
