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
```

## Two processes

| Process | URL | Purpose |
|---|---|---|
| uvicorn | `https://127.0.0.1:7821/ui/` | the actual app + /ui SPA (self-signed cert) |
| login-server | `http://127.0.0.1:7822/` | bookmark page that mints a fresh `/auth/bootstrap?ot=...` link on every refresh |

Bookmark **http://127.0.0.1:7822/** in the browser. Refresh it any time
you need a new session — each nonce is single-use, 60s TTL.

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
AWM_EXPOSED_PORT=7821 awm scope list           # easiest — skips workspace discovery
AWM_WORKSPACE=$(pwd) awm scope list             # alternative — reads dev/.awm/exposed.json
```

The harness itself uses the loopback bearer in `.awm/auth.token` directly,
so it never needs the operator's CLI.

## Don't use this directory as an agent CWD

It has no real projects, no real data, and exists purely as a sandbox for
the operator UI.
