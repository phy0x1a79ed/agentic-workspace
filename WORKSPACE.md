# Workspace Reference

Shared context for all agents in this workspace.

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
      context.md                 # scope instructions
      history.md                 # auto-generated: open/resolved session history
      artifacts.md               # auto-generated: project artifact index
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md`, runs `awm_refresh`, reads `history.md` + `artifacts.md`.
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

The frontend has two complementary seams that contain UI complexity and turn composition bugs into autonomous test failures.

### Per-component dev surface (`infra-dev-components`)

Each component owns a sibling `<Name>.fixtures.ts` file that declares variants. No central registry — Vite globs them at build time.

```ts
// frontend/src/lib/components/StatusTag.fixtures.ts
import type { ComponentProps } from 'svelte';
import Component from './StatusTag.svelte';

const fixtures: Record<string, ComponentProps<typeof Component>> = {
  active:    { status: 'active' },
  failed:    { status: 'failed' },
  // ...
};
export { Component as component };
export default fixtures;
```

The dev routes mount one fixture at a time:

- `/dev/components` — auto-generated index of every `*.fixtures.ts` under `src/lib/components/`.
- `/dev/components/[slug]?v=<variant>` — single-component view with variant switcher.
- The root `+layout.svelte` skips app chrome and the backend bootstrap on `/dev/*`, so dev pages never call `/voice`, `/rooms`, `/peers`, or `/vagrant`.

`npm run test` runs `vitest` + `jsdom`. A single generic runner (`src/lib/dev/fixtures.test.ts`) globs the same fixture set and mounts every variant — crash-on-mount bugs surface in CI without anyone opening a browser. Adding a fixture needs zero changes to the runner.

**Bind-prop wrapper pattern.** For `$bindable` props whose bug lives at the parent's bind direction, the fixture points at a thin wrapper Svelte file that wires the bind from local state. For `AgentList`, the parent itself is the wrapper, so no extra file is needed — the failing variants in `AgentList.fixtures.ts` reproduce the composition-seam crash autonomously.

### Typed seam (`infra-typed-seams`)

`npm run gen-types` spawns a one-shot Python process in the `awm` mamba env that imports `awm.exposed:app` and calls `app.openapi()` directly. No live uvicorn required, no auth wall. Output goes to `frontend/src/lib/api/generated.ts` (committed). Spawn cwd + `sys.path` are pinned to the worktree root so `import awm` resolves to the worktree's source, not the editable install (see memory `awm_two_source_trees`).

Hand-written interfaces in `client.ts` get progressively replaced by re-exports from `generated.ts`. The first proof-of-seam is `VagrantSessionResponse`. The migration is intentionally narrow — types that match 1:1 swap immediately; types that diverge in shape stay hand-written until the backend tightens its `response_model` declarations.

Engine `CONFIG_SCHEMA` JSON Schemas escape this pipeline (FastAPI types their envelope as `dict[str, Any]` → `unknown`). Fixtures for engine forms hand-shape JSON Schema blobs; if drift becomes a real problem, a future `infra-engine-schema-snapshot` scope can close it.

### Workflow

```bash
# Visual: see fixtures in the browser
cd frontend && npm install
npm run dev      # http://localhost:12103/ui/dev

# Autonomous: fail CI on crash-on-mount bugs
npm run test

# Regenerate types after Pydantic model changes
npm run gen-types
npm run check
```

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

## Service Hub

`awm.exposed:app` (port 7820) is a routing layer. Most requests are served by its in-process routers (`/rooms`, `/peer`, `/voice`, …). A few path prefixes are *registered* at runtime; matched requests are either forwarded to an external service or served from a registered directory.

### When to make a `svc-*` scope

Use a `svc-*` worktree when a stripe needs to **own** a path prefix end-to-end: ship its own routes, run as its own process, iterate without rebuilding the monolith. PTT V2 (audio + WS + STT) is the first customer. Pure shared-library refactors stay in `awm/`.

### Registration lifecycle

Two registration kinds share the same CLI + lease lifecycle.

**URL forward** — for `svc-*` processes that own a prefix end-to-end:

```bash
# in the svc-* worktree, with the service running on a chosen port:
awm hub register --name <svc> --prefix </owned-path> --url http://127.0.0.1:<port>
```

**Static dir** — for frontend slices that compile a bundle and want it reachable on the hub origin without running a dev server through the hub:

```bash
# in the comp-* worktree, after building (e.g. vite build → ./dist):
awm hub register --name <comp> --prefix </comp-path> --dir ./dist \
  [--entry main.js] [--css style.css] [--mount-id app]
```

If the registered directory has no `index.html`, the hub renders a minimal ESM shell at the prefix root: an empty `<div id="<mount-id>"></div>`, optional CSS `<link>`s, and a `<script type="module" src="<prefix>/<entry>">`. Drop an `index.html` into the directory to take over entirely.

Both forms POST to `/hub/register`, then hold a WS lease at `/hub/lease/<id>` until you Ctrl-C. Lease close → eviction within the next event-loop tick. No file-based config, no heartbeat tuning. Re-running re-registers from scratch.

Other commands:
- `awm hub list` — show current registrations (includes `kind: url|static`).
- `awm hub deregister <name>` — admin force-evict.
- `awm hub trust-self` — install the local auth token at `$AWM_DIR/peers/<self>.token` so the hub's forwarded requests pass `require_peer_bearer` on the service side. Run once per node. Only needed for URL-kind registrations.

### Auth model

Hub → service is degenerate peer auth (URL kind only). The hub injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>` on every forwarded request; the user's bearer (`Authorization` header / `awm_session` cookie) is stripped. `X-Awm-As` is preserved verbatim. Services gate routes with `from awm.middleware_auth import require_peer_bearer` — one import, no new bearer concept.

Static-kind registrations don't proxy, so there's no second-hop auth — bytes are served by the hub directly, subject to whatever middleware sits in front of the hub itself. WS connections to a static prefix are closed with code 1003.

### What `comp-*` and frontend slices need to know

**Nothing about consuming the hub.** The hub IS `awm.exposed:app` on :7820; no new origin, no new port. With an empty registry the behavior is byte-identical to a hub-less awm.

To **publish** a built component through the hub: `awm hub register --dir <dist>` after building. No need to wire up a port or a dev server — the hub serves the files.

### Demos

- `awm/demos/echo_svc.py` — 60-line FastAPI smoke test; copy as the starting point for a real `svc-*`.
- `awm/demos/static_demo/` — naked `main.js` + `style.css` bundle; copy as the starting point for a `comp-*` registration. README has the one-liner.

### Constraints

- **Never run two hubs on one node.** Both would bind :7820 and one would fail.
- The hub control plane (`/hub/*`) is exempt from forwarding and rejected at registration — `--prefix /hub` and `/hub/*` return 409. The lease socket has to stay reachable.
