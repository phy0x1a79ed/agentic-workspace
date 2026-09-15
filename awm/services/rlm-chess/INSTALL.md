# The `rlm-chess` service

## Purpose & Contents

This file covers what the code does not show about the chess realm: how to install it, what it depends on outside awm, and which settings must agree. The verb surface is the manifest in `awm/rlm_chess/hub_adapter.py`. The live list is `rlm(verb="describe")`. The arena's control API is `PROTOCOL.md` in the arena checkout.

## What the realm is

`rlm-chess` serves chess arena sessions through the realm-family contract. The arena engine is not in awm. It lives in the `projects/game-bot/chess` checkout. The service starts arena daemons, talks to them over their Unix sockets, and re-emits their watch streams on the `chess` topic. A session id is the arena's five-character sid.

A player name that starts with `@` is an external seat. A person or an agent plays it with `move`. The `/ui/chess/` page is one such client.

## Install

1. Create the arena's conda env: `dev/chess.sh env` in the arena checkout.
2. Run `CHESS_ARENA_ROOT=<arena checkout> bash install.sh`.

`install.sh` installs the arena editable into the `awm` env, which also installs python-chess. It writes `.runtime-env` with the arena checkout and the `chess` env interpreter. `run.sh` exports both in either launch mode.

**CAUTION:** `install.sh` also installs the shared component libraries editable from this checkout. Run it only from the checkout the gateway runs. On a dev sandbox, install the arena alone with `mamba run -n awm pip install -e <arena checkout>` and write `.runtime-env` by hand.

**CAUTION:** Point `CHESS_ARENA_ROOT` at a stable checkout, not a feature worktree. Daemons and bots start from that tree, so deleting it breaks running sessions.

## State and restarts

- **Data:** arena saves and session records live in `.awm/services/rlm-chess/arena/`, never in a git worktree.
- **Sockets:** `/tmp/chess-arena-<uid>-<tag>/`, where the tag hashes the workspace. A sandbox and prod therefore never see each other's sessions.
- **Restarts:** daemons are detached and outlive the service. On start the service re-watches every live session labelled `owner: rlm-chess`.
- **Lifetime:** a waiting session with no caller and no watcher saves itself as `autosave-<sid>` and exits after `AWM_CHESS_IDLE_TIMEOUT_S` (default 1800). A finished session exits after `AWM_CHESS_FINISHED_LINGER_S` (default 900).

`CHESS_ARENA_DATA` and `CHESS_ARENA_RUNTIME` override the data and socket directories. Every client of a session must agree on both, including an arena CLI used for debugging.

## Test

Run from this directory, with the awm dist roots and this service on `PYTHONPATH`:

`CHESS_PYTHON=<chess env python> mamba run -n awm python -m pytest tests`

The tests start real daemons in a throwaway directory under `/tmp`.
