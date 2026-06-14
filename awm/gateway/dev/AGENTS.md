# Dev Harness Workspace

This directory is **not** a real AWM workspace — it exists only to anchor
`awm.config.find_workspace_root()` here when developers run the web-ui locally
against an isolated backend. All runtime state (`.awm/`, `projects/`, `data/`)
lives next to this file and is gitignored.

## Quick start

```bash
./run.sh                # start uvicorn (HTTP loopback)
./run.sh restart        # stop + start
./run.sh stop
./run.sh status         # what's running, plus URLs
./run.sh seed           # (re)seed the sandbox via the live HTTP API
./run.sh reset          # wipe state and re-seed (prompts first)
./run.sh logs           # tail uvicorn log
```

## Three scopes in parallel

This sandbox runs in **every** `projects/awm/<scope>/` worktree. Each scope
gets a distinct port band derived from its directory name, so all three can
run side-by-side:

| Scope worktree | uvicorn |
|---|---|
| `projects/awm/dev/` (`dev` branch — **integration**) | `7821` |
| `projects/awm/web-ui/` (`feat/web-ui`) | `7831` |
| `projects/awm/web-backend/` (`feat/web-backend`) | `7841` |
| any other scope (fallback) | `7851` |

**The uvicorn port IS the service-hub origin for this sandbox.** `AGENTS.md`
§ "Service Hub" documents the hub at `:7819` (the production default — the
systemd-managed `awm.service` on this node). When you're consuming or
registering against the hub *from inside a dev sandbox*, use this worktree's
uvicorn port instead. The hub control plane lives at
`http://127.0.0.1:<uvicorn>/hub/` (e.g. `http://127.0.0.1:7821/hub/register`
for the `dev/` sandbox).

Each worktree's `dev/.awm/`, `dev/projects/`, `dev/data/` are gitignored —
state is fully isolated. `./run.sh start` in three different worktrees Just
Works.

### Workflow

- Feature work happens on `feat/web-ui` or `feat/web-backend`. Live-test
  inside that worktree's `dev/` sandbox.
- Merging up: PRs land on `dev`. The `projects/awm/dev/` worktree is the
  canonical integration test target — run its sandbox to verify a merge.

### Per-worktree overrides

Drop a gitignored `dev/.env` next to `run.sh` for per-worktree settings.
It's `source`d before port defaults are computed:

```bash
# dev/.env (gitignored)
AWM_PORT=9000
AWM_API_TARGET=http://127.0.0.1:7841
```

Environment variables exported in the shell take precedence over `.env`,
which takes precedence over the scope-band defaults.

## One process (per scope)

| Process | URL (web-ui scope) | Purpose |
|---|---|---|
| uvicorn | `http://127.0.0.1:7831/` | the hub origin; serves backend routes + per-page stripes at `/ui/<name>/` |

Plain HTTP loopback — auth is gone, the listener trusts every caller.

## What's seeded

`seed.py` is a thin stdlib-only HTTP client against the running uvicorn
(no `awm.*` imports). It creates:

- Projects `demo`, `playground`
- Scopes `demo/alpha`, `demo/beta`, `playground/experiment`
- One demo room with two posts (via `/invoke` → `room_create` / `room_post`)

## Production-code workarounds (parked in `run.sh`)

Two quirks of `awm.services.projects.create_project` need environment
hacks. They live in `run.sh` so `seed.py` doesn't have to know about them:

- **`gh` shim**: `create_project` shells out to `gh repo create` if `gh` is
  on PATH. `run.sh` puts a no-op `gh` shim earlier on PATH to suppress that.
- **`init.defaultBranch=main`**: `git init --bare` honours the operator's
  default branch; if not `main` the subsequent worktree-add fails. `run.sh`
  exports `GIT_CONFIG_KEY_0=init.defaultBranch GIT_CONFIG_VALUE_0=main`.

## CLI vs harness

The user's shell typically has `AWM_WORKSPACE=/home/tony/agentic_workspace`
exported, which makes `awm <cmd>` from anywhere hit **production** (port
7819). To target the sandbox:

```bash
AWM_PORT=7831 awm scope list           # pick the band for your scope (web-ui=7831, dev=7821, web-backend=7841)
AWM_WORKSPACE=$(pwd) awm scope list    # alternative — runs against this sandbox's .awm/
```

The harness talks to the loopback port directly; no token, no cookie.

## Page stripes

The web UI is composed of per-page stripes under `../packages/pages/<name>/`
(Svelte 5 + Vite), each registered with the hub as `kind=page` and served at
`/ui/<name>/`. The active pages are `agent`, `tts`, `stt`, and
`primitives-gallery`.

`./run.sh start` builds the pages (`npm run build --workspaces`) into `dist/`
as the hub comes up. See the root `README.md` § *Developing a package* for the
authoring workflow.

## Don't use this directory as an agent CWD

It has no real projects, no real data, and exists purely as a sandbox for
the operator UI.
