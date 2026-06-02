# AWM Internal Architecture

*Internal architecture reference for agents working ON awm itself: Service Hub protocol, vertical-stripe component dev infra, Python/env conventions. Auto-injected only when the agent's cwd contains this file at its root — `projects/awm/*` scopes inherit it via .bare-worktree sharing; other projects' agents never see it. **Do not merge into WORKSPACE.md** — that file is universal, this one is awm-private; keeping them separate is what keeps non-awm agents' contexts uncluttered.*

For workspace structure (paths, MCP tools, project map, scope lifecycle) see `WORKSPACE.md` (auto-injected before this file). This file assumes you're modifying awm itself.

## Component Dev Architecture

The frontend has two complementary seams that contain UI complexity and turn composition bugs into autonomous test failures. **Do this work in a worktree that has `feat/infra-dev-components` merged in** (every `comp-*` and `infra-typed-seams` branch carries the doc but not the runtime — see § "Where to run" below).

### Per-component dev surface (`infra-dev-components`)

Each component owns a sibling `<Name>.fixtures.ts` file declaring variants. No central registry — Vite globs them at build time.

```ts
// frontend/src/lib/components/StatusTag.fixtures.ts
import type { ComponentProps } from 'svelte';
import Component from './StatusTag.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  active: { status: 'active' },
  failed: { status: 'failed' },
};
export { Component as component };
export default fixtures;
```

Dev surface routes:

- `/dev/components` — auto-generated index of every `*.fixtures.ts` under `src/lib/components/`.
- `/dev/components/[slug]?v=<variant>` — single-component view with variant switcher.
- Root `+layout.svelte` skips app chrome and the backend bootstrap on `/dev/*`, so dev pages never call `/voice`, `/rooms`, `/peers`, or `/vagrant`.

`npm run test` runs `vitest` + `jsdom`. A single generic runner (`src/lib/dev/fixtures.test.ts`) globs the same fixture set and mounts every variant — **crash-on-mount bugs surface in CI without anyone opening a browser**. Adding a fixture requires zero changes to the runner.

### Bind-prop wrapper pattern

For `$bindable` props whose bug lives at the parent's bind direction, the fixture points at a thin wrapper Svelte file that wires the bind from local state. For `AgentList`, the parent itself is the wrapper, so no extra file is needed — the failing variants in `AgentList.fixtures.ts` reproduce the composition-seam crash autonomously.

### Typed seam (`infra-typed-seams`)

`npm run gen-types` spawns a one-shot Python process in the `awm` mamba env that imports `awm.exposed:app` and calls `app.openapi()` directly. No live uvicorn required, no auth wall. Output goes to `frontend/src/lib/api/generated.ts` (committed). Spawn cwd + `sys.path` are pinned to the worktree root so `import awm` resolves to the worktree's source, not the editable install (see memory `[[awm_two_source_trees]]`).

Hand-written interfaces in `client.ts` get progressively replaced by re-exports from `generated.ts`. The first proof-of-seam is `VagrantSessionResponse`. The migration is intentionally narrow — types that match 1:1 swap immediately; types that diverge in shape stay hand-written until the backend tightens its `response_model` declarations.

Engine `CONFIG_SCHEMA` JSON Schemas escape this pipeline (FastAPI types their envelope as `dict[str, Any]` → `unknown`). Fixtures for engine forms hand-shape JSON Schema blobs.

### Workflow

`node`/`npm` live in the `awm` mamba env, not on the default PATH. Prefix as shown:

```bash
cd frontend
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm install

# Visual: see fixtures in the browser
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run dev   # http://localhost:12103/ui/dev

# Autonomous: fail CI on crash-on-mount bugs
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run test

# Regenerate types after Pydantic model changes
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run gen-types
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run check
```

### Where to run

- **`feat/infra-dev-components`** has the dev routes, vitest config, and the generic runner. Anything you add a fixture for shows up here.
- **`feat/infra-typed-seams`** has the `gen-types` script and `generated.ts`.
- **`feat/comp-*`** branches carry only the fixture file for their component. To verify a `comp-*` fixture, either merge `feat/infra-dev-components` into the comp branch (and `feat/infra-typed-seams` if the component needs generated types), or use the `verify/integration` branch that octopus-merges all five.

## Service Hub

`awm.exposed:app` is a routing layer. Most requests are served by its in-process routers (`/rooms`, `/peer`, `/voice`, …). A few path prefixes are *registered* at runtime; matched requests are either forwarded to an external service (`kind=url`) or served from a registered directory (`kind=static`).

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

A vertical stripe typically combines a `svc-*` (backend, `kind=url`) and a `comp-*` (frontend bundle, `kind=static`) registered through the same hub origin. End-user view:

```
https://127.0.0.1:7820/x/whatever   ← backend routes (forwarded to svc-X process)
https://127.0.0.1:7820/comp-x/      ← frontend bundle (served from disk)
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

**Browser login.** The hub middleware short-circuits route auth for both `kind=static` and `kind=url`, so a stripe-only URL (`/comp-x/`, `/x/...`) opens without a cookie. To exercise a stripe inside the full SPA — where the bundle may `fetch('/rooms')`, `/voice`, etc. — visit the login bookmark first:

```
http://127.0.0.1:<login-port>/   (e.g. :7822 for dev, :7832 for web-ui)
```

Each refresh mints a fresh single-use 60s-TTL `/auth/bootstrap?ot=…` URL; clicking it sets an `HttpOnly` cookie. CLI form: `./dev/run.sh login` from inside the sandbox prints the same URL.

#### svc-X (kind=url)

1. **Build the FastAPI service.** Two requirements:
   - Mount your routes under the claimed prefix (`APIRouter(prefix="/x")`) — the hub forwards the path verbatim, so the same path it gets must land on your routes.
   - Gate routes with `Depends(require_peer_bearer)` from `awm.middleware_auth`. The hub strips the user's bearer and injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>`; `require_peer_bearer` validates exactly that. `X-Awm-As` is preserved verbatim. Copy `awm/demos/echo_svc.py` as the starting skeleton.

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

Hub → service is degenerate peer auth (URL kind only). The hub injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>` on every forwarded request; the user's bearer (`Authorization` header / `awm_session` cookie) is stripped. `X-Awm-As` is preserved verbatim. Services gate routes with `from awm.middleware_auth import require_peer_bearer` — one import, no new bearer concept.

Static-kind registrations don't proxy, so there's no second-hop auth — bytes are served by the hub directly, subject to whatever middleware sits in front of the hub itself.

### What `comp-*` and frontend slices need to know about consuming

**Nothing.** The hub IS `awm.exposed:app`. Same origin, no new port. With an empty registry the behavior is byte-identical to a hub-less awm. The frontend can `fetch('/x/whatever')` without CORS, cookies, or a second port.

### Demos

- `awm/demos/echo_svc.py` — 60-line FastAPI smoke test; copy as the starting point for a real `svc-*`.
- `awm/demos/static_demo/` — naked `main.js` + `style.css` bundle; copy as the starting point for a `comp-*` registration. README has the one-liner.

### Gotchas

- **Prefix conflicts return 409.** Pick a unique prefix per stripe. `/hub` and `/hub/*` are reserved (the lease socket has to stay reachable).
- **One lease holder per service_id.** Re-registering while a lease is held returns 409 — Ctrl-C the old one or `awm hub deregister <name>` first.
- **`AWM_WORKSPACE` matters.** Without it, the CLI uses the global discovery file and may target the prod `:7820` instead of your sandbox. `./dev/run.sh` exports it for its own children; if you shell out separately, export it yourself.
- **Vite dev server vs static dir.** During hot-reload iteration, register `--url http://127.0.0.1:<vite-port>` instead of `--dir` — point at the dev server directly. Switch to `--dir ./dist` once you're past the rebuild loop.
- **Never run two `awm.exposed` on the same port.** Side-by-side sandboxes on distinct ports (`:7821`, `:7831`, …) are explicitly supported and how dev parallelism works.

## Developing a vertical stripe

A *vertical stripe* is one feature packaged together as a workspace package under `projects/awm/dev/packages/<name>/`: a static frontend bundle plus (optionally) one long-running backend process. The hub discovers stripes by walking `packages/*/package.json` at sandbox start, registers each as `kind=stripe`, supervises the backends, and the dev-shell at `/dev/` lets you mount any of them in isolation for testing.

This complements the older `svc-*` + `comp-*` flow (§ Stripe-presentation protocol). The packaged form is preferred for **new** components: one directory, one manifest, no separate registration boilerplate, scope worktrees inherit the whole monorepo automatically.

### Anatomy

```
packages/<name>/
  package.json     ← declares the stripe via a `stripe` field
  src/             ← source (only for stripes with a real build step)
  dist/            ← frontend bundle the hub serves (generated; gitignored)
    index.html     ← served at the prefix root
    main.js, …
  .gitignore       ← `dist/` — for stripes that build their bundle
  server.js        ← backend entry (optional; any executable)
```

The hub serves `dist/` at `<prefix>/` (canonical paths only — see § Stripe-presentation protocol for what that means) and proxies `<prefix>/_api/*` to the supervised backend at the port the hub allocated.

### `dist/` is integrator-built, not committed

The contract: `dist/` is whatever's on disk at sync time, full stop. *How* it got there is the package's business.

- **Stripes with a `build` script in `package.json`** generate `dist/` and gitignore it locally (`packages/<name>/.gitignore` with `dist/`). The integrator (`./dev/run.sh start`) runs `npm install && npm run build --workspaces --if-present` before launching `awm stripe sync`, so generated `dist/`s exist by the time the hub registers them. Skipping the build leaves stale or missing bytes; the hub serves what it finds.
- **Hand-authored stripes** (`hello`, `dev-shell`) have no `build` script — their `dist/` IS source, hand-written there, and stays tracked in git. `--if-present` skips them at build time; the hub serves the committed bytes.

The repo stays clean (no 100K+ bundles in PR diffs) and the hub stays dumb (still serves bytes verbatim, no compile-on-request). The trade is one rebuild per sandbox start.

### `stripe` field in package.json

```jsonc
{
  "name": "@awm/hello",
  "stripe": {
    "frontend": "dist/",            // path to the bundle dir (relative to package.json)
    "prefix": "/hello",             // optional — defaults to "/" + bare-name (no @scope/)
    "backend": {                    // optional — omit for frontend-only stripes
      "cmd": ["node", "server.js", "${AWM_SERVICE_PORT}"],
      "env": {},                    // merged under hub-injected vars (hub wins on conflict)
      "health": "/healthz",         // path the hub polls until 200
      "cwd": "."                    // optional — defaults to the frontend dir
    }
  }
}
```

Packages **without** a `stripe` field are libraries (e.g. `@awm/bus`); `awm stripe sync` ignores them.

### Backend contract

When a stripe declares a backend, the hub:

1. Allocates a free port from `AWM_STRIPE_PORT_POOL` (default `7900-7999`).
2. Spawns `cmd` with `AWM_SERVICE_PORT=<port>` in env, and substitutes any `${AWM_SERVICE_PORT}` token in `cmd` args verbatim (no shell — argv form only).
3. Polls `http://127.0.0.1:<port><health>` every 500ms, up to 30s. First 2xx → `backend_status: ready`; timeout → `down`.
4. Routes `<prefix>/_api/*` to the backend once ready (503 + Retry-After before that).
5. SIGTERMs the process group on lease close (5s grace → SIGKILL). Backends should drain on SIGTERM and exit cleanly.

stdout + stderr land in `$AWM_DIR/logs/stripes/<service_id>.log`. **No auto-restart** in dev — crashes stay loud so you notice. No env mutation outside the `env` map. No file watching — rebuild + re-register (lease re-attach) to pick up changes.

**Caller identity.** Every proxied request (HTTP and WebSocket) carries `X-Awm-As: <operator>` when the browser session is authenticated, and *no* `X-Awm-As` header at all when anonymous. The hub mints the header at the proxy edge from the `awm_as` cookie — your backend does not read cookies, does not validate bearers, does not call `/auth/whoami`. The header is forge-resistant: the hub overwrites any inbound `X-Awm-As` the client supplied. Bind to `127.0.0.1` (the supervisor already does); that's the trust boundary. If you need finer-grained policy, enforce it yourself against the header value.

### Authoring a frontend stripe

- **Use relative URLs for backend calls.** `fetch('./_api/echo')`, not `fetch('/hello/_api/echo')`. The same bundle then works at any prefix (`/hello`, `/hello-rework`, `/dev-stuff/hello`, …) without rebuilding.
- **`@awm/bus` is the cross-stripe pub/sub.** Import the singleton: `import { bus } from '@awm/bus'`. Channels are plain strings; payloads are `unknown` — narrow at the subscribe site. Use `replayLast: true` for late-mounting consumers of single-slot state.
- **Read the operator with `getOperator()` from `@awm/bus`.** It's a cached fetch to `/auth/whoami` — one round-trip per page load no matter how many components call it. Don't build `X-Awm-As` yourself on outgoing fetches; the hub will inject (and overwrite) it for you, so your value is silently ignored. `awm_as` is HttpOnly so you can't read it from JS anyway.
- **Sibling wiring goes through props/emitters**, not the bus. The bus is for events that cross stripe boundaries; component-to-component within a composite stripe stays explicit so the data flow is local to the parent.
- **Frontend bundles are pure static.** No SSR, no API routes — that's what the backend is for.

### Sandbox workflow

**There is one canonical hub per node: the dev sandbox at `projects/awm/dev/` on port `7821`.** `comp-*` and `svc-*` scope worktrees do not run their own parallel hub for component work — they register *into* the dev sandbox's hub. Running a second `./dev/run.sh` from a scope worktree is reserved for work on the hub itself (`awm.exposed`, `dev/run.sh`, stripe-sync, control-plane); it is not the component-dev path. (Per-scope port bands exist — see § Hub origin — so the parallel-sandbox path *works*, it's just rarely the right one.)

```bash
cd /home/tony/agentic_workspace/projects/awm/dev

./dev/run.sh restart            # uvicorn + login server + workspace build + `awm stripe sync packages/`
awm stripe list                 # all registered stripes, with backend status
```

`./dev/run.sh start` does, in order: launch uvicorn, launch the login server, install workspace deps if `node_modules/` is missing, run `npm run build --workspaces --if-present`, then launch `awm stripe sync $REPO_ROOT` as a tracked process (PID in `.awm/stripe-sync.pid`). Build failures are logged to `.awm/stripe-sync.log` and abort the sync — fix the build, then restart.

On `stop` / `restart` the sync process is SIGTERM'd first, which closes every lease, which triggers per-stripe eviction (and SIGTERMs the supervised backends) before uvicorn goes down.

For tighter iteration on a single stripe, drop into the package and run its `build` script directly — no need to restart the sandbox unless you're touching the backend, the manifest, or registration plumbing.

Open `https://127.0.0.1:7821/dev/` in a browser → pick a stripe from the rail → it mounts in an iframe at its registered prefix. Each stripe runs at its own URL so you can also deep-link directly (`https://127.0.0.1:7821/hello/`).

### Composing stripes

Two patterns, used together:

- **Build-time composition (preferred for tight coupling).** A composite stripe declares its leaves as workspace deps in its own `package.json`, imports them, and renders them. The leaves are registered separately too — each appears in the dev-shell — but the production view comes from the composite stripe's bundle. Component-to-component wiring is via props + emitters at the composite's boundary.
- **Runtime composition (for loose coupling).** Two stripes that don't know about each other coordinate through `@awm/bus`. The STT stripe `publish`es a transcribed utterance; a chat stripe `subscribe`s and renders it. Neither stripe imports the other.

### Reworking an existing stripe

The whole point of the monorepo is that a scope worktree gets a complete copy of `packages/` via git, so you can iterate on a stripe in isolation while `dev`'s version stays mounted. Default flow: **edit in your scope worktree, then register the scope's bundle into dev's hub** under a unique name. Dev's auto-synced copy of the stripe stays mounted alongside, so you A/B against it at distinct URLs.

```bash
awm scope create awm hello-rework --from dev
cd /home/tony/agentic_workspace/projects/awm/hello-rework

# Edit packages/hello/..., then rebuild this scope's bundle:
PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH \
  npm --workspace @awm/hello run build      # skip for hand-authored stripes

# Register into dev's hub under a unique name + prefix (lease blocks; Ctrl-C evicts):
export AWM_WORKSPACE=/home/tony/agentic_workspace/projects/awm/dev/dev
awm stripe register \
  --package packages/hello \
  --name @awm/hello-rework \
  --prefix /hello-rework
# Now dev's hub has both @awm/hello (auto-synced) at /hello AND
# @awm/hello-rework (this scope's bundle) at /hello-rework.

# Finished:
awm scope complete awm hello-rework --merge --cleanup
# Back on dev: pull, restart → sync re-registers the merged version at /hello.
```

The lease watches `packages/<name>/dist/` on disk, so rebuilding the bundle in this worktree is picked up without re-registering. Re-register only when the manifest, prefix, or backend changes.

If you genuinely need a second hub (working on `awm.exposed` itself, or testing what happens when dev's hub is down), `./dev/run.sh restart` from this worktree spins up a parallel sandbox on the scope's port band — see § Hub origin for the table. That sandbox auto-syncs its own `packages/`, but it's a separate origin you log into separately.

### Gotchas

- **Name collisions on rework.** Without `--name <override>` the manual register collides with the auto-synced version — `awm stripe register` will 409. Pick a distinct name for the rework copy.
- **`backend_status: starting` returns 503.** Routing the `_api/*` sub-prefix is gated on the health-poll having seen 200. If a stripe stays `starting`, check `$AWM_DIR/logs/stripes/<service_id>.log` — the backend's own stderr is the first place to look.
- **No port collision recovery.** If the backend logs ECONNREFUSED on its own port or "address in use", the supervisor doesn't retry — kill and re-register. The port pool advances past in-use ports on the next allocation.
- **`awm stripe sync` blocks.** It holds every lease in one process. Running it directly (rather than through `./dev/run.sh`) means Ctrl-C tears everything down, which is what you want when iterating on the sync itself.
- **Vite-dev for hot reload.** The hub serves what's on disk; for the rebuild-on-save loop, register the stripe with `awm hub register --url http://127.0.0.1:<vite-port>` instead of via the package — same trick as `comp-*`.
- **Setting `X-Awm-As` manually on a `fetch` is a no-op.** The hub overwrites it from the `awm_as` cookie on every stripe proxy hop, so the value you sent is dropped. If you need to act on behalf of a different operator, that's peer federation (see § Service Hub), not a stripe concern.

## Awm Editable Install Gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `/home/tony/lib/miniforge3/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds:

- Run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping, OR
- Spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree (this is how `npm run gen-types` does it — see `frontend/scripts/gen-types.py`), OR
- Merge dev → release to advance the editable mapping's target (what the deploy step does).

See memory `[[awm_two_source_trees]]` for the full failure mode.

## Running tests

Pytest tests live under `awm/tests/awm/tests/` and are organized into per-subsystem subdirectories (`unit/`, `hub/`, `scopes/`, `messaging/`, `federation/`, `auth/`, `mcp/`, `artifacts/`, `sessions/`, `agent/`, `misc/`). Every test file declares a module-level `pytestmark` so you can select by subsystem **or** by speed; markers are registered in `pyproject.toml` (`pytest --markers` lists them).

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

Frontend tests are separate: `cd frontend && PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH npm run test` runs the vitest fixture sweep — already fast (jsdom only).

## Agent Rules

1. **Keep WORKSPACE.md and AGENTS.md audience-pure** — workspace-structural goes in `WORKSPACE.md`, awm-internal goes here. If you find yourself adding path tables or MCP catalogs to this file, they belong in `WORKSPACE.md` instead.
2. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it's the mechanism that keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
3. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.
4. **Don't break the SessionStart hook contract** — `awm context emit` must exit 0 with empty stdout when no relevant files exist. Hooks that error block session start.
