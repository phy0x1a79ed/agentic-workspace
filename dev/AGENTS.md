# Dev Harness Workspace

This directory is **not** a real AWM workspace — it exists only to anchor
`awm.config.find_workspace_root()` here when developers run the web-ui locally
against an isolated backend. All runtime state (`.awm/`, `projects/`, `data/`)
lives next to this file and is gitignored.

## Quick start

```bash
./run.sh                # start uvicorn (HTTPS) + login bookmark server
./run.sh restart        # stop + start
./run.sh stop
./run.sh status         # what's running, plus URLs
./run.sh seed           # (re)seed the sandbox via the live HTTP API
./run.sh reset          # wipe state and re-seed (prompts first)
./run.sh login          # one-line CLI form of the login URL
./run.sh logs           # tail uvicorn log
./run.sh frontend       # Vite dev server for ../frontend (HTTP, port 12103)
./run.sh build          # production SvelteKit build → ../awm/static/
```

## Three scopes in parallel

This sandbox runs in **every** `projects/awm/<scope>/` worktree. Each scope
gets a distinct port band derived from its directory name, so all three can
run side-by-side:

| Scope worktree | uvicorn | login | Vite |
|---|---|---|---|
| `projects/awm/dev/` (`dev` branch — **integration**) | `7821` | `7822` | `12103` |
| `projects/awm/web-ui/` (`feat/web-ui`) | `7831` | `7832` | `12113` |
| `projects/awm/web-backend/` (`feat/web-backend`) | `7841` | `7842` | `12123` |
| any other scope (fallback) | `7851` | `7852` | `12153` |

Each worktree's `dev/.awm/`, `dev/projects/`, `dev/data/` are gitignored —
state is fully isolated. `./run.sh start` in three different worktrees Just
Works.

### Workflow

- Feature work happens on `feat/web-ui` or `feat/web-backend`. Live-test
  inside that worktree's `dev/` sandbox.
- Merging up: PRs land on `dev`. The `projects/awm/dev/` worktree is the
  canonical integration test target — run its sandbox to verify a merge.

### Cross-scope live test

The frontend dev can point their Vite proxy at **any** running uvicorn by
exporting `AWM_API_TARGET` before `./run.sh frontend`. Default is the same
worktree's own uvicorn (solo mode). Example: have a web-backend dev's
in-progress backend serve the frontend dev's UI:

```bash
# in projects/awm/web-ui/dev
AWM_API_TARGET=https://127.0.0.1:7841 ./run.sh frontend
```

Caveat: the browser must have an authed cookie for the target backend, so
visit `http://127.0.0.1:7842/` (web-backend's login bookmark) once first.

### Per-worktree overrides

Drop a gitignored `dev/.env` next to `run.sh` for per-worktree settings.
It's `source`d before port defaults are computed:

```bash
# dev/.env (gitignored)
AWM_EXPOSED_PORT=9000
AWM_API_TARGET=https://127.0.0.1:7841
```

Environment variables exported in the shell take precedence over `.env`,
which takes precedence over the scope-band defaults.

## Two processes (per scope)

| Process | URL (web-ui scope) | Purpose |
|---|---|---|
| uvicorn | `https://127.0.0.1:7831/ui/` | the actual app + /ui SPA (self-signed cert) |
| login-server | `http://127.0.0.1:7832/` | bookmark page that mints a fresh `/auth/bootstrap?ot=...` link on every refresh |

Bookmark the login URL for your scope. Refresh it any time you need a new
session — each nonce is single-use, 60s TTL.

## What's seeded

`seed.py` is a thin stdlib-only HTTP client against the running uvicorn
(no `awm.*` imports). It creates:

- Projects `demo`, `playground`
- Scopes `demo/alpha`, `demo/beta`, `playground/experiment`
- One demo room with two posts (via `/invoke` → `room_create` / `room_post`)
- A fake remote peer `peer-test` (via `awm peer add` CLI; ping will fail
  with an ssh-resolve error — exercises the UI error path)
- A local peer identity `dev-sandbox` (with its token copied into
  `peers/dev-sandbox.token` so pinging self works)

## Why HTTPS, why self-signed

`awm.exposed` sets cookies with `secure=True`, so a plain-HTTP browser
silently drops them. `_prep.py` calls `auth_svc.bootstrap_tls()` to generate
a loopback cert under `.awm/tls/`. Your browser will warn — click through.

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
12100). To target the sandbox:

```bash
AWM_EXPOSED_PORT=7831 awm scope list           # pick the band for your scope (web-ui=7831, dev=7821, web-backend=7841)
AWM_WORKSPACE=$(pwd) awm scope list             # alternative — reads dev/.awm/exposed.json
```

The harness itself uses the loopback bearer in `.awm/auth.token` directly,
so it never needs the operator's CLI.

## Frontend (SvelteKit)

The web UI source lives in `../frontend/` — a SvelteKit 2 + Svelte 5 + Tailwind
v4 + bits-ui project. The production build is emitted to `../awm/static/` and
served by uvicorn at `/ui/`. See `../frontend/` for component layout and design
tokens (DM Sans/Mono, dark-only, `--atomizer` blue accent).

- `./run.sh frontend` — runs Vite dev on `0.0.0.0:$VITE_PORT` (scope-derived;
  web-ui=12113, dev=12103, web-backend=12123) with API/WS proxied to the
  worktree's own uvicorn. Override `AWM_API_TARGET` to point at a different
  scope's backend. Vite runs over HTTP, so cookies set with `Secure=True`
  will not flow through — visit the target backend's login URL once to mint
  a session before relying on the dev server for authed calls.
- `./run.sh build` — `cd ../frontend && npm install && npm run build`. The
  cutover replaces `awm/static/{index.html, _app/, mic-worklet.js, favicon.svg}`
  on every build; `awm/static/login.html` is server-rendered and must be
  preserved by hand if you wipe the directory.

The backend SPA fallback lives in `awm/exposed.py` next to the static dir
declaration: it serves real assets directly and falls back to `index.html`
for any unknown `/ui/<path>` so deep links survive hard reloads.

## Don't use this directory as an agent CWD

It has no real projects, no real data, and exists purely as a sandbox for
the operator UI.
