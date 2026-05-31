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
   - If not, the hub renders an ESM auto-shell: empty `<div id="<mount-id>"></div>`, `<link>` for each `--css`, `<script type="module" src=".../<entry>">`.
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

## Awm Editable Install Gotcha

General Python env rules (use `mamba run -n awm`, never `pip` / `python` / `mamba activate` directly) live in `WORKSPACE.md`. The awm-specific wrinkle:

The editable install at `/home/tony/lib/miniforge3/envs/awm/lib/python3.14/site-packages/__editable___awm_0_1_0_finder.py` maps `awm` → `/home/tony/agentic_workspace/awm` (the **release** worktree). When iterating on dev-tree code, `import awm` from the conda env silently resolves to release code, not the dev tree. Workarounds:

- Run with explicit `PYTHONPATH=<dev-worktree>` to shadow the editable mapping, OR
- Spawn a Python subprocess with cwd + `sys.path[0]` pinned to the dev worktree (this is how `npm run gen-types` does it — see `frontend/scripts/gen-types.py`), OR
- Merge dev → release to advance the editable mapping's target (what the deploy step does).

See memory `[[awm_two_source_trees]]` for the full failure mode.

## Agent Rules

1. **Keep WORKSPACE.md and AGENTS.md audience-pure** — workspace-structural goes in `WORKSPACE.md`, awm-internal goes here. If you find yourself adding path tables or MCP catalogs to this file, they belong in `WORKSPACE.md` instead.
2. **The `awm/skills/awm/debrief.md` skill is mandatory at end-of-session** — it's the mechanism that keeps `.awm/history.md` and `.awm/artifacts.md` accurate across all scopes.
3. **`awm scope heal` is idempotent and safe** — run with `--dry-run` first to preview, then for real. Enforces tier-3 = `.awm/` only.
4. **Don't break the SessionStart hook contract** — `awm context emit` must exit 0 with empty stdout when no relevant files exist. Hooks that error block session start.
