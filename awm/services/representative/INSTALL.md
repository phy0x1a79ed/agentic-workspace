# Installing the `door` service

## Purpose & Contents

This file is the operator's contract for the `door` service: what it is, when
it runs, the environment it reads, and how to test it. It does not hold the verb
list (`awm door --help` renders it from the live manifest) or the
representative's instructions (`awm/representative/personas.py`).

## What this is

The swarm's front door. It listens to the federation board for cards addressed
to this swarm, keeps them in a SQLite queue, wakes the representative session,
and keeps one representative and one secretary session alive. The
representative triages the queue and hands each card to a domestic agent. The
agent completes the card on the board. The door never does.

## When it runs

The service is installed on every node and does nothing unless both hold:

- `AWM_FRONT_DOOR=1` is set in the node's env file, `<workspace>/.awm/env`.
- The node's role is `fleet`. A station (`AWM_NODE_ROLE=station`) logs a refusal
  and does nothing, even with the flag.

**WARNING** Set the flag on exactly one node per swarm. Nothing in code stops a
second door. Two doors both claim cards, so each card goes to whichever claims
first, and each door starts its own representative.

## Environment

| Env var | Meaning |
|---|---|
| `AWM_FRONT_DOOR` | `1` enables the door |
| `AWM_SWARM` | this swarm's name. The door claims cards addressed to it (default `tony`) |
| `AWM_BOARD_URL`, `AWM_BOARD_TOKEN` | the board's door and this swarm's party token. Without them the door still keeps its sessions alive and queues nothing |
| `AWM_DOOR_DB` | the queue file (default: the standard service state path, `<workspace>/.awm/services/door/door.db`) |
| `AWM_DOOR_STATE` | where the reconcile lock lives (default: beside the queue) |
| `AWM_DOOR_INTERVAL_S` | seconds between reconcile ticks (default 60) |
| `AWM_DOOR_CATCHUP_S` | seconds between board catch-up reads (default 300) |
| `AWM_DOOR_NOTIFY_BATCH_S` | seconds a new card waits for others before the representative is woken (default 5) |
| `AWM_DOOR_NOTIFY_RETRY_S` | seconds before a failed wake is retried (default 30) |
| `AWM_DOOR_REANNOUNCE_MIN` | minutes a card may stay queued before the representative is told again, "N still waiting" (default 10) |
| `AWM_DOOR_WORK_PROJECT`, `AWM_DOOR_WORK_SCOPE` | where delegates start when a card names no scope (default `awm`, `door-work`). The door creates the scope with `scope_create` on a tick when its worktree is missing, and retries on the next tick after a failure |
| `AWM_DOOR_MESSAGE_BACKLOG_S` | how old a message card may be and still be queued by a catch-up (default 3 days) |

## How it keeps its sessions

Each tick calls `cx list`. A session counts as the representative or the
secretary only if the door started it (the door records the job id `cx start`
returns, in the queue database) and it carries the role's `mode` label. A
renamed session still counts. Another session carrying the label is logged and
ignored. If the queue database is lost, a surviving session is no longer
recognised, and the replacement start is refused while it still holds the name. A missing role is started with the launch config in
`personas.py`, which always passes an explicit `permission`. The door never
stops, renames or takes a session. When `cx list` fails the tick does nothing.

The reconcile loop holds a non-blocking lock on `<state>/reconcile.lock` for
the life of the process, so a second copy of the service reconciles nothing.

## How it wakes the representative

When cards arrive, the door types one line into the representative's session
over the Claude Code daemon's PTY socket (`awm.claudedaemon.job`): "N new
cards, run door list". The line never carries card text. A session at a modal
prompt, or in any status other than idle or busy, is skipped and retried. A
card that stays queued is announced again after `AWM_DOOR_REANNOUNCE_MIN`.

## Install

    bash install.sh

It editable-installs the shared components, the board client and this service
into the `awm` env and writes the gitignored `.runtime-env` sidecar.

## Tests

    PYTHONPATH=awm/services/representative:awm/services/board:awm/service_components/config:awm/service_components/persistence:awm/service_components/gatewayclient:awm/service_components/claudedaemon \
      mamba run -n awm python -m pytest -p no:cacheprovider awm/services/representative/tests

The `awm` env's editable installs point at the deployed tree, so the explicit
`PYTHONPATH` is what makes the run use this checkout. The board is on the path
for `awm.board.client`.
