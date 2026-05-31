# AWM Scope Agent

You are working in a scope worktree of the `awm` project. This file is the tracked, branch-shared orientation for **all** scope agents in this workspace. The per-scope override layer lives in `.awm/context.md` (gitignored, auto-loaded by the `SessionStart` hook).

## Workspace Layout

| Path | Purpose |
|------|---------|
| `awm/` | AWM service package (Python) + skills catalog |
| `data/` | Shared data (per-project; raw, staged, outputs) |
| `projects/` | Project bare repos + git worktrees (agents work here) |

### Per-Scope Layout

Agents land directly in the git worktree. All AWM metadata lives in a `.awm/` dotdir inside:

```
projects/{project}/
  .bare/                         # bare git repo
  {scope}/                       # git worktree — agent CWD
    .awm/                        # AWM metadata (gitignored)
      context.md                 # scope instructions (auto-loaded)
      history.md                 # auto-generated: open/resolved session history
      artifacts.md               # auto-generated: project artifact index
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md` (auto-loaded), runs `awm_refresh`, reads `history.md` + `artifacts.md`.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent follows `skills_get path="awm/debrief.md"`.
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own. Names are flat (slashes are rejected — see `awm/services/_validation.py`), so the family is encoded as a hyphen-prefix.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | A single frontend component + its fixtures (one slug under `/dev/components/`). |
| `svc-*`  | service   | A backend service contract — endpoints, models, the Pydantic surface for an area. |
| `feat-*` | feature   | End-to-end integration that wires components, services, and external engines together (e.g. `feat-stt`, `feat-rooms`). |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume — codegen, dev surfaces, test runners. |

Older scopes (`dev`, `sentry`, `vagrant-*`, `voice`, `web-ui`) predate this convention and keep their flat keyword names. The prefix family applies to scopes created from this point forward.

The dev surface that backs the `comp-*` family lives at `/dev/components/[name]` (see `infra-dev-components`).

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

## Skills System

Skills are dynamic protocols that improve with use:

- **AWM skills** (`skills/awm/`): workspace procedures that drive the MCP tool surface (create-project, create-scope, debrief, skill-update)
- **Tool guides** (`skills/tools/`): external-tool references (git, mamba, dependencies, mcp, metasmith, plotly)
- Skills have frontmatter with `tags`, `requires`, and `scope` for search and hierarchy
- `skills_search` combines keyword + semantic search (sentence-transformers embeddings)
- **Session logs** can include execution traces attached to skills — log what happened, outcome, deviations, and improvement suggestions via `session_log`
- A dedicated `awm/skill-improvement` scope periodically reads session logs and revises skills

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}`
- PRs created from feature branches
- See `skills/tools/git.md` for details

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use:** `python`, `python3`, `pip`, or `pip3` directly.
**Do NOT use:** `conda activate` or `mamba activate` (requires interactive shell init).

**Always use:**
```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `.awm/data/`** — shared across all scopes in the project.
3. **Don't edit `.awm/history.md` or `.awm/artifacts.md`** — these are auto-generated. Use MCP tools.
4. **Follow the debrief skill** when ending a session — log sessions, register artifacts, reflect.
5. **Search skills first** — use `skills_search` before starting unfamiliar workflows.
