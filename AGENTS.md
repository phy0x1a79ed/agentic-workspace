# AWM Internal Architecture

*Internal architecture reference for agents working ON awm itself: Service Hub protocol, package model, Python/env conventions. Auto-injected only when the agent's cwd contains this file at its root — `projects/awm/*` scopes inherit it via .bare-worktree sharing; other projects' agents never see it. **Do not merge into WORKSPACE.md** — that file is universal, this one is awm-private; keeping them separate is what keeps non-awm agents' contexts uncluttered.*

For workspace structure (paths, MCP tools, project map, scope lifecycle) see `WORKSPACE.md` (auto-injected before this file). This file assumes you're modifying awm itself.

## Modular tree migration (in progress)

awm is being split from the monolithic `awm/` package into a nested tree of pip dists under `awm/`: the **gateway** (`awm/gateway/`, package `awm.gateway`) — the sole interface + coordination hub — plus shared components (`awm.config`, `awm.persistence`) and self-contained feature services (`awm.scopes`, `awm.agents`, `awm.artifacts`, `awm.skills`, `awm.discord`). As of this checkpoint the **gateway boots standalone with an empty feature catalog**; feature surfaces (scopes/rooms/artifacts/sessions/…) return only as those modules migrate and register, not because they're baked into the gateway.

- **Install/iterate each module via its own `install.sh`, never hand-rolled `pip`.** `awm/gateway/install.sh` is the composition root — it installs `config` + `persistence` and every feature dist `--no-deps`, then the gateway itself (resolving third-party deps). Components (`config`, `persistence`, `ui_components`) are pure imported source and have no `install.sh`. Override the target env with `AWM_ENV`.
- **Registration contract.** How a service declares its API (the serializable `ready.api` manifest — `functions`/`emitters`/`subscriptions`), how that threads into the MCP/CLI/HTTP generation layer (manifest → `Operation` → the unchanged `operations.py` compiler; dispatch is catalog-owned), and the **hub-mediated** service↔service comms model (`call` request/reply, `emit`/`sub` pub/sub, `Bridge` streaming — all identity-aware via `as_`, never direct sockets) are documented in `awm/gateway/awm/gateway/catalog.py`. The catalog is the single live source the gateway renders to all three surfaces; `/tools` and `/invoke` read from it.
- **Concurrency.** One server loop owns all async hub state; blocking work is offloaded (`run_in_threadpool` / `run_in_executor`); `asyncio.run()` is banned inside the daemon (survives only in the CLI/MCP-proxy/entrypoint processes). `catalog.dispatch` is async, `/tools` is sync.

## Federation: retired

Federation is gone — see git history for the deletion (commits S1–S5, S6, S8 on `dev` around 2026-06-04). No `/peer/*` routes, no `awm peer *` CLI, no `awm-exposed.service`, no auth layer, no `peers`/`peer_sync_state` tables. The local listener (`awm.service` on `127.0.0.1:7819`) is the only listener. Single host, no auth, plain HTTP loopback.

## Service Hub

`awm.server:app` is a routing layer. Most requests are served by its in-process routers (`/rooms`, `/hub`, …). A few path prefixes are *registered* at runtime; matched requests dispatch to one of four kinds:

| Kind | Surface | Typical caller |
|---|---|---|
| `page` | static bundle at `/ui/<name>` | `packages/pages/*` via `awm packages sync` |
| `service` | RPC-over-WS at `/svc/<name>` | `packages/services/*` via `awm packages sync` |
| `url` | HTTP/WS proxy at any prefix | external services registered via `awm hub register --url ...` |
| `static` | static bundle at any prefix | external bundles registered via `awm hub register --dir ...` |

Each prefix maps to a stack of records (base + optional overlays). Overlays are pushed via `POST /hub/shadow/register` (the `awm dev shadow` CLI); the topmost overlay receives traffic, with base traffic resuming instantly when an overlay's lease closes.

### Post-implementation summary (this scope)

This scope migrates the package model from one ambiguous `kind="stripe"` to three structurally distinct kinds:

- **Folder taxonomy.** `packages/{components,services,pages,_shared}/` — see § Developing a package.
- **Layout-driven generator.** `awm packages gen <repo_root>` writes per-package `package.json` and per-page `vite.config.ts` from the folder layout + a regex scan of `src/` for `@awm/<x>` imports. Lives at `awm/services/packages/gen.py`. Generated files are committed; CI gates on `git diff --quiet` after a fresh run.
- **Service RPC layer.** `awm/services/hub/rpc.py` holds the in-memory `ControlChannel` table keyed by `service_id`. Three endpoint families: `POST /hub/service/register`, `WS /hub/service/control/{sid}`, `WS /hub/service/bridge/{sid}/{bid}`. JSON envelopes on the control WS (call/reply/notify/sub/unsub/emit/session.*); direct sessions/emitters get a raw-frame bridge so PCM audio (TTS, PTT) byte-relays without JSON wrapping.
- **PID journal + 10s reconnect.** Service registrations are journaled to `<AWM_DIR>/state/services.json`. On hub boot, each service has 10 seconds to re-open its control WS; silent ones get `SIGTERM` on their last-known PID + a respawn from `start_cmd`.
- **Shadow overlays.** Each registered prefix is a stack; `awm dev shadow <pkg>...` pushes overlays from a scope worktree against a running hub. Lease close pops the overlay; base traffic resumes with no respawn.
- **Migration.** `packages/components/primitives/` (from old `packages/primitives`), `packages/pages/{primitives-gallery,tts,ptt,agent}/`, `packages/services/{tts,ptt}/`. The pre-redesign legacy packages (`bus`, `dev-shell`, `hello`, the old `kind=stripe` `ptt`/`tts`/`agent`, and the old `chat-primitives` library) were deleted outright in the `agent-harness` scope — no holding pen.

### Hub origin = `awm.server` port

There's only ever one hub origin per node — the `awm.server` process. Which port that is depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd-managed) | `7819` | `awm.service` on the host |
| Dev sandbox: `projects/awm/dev/` | `7821` | `./dev/run.sh start` |
| Dev sandbox: `projects/awm/web-ui/` | `7831` | same |
| Dev sandbox: `projects/awm/web-backend/` | `7841` | same |
| Dev sandbox: any other scope (fallback) | `7851` | same |

Per-scope port bands are set by `dev/run.sh` from the worktree dirname, so dev sandboxes run side-by-side with prod (and each other) on distinct ports.

**Substitute your sandbox port** whenever this section says `:7819`.

### When to make a `svc-*` scope

A `svc-*` scope is for cross-cutting work on a single backend service — the service itself lives in `packages/services/<name>/` regardless of which scope is editing it. Similarly, a `comp-*` scope is for cross-cutting work on a single shared component in `packages/components/<name>/`. Day-to-day, see § *Developing a package* below — that is the authoritative workflow doc and supersedes the older `kind=url` / `kind=static` flows documented in § *Stripe-presentation protocol*.

### Stripe-presentation protocol

A vertical stripe typically combines a `svc-*` (backend, `kind=url`) and a `comp-*` (static bundle, `kind=static`) registered through the same hub origin. End-user view:

```
http://127.0.0.1:7819/x/whatever   ← backend routes (forwarded to svc-X process)
http://127.0.0.1:7819/comp-x/      ← static bundle (served from disk)
```

Same scheme, same host, same port. The svc-X port is plumbing the hub knows about; browsers never see it.

#### One-time per node

```bash
./dev/run.sh start              # bring up the dev hub (or skip if prod awm.service is already up)
export AWM_WORKSPACE=$PWD/dev   # so the awm CLI uses this sandbox's .awm/
```

No auth bootstrap: federation is gone, the listener is loopback-only, no bearer or cookie is required.

#### svc-X (kind=url)

1. **Build the FastAPI service.** Mount your routes under the claimed prefix (`APIRouter(prefix="/x")`) — the hub forwards the path verbatim, so the same path it gets must land on your routes. No auth deps to wire up; the hub forwards on loopback without injecting any headers.

2. **Run on a free port** (any port that isn't the hub or another service):

   ```bash
   python -m svc_x --port 9101 --prefix /x
   ```

3. **Register + hold the lease** (separate terminal; blocks until Ctrl-C):

   ```bash
   awm hub register --name svc-x --prefix /x --url http://127.0.0.1:9101
   ```

   The hub now forwards HTTP+WS at `/x/*` to your process. Ctrl-C closes the lease WS → eviction on the next event-loop tick. Re-running re-registers from scratch.

#### comp-X (kind=static)

1. **Build the bundle** to a directory (`vite build` → `./dist`, or hand-roll a `main.js` + `style.css` like `awm/demos/static_demo/`).

2. **Register + hold the lease**:

   ```bash
   awm hub register \
     --name comp-x --prefix /comp-x --dir ./dist \
     [--entry main.js] [--css style.css] [--mount-id app]
   ```

   - If `./dist` has an `index.html`, the hub serves it verbatim.
   - **Canonical paths only.** `kind=static` serves what is on disk: either the file at the exact path, or a directory's `index.html` (the universal static-server default). No `Accept`-conditional fallback, no SPA shell synthesis, no extension sniffing — a miss is a 404. For deep-link refresh in a SvelteKit / React Router bundle, prerender every route so each URL has a real `index.html` on disk (`prerender = true` and `trailingSlash = 'always'` in the layout, so SvelteKit emits `<route>/index.html`; runtime state in query params, not path segments). For routing the server can't enumerate at build time, register the upstream as `kind=url` and let it answer.
   - If `./dist` has no `index.html`, the hub renders an ESM auto-shell at the prefix root only: empty `<div id="<mount-id>"></div>`, `<link>` for each `--css`, `<script type="module" src=".../<entry>">`. Subpaths under that root still 404 unless they exist on disk.
   - WS connections to a static prefix are closed (code 1003).
   - No second-hop auth — bytes are served by the hub directly.

   Same Ctrl-C-evicts lease model as kind=url.

#### Both together

A live stripe usually has three foreground processes — the service, its lease, and the component's lease:

| Term | Command | Purpose |
|---|---|---|
| 1 | `./dev/run.sh start` (or running) | the hub |
| 2 | `python -m svc_x --port 9101 --prefix /x` | svc-X process |
| 3 | `awm hub register --name svc-x --prefix /x --url http://127.0.0.1:9101` | URL lease |
| 4 | `awm hub register --name comp-x --prefix /comp-x --dir ./dist --entry main.js` | static lease |

Verify: `awm hub list` (shows both, with `kind: url`/`kind: static` and `lease_held: true`). Hit the routes through the hub origin.

### Hub control plane

All four lease/registration commands POST/DELETE/WS to `/hub/*` on the hub origin. Useful as the raw API when scripting outside the CLI:

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/hub/register` | Register a service; returns `service_id` + `lease_ws_path` |
| `WS`     | `/hub/lease/{service_id}` | Hold lease; disconnect → eviction |
| `GET`    | `/hub/services` | List registrations + lease state |
| `DELETE` | `/hub/services/{name}` | Force-evict by name |

All four are unauthenticated — `awm.server` binds loopback only and has no auth layer.

### What `comp-*` and page stripes need to know about consuming

**Nothing.** The hub IS `awm.server:app`. Same origin, no new port. With an empty registry the behavior is byte-identical to a hub-less awm. A page stripe can `fetch('/x/whatever')` without CORS, cookies, or a second port.

### Demos

- `awm/demos/static_demo/` — naked `main.js` + `style.css` bundle; copy as the starting point for a `comp-*` registration.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix per stripe. `/hub` and `/hub/*` are reserved (the lease socket has to stay reachable).
- **One lease holder per service_id.** Re-registering while a lease is held returns 409 — Ctrl-C the old one or `awm hub deregister <name>` first.
- **`AWM_WORKSPACE` matters — for the CLI and for every svc process.** Without it, the CLI hits the global discovery and may target prod `:7819` instead of your sandbox. `./dev/run.sh` exports it for its own children; if you shell out separately, export it yourself. If a svc is already running, `tr '\0' '\n' </proc/<pid>/environ | grep AWM_WORKSPACE` tells you which hub it was set up against; register it there rather than spinning a parallel sandbox.
- **Vite dev server vs static dir.** During hot-reload iteration, register `--url http://127.0.0.1:<vite-port>` instead of `--dir` — point at the dev server directly. Switch to `--dir ./dist` once you're past the rebuild loop.
- **Never run two `awm.server` on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are explicitly supported and how dev parallelism works.

## Developing a package — see `README.md`

The day-to-day "how do I ship a component / page / service" workflow lives in
[`README.md`](README.md) § *Developing a package*. That doc covers the folder
taxonomy, what you author on disk, the `awm packages gen → npm install → build`
pipeline, the `start.sh` + control-WS handshake services do, the
`awm dev shadow` flow, the hub-service control-plane envelopes, and gotchas.

This file (AGENTS.md) is for **internal** awm work — modifying the hub, the
registry, the supervisor, the RPC envelope layer, the manifest generator,
etc. Where README.md tells you how to use the package system, the rest of
this file tells you where the implementation lives:

- **Registry overlay + kinds** — `awm/services/hub/registry.py` (one `_stacks` dict per prefix; base + overlays LIFO; `kind` Literal = `url` | `static` | `page` | `service`).
- **RPC layer** — `awm/services/hub/rpc.py` (in-memory `ControlChannel` per service, `_pending` call table, subscriber registry, session table, bridge id allocator).
- **Service translator + bridge** — `awm/services/hub/proxy.py::proxy_service_http` / `open_session_via_http` / `proxy_session_ws` / `proxy_service_emit_ws`.
- **Supervisor + PID journal** — `awm/services/hub/supervisor.py::reconcile_journaled_services` / `spawn_service` / `kill_pid_group`; state at `<AWM_DIR>/state/services.json`.
- **Manifest generator** — `awm/services/packages/gen.py` (regex scan for `from '@awm/<x>'`, idempotent write-if-changed).
- **CLI** — `awm packages gen/sync/list` and `awm dev shadow` live in `awm/cli.py` (search for `packages_app` / `dev_app`).
- **Middleware dispatch** — `awm/server.py::HubRoutingMiddleware._dispatch_service` (the entry point that translates `/svc/<name>/{fn,session,emit}/*` into the rpc.py layer).

The Service Hub section above carries the *external* contract (which kinds exist, the post-implementation summary of what this scope built, the port table); this section tells you which files implement each piece if you're about to change them.

## Awm Editable Install Gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `/home/tony/lib/miniforge3/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds:

- Run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping, OR
- Spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree, OR
- Merge dev → release to advance the editable mapping's target (what the deploy step does).

See memory `[[awm_two_source_trees]]` for the full failure mode.

## Running tests

Pytest tests live under `awm/tests/` and are organized into per-subsystem subdirectories (`unit/`, `hub/`, `scopes/`, `messaging/`, `auth/`, `mcp/`, `artifacts/`, `sessions/`, `agent/`, `misc/`). Every test file declares a module-level `pytestmark` so you can select by subsystem **or** by speed; markers are registered in `pyproject.toml` (`pytest --markers` lists them).

```bash
# Fast dev-iteration set. Pure unit + small in-process tests, no subprocesses.
mamba run -n awm pytest -m unit

# One subsystem at a time (path or marker — both work):
mamba run -n awm pytest awm/tests/hub/
mamba run -n awm pytest -m messaging

# Everything except subprocess/git clusters:
mamba run -n awm pytest -m "not slow"

# Full suite. Run before merging.
mamba run -n awm pytest

# Preview a selection without running it (sanity-check before a long run):
mamba run -n awm pytest -m unit --collect-only -q
```

Markers in use: `smoke` / `slow` / `subprocess` (cross-cutting), plus one per subsystem (`unit`, `hub`, `scopes`, `messaging`, `auth`, `mcp`, `artifacts`, `sessions`, `agent`, `misc`). To retag a file, edit its top-of-file `pytestmark = [...]` line.

## Agent Rules

1. **Keep WORKSPACE.md and AGENTS.md audience-pure** — workspace-structural goes in `WORKSPACE.md`, awm-internal goes here. If you find yourself adding path tables or MCP catalogs to this file, they belong in `WORKSPACE.md` instead.
2. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it's the mechanism that keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
3. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.
4. **Don't break the SessionStart hook contract** — `awm context emit` must exit 0 with empty stdout when no relevant files exist. Hooks that error block session start.
