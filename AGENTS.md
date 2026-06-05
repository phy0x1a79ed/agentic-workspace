# AWM Internal Architecture

*Internal architecture reference for agents working ON awm itself: Service Hub protocol, package model, Python/env conventions. Auto-injected only when the agent's cwd contains this file at its root — `projects/awm/*` scopes inherit it via .bare-worktree sharing; other projects' agents never see it. **Do not merge into WORKSPACE.md** — that file is universal, this one is awm-private; keeping them separate is what keeps non-awm agents' contexts uncluttered.*

For workspace structure (paths, MCP tools, project map, scope lifecycle) see `WORKSPACE.md` (auto-injected before this file). This file assumes you're modifying awm itself.

## Federation: DISABLED (as of 2026-06-04)

`awm-exposed.service` is **stopped and unit-unlinked**. The `/peer/*` routes, cross-host messaging, federated find, cross-peer rooms, leadership election, and cr-sqlite replication are all off. `awm.service` (local IPC on `127.0.0.1:7819`) is unaffected — every MCP tool, CLI command, and in-process API that doesn't cross a host boundary still works.

**Why off, not repaired:** peers (`capella`/`mira`/`xps`) last gossiped on 2026-05-25; the exposed listener had been crash-looping on `EADDRINUSE` (5,222 systemd restarts) leaking SSH tunnels by the hundreds; and three substrate changes are queued — v37 identity-table schema (landed on this branch, deploy pending), 74 commits + 26 dirty files on `dev`, and the new agent infrastructure — any of which would re-break a same-day repair. The peer registry rows (`peer_search` / `awm peer list`) are intact for when federation comes back.

**Don't:**
- Rely on `@<peer-id>` scope addressing (`awm inbox send scope:x@mira ...`) — it will block on a missing tunnel.
- Trust `leadership_state` from `awm status` — it reports `STANDBY` / `current_leader: null` permanently while exposed is down.
- Add code that assumes the `/peer/*` surface is reachable from this host.

**To re-enable** (do this *after* v37 deploy + dev consolidation + agent-infra rollout land):

```bash
systemctl --user link /home/tony/agentic_workspace/deploy/awm-exposed.service
systemctl --user enable --now awm-exposed.service
systemctl --user status awm-exposed.service
awm peer ping mira && awm peer ping xps
```

Before re-enabling, also fix `voice engine restore failed: No module named 'voice'` in the exposed startup path — leftover from the S3 voice removal (commit `5766206`); harmless warning today, but it indicates other dangling references the deploy may turn into hard errors.

## Service Hub

`awm.exposed:app` is a routing layer. Most requests are served by its in-process routers (`/rooms`, `/peer`, …). A few path prefixes are *registered* at runtime; matched requests dispatch to one of four kinds:

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

What this scope did NOT do (deferred):

- Cross-peer service replication.

### Hub origin = `awm.exposed` port

There's only ever one hub origin per node — the `awm.exposed` process. Which port that is depends on context:

| Context | Port | What runs |
|---|---|---|
| Production (systemd-managed) | `7820` | `awm.exposed.service` on the host |
| Dev sandbox: `projects/awm/dev/` | `7821` | `./dev/run.sh start` |
| Dev sandbox: `projects/awm/web-ui/` | `7831` | same |
| Dev sandbox: `projects/awm/web-backend/` | `7841` | same |
| Dev sandbox: any other scope (fallback) | `7851` | same |

The per-scope port bands are set by `dev/run.sh` from the worktree dirname, so all sandboxes can run side-by-side with the real `awm.exposed` and with each other. Bookmark `http://127.0.0.1:<login-port>/` (always uvicorn+1) for one-shot login URLs. See `dev/AGENTS.md` for the full per-worktree table.

**Substitute your sandbox port** whenever this section says `:7820`.

### When to make a `svc-*` scope

Use a `svc-*` worktree when a stripe needs to **own** a path prefix end-to-end: ship its own routes, run as its own process, iterate without rebuilding the monolith. PTT V2 (audio + WS + STT) is the first customer. Pure shared-library refactors stay in `awm/`.

### Stripe-presentation protocol

A vertical stripe typically combines a `svc-*` (backend, `kind=url`) and a `comp-*` (static bundle, `kind=static`) registered through the same hub origin. End-user view:

```
https://127.0.0.1:7820/x/whatever   ← backend routes (forwarded to svc-X process)
https://127.0.0.1:7820/comp-x/      ← static bundle (served from disk)
```

Same scheme, same host, same port. The svc-X port is plumbing the hub knows about; browsers never see it.

#### One-time per node

```bash
./dev/run.sh start              # bring up the dev hub (or skip if prod awm.exposed is already up)
export AWM_WORKSPACE=$PWD/dev   # so the awm CLI uses this sandbox's .awm/ for token + discovery
awm hub trust-self              # writes auth token to .awm/peers/<self>.token
                                # — only needed if any stripe uses kind=url
```

`trust-self` is idempotent. It only matters for URL-kind registrations: the hub forwards as the local peer, so the local peer's own token has to live in `peers/`.

**Browser login.** The hub middleware short-circuits route auth for both `kind=static` and `kind=url`, so a stripe-only URL (`/comp-x/`, `/x/...`) opens without a cookie. To exercise a stripe that calls back to authenticated routes (`fetch('/rooms')`, `/peer/...`, etc.), visit the login bookmark first:

```
http://127.0.0.1:<login-port>/   (e.g. :7822 for dev, :7832 for web-ui)
```

Each refresh mints a fresh single-use 60s-TTL `/auth/bootstrap?ot=…` URL; clicking it sets an `HttpOnly` cookie and redirects to `/ui/agent`. CLI form: `./dev/run.sh login` from inside the sandbox prints the same URL.

#### svc-X (kind=url)

1. **Build the FastAPI service.** Two requirements:
   - Mount your routes under the claimed prefix (`APIRouter(prefix="/x")`) — the hub forwards the path verbatim, so the same path it gets must land on your routes.
   - Gate routes with `Depends(require_peer_bearer)` from `awm.services.auth.middleware_auth`. The hub strips the user's bearer and injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>`; `require_peer_bearer` validates exactly that. `X-Awm-As` is preserved verbatim. Copy `awm/demos/echo_svc.py` as the starting skeleton.

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

All four require `Authorization: Bearer $(cat .awm/auth.token)`.

### Auth model

Hub → service is degenerate peer auth (URL kind only). The hub injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>` on every forwarded request; the user's bearer (`Authorization` header / `awm_session` cookie) is stripped. `X-Awm-As` is preserved verbatim. Services gate routes with `from awm.services.auth.middleware_auth import require_peer_bearer` — one import, no new bearer concept.

Static-kind registrations don't proxy, so there's no second-hop auth — bytes are served by the hub directly, subject to whatever middleware sits in front of the hub itself.

### What `comp-*` and page stripes need to know about consuming

**Nothing.** The hub IS `awm.exposed:app`. Same origin, no new port. With an empty registry the behavior is byte-identical to a hub-less awm. A page stripe can `fetch('/x/whatever')` without CORS, cookies, or a second port.

### Demos

- `awm/demos/echo_svc.py` — 60-line FastAPI smoke test; copy as the starting point for a real `svc-*`.
- `awm/demos/static_demo/` — naked `main.js` + `style.css` bundle; copy as the starting point for a `comp-*` registration. README has the one-liner.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix per stripe. `/hub` and `/hub/*` are reserved (the lease socket has to stay reachable).
- **One lease holder per service_id.** Re-registering while a lease is held returns 409 — Ctrl-C the old one or `awm hub deregister <name>` first.
- **`AWM_WORKSPACE` matters — for the CLI and for every svc process.** Without it, the CLI uses the global discovery file and may target the prod `:7820` instead of your sandbox. `./dev/run.sh` exports it for its own children; if you shell out separately, export it yourself. A `kind=url` svc's `AWM_WORKSPACE` must match the hub it's registered with — `require_peer_bearer` looks the hub's injected bearer up in that workspace's `peers/` dir, so a mismatch yields silent 401s on every forwarded request. If a svc is already running, `tr '\0' '\n' </proc/<pid>/environ | grep AWM_WORKSPACE` tells you which hub it was set up against; register it there rather than spinning a parallel sandbox.
- **Vite dev server vs static dir.** During hot-reload iteration, register `--url http://127.0.0.1:<vite-port>` instead of `--dir` — point at the dev server directly. Switch to `--dir ./dist` once you're past the rebuild loop.
- **Never run two `awm.exposed` on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are explicitly supported and how dev parallelism works.

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
- **Middleware dispatch** — `awm/exposed.py::HubRoutingMiddleware._dispatch_service` (the entry point that translates `/svc/<name>/{fn,session,emit}/*` into the rpc.py layer).

The Service Hub section above carries the *external* contract (which kinds exist, the post-implementation summary of what this scope built, the port table); this section tells you which files implement each piece if you're about to change them.

## Awm Editable Install Gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `/home/tony/lib/miniforge3/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds:

- Run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping, OR
- Spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree, OR
- Merge dev → release to advance the editable mapping's target (what the deploy step does).

See memory `[[awm_two_source_trees]]` for the full failure mode.

## Running tests

Pytest tests live under `awm/tests/` and are organized into per-subsystem subdirectories (`unit/`, `hub/`, `scopes/`, `messaging/`, `federation/`, `auth/`, `mcp/`, `artifacts/`, `sessions/`, `agent/`, `misc/`). Every test file declares a module-level `pytestmark` so you can select by subsystem **or** by speed; markers are registered in `pyproject.toml` (`pytest --markers` lists them).

```bash
# Fast dev-iteration set (~35s on this host, 161 tests). Pure unit + small
# in-process tests, no subprocesses, no federation. Use on every save.
mamba run -n awm pytest -m smoke

# One subsystem at a time (path or marker — both work):
mamba run -n awm pytest awm/tests/hub/
mamba run -n awm pytest -m messaging

# Everything except subprocess/git/federation/replication clusters:
mamba run -n awm pytest -m "not (slow or federation)"

# Full suite (~10 min). Run before merging.
mamba run -n awm pytest

# Preview a selection without running it (sanity-check before a long run):
mamba run -n awm pytest -m smoke --collect-only -q
```

Markers in use: `smoke` / `slow` / `federation` / `subprocess` (cross-cutting), plus one per subsystem (`unit`, `hub`, `scopes`, `messaging`, `auth`, `mcp`, `artifacts`, `sessions`, `agent`, `misc`). To retag a file, edit its top-of-file `pytestmark = [...]` line.

## Agent Rules

1. **Keep WORKSPACE.md and AGENTS.md audience-pure** — workspace-structural goes in `WORKSPACE.md`, awm-internal goes here. If you find yourself adding path tables or MCP catalogs to this file, they belong in `WORKSPACE.md` instead.
2. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it's the mechanism that keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
3. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.
4. **Don't break the SessionStart hook contract** — `awm context emit` must exit 0 with empty stdout when no relevant files exist. Hooks that error block session start.
