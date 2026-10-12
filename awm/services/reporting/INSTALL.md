# Installing the `reporting` service

## Purpose & Contents

This file is the operator's contract for the `reporting` service: what it is,
where it runs, the environment it reads, the one-time migration from the `door`
service, and how to test it. It does not hold the verb list (`awm reporting
--help` renders it from the live manifest) or the personas' instructions
(`awm/reporting/personas.py`).

## What this is

Two parts in one service.

**Task reporting** runs on every fleet node. Each node keeps a SQLite ledger of
the tasks its sessions work on and shows them on the Swarm board in Trilium. The
ledger is the source of truth and the board only displays it. Claude Code hooks
and three agent verbs (`claim_task`, `request_human_user`,
`request_human_review`) drive it. The reconcile loop closes a task when its
session ends, and journal posts from a task's scope append to its card.

**The front door** runs only where `AWM_FRONT_DOOR=1`. It listens to the
federation board for cards addressed to this swarm, keeps them in a SQLite queue,
wakes the representative session, and keeps one representative and one secretary
session alive. The representative triages the queue and hands each card to a
domestic agent. The agent completes the card on the board. The door never does.

## When it runs

The service is installed on every node. A station (`AWM_NODE_ROLE=station`)
logs a refusal and runs nothing. A fleet node tracks tasks. A fleet node also
runs the front door when `AWM_FRONT_DOOR=1` is set in the node's env file,
`<workspace>/.awm/env`.

**WARNING** Set the flag on exactly one node per swarm. Nothing in code stops a
second door. Two doors both claim cards, so each card goes to whichever claims
first, and each door starts its own representative.

## The Swarm board

Run `reporting board_ensure` once, on any node. It makes "Swarm board" under the
AWM note, with the columns Proposed, Planned, Active, Blocked and Pending review
and a "Ledger" note. Every node finds the board by its `#swarmBoard` label and
refuses task verbs while it is missing. Tony copies tasks into Proposed.

- A card is a note under a column. A move adds the new column branch and removes
  only the old one, so a clone on Tony's own board stays. The service never calls
  `note_move`.
- A card's body is Tony's and is never written. The service writes only a
  Markdown child called "Record" (Metadata, Plan, Todo with a Removed
  subsection, Journal), the per-node notes under "Ledger", and the notes under
  the private "Ops" book. All of them carry `#agentManaged` and `#readOnly`.
- A node writes only the cards of its own tasks. Trilium resolves conflicts per
  note, last write wins, and syncs to deneb about every 60 s.
- A card that Tony deletes from the board closes its task as cancelled. A card
  that Tony drags to another column is recorded as an event and left there.
- A task whose session ended is filed under its node's Ledger note. A background
  session that died without a stop stays on the board, flagged as ended.

## Environment

| Env var | Meaning |
|---|---|
| `AWM_FRONT_DOOR` | `1` runs the front door on this node |
| `AWM_SWARM` | this swarm's name. The door claims cards addressed to it (default `tony`) |
| `AWM_BOARD_URL`, `AWM_BOARD_TOKEN` | the federation board's door and this swarm's party token. Without them the door still keeps its sessions alive and queues nothing |
| `AWM_REPORTING_DB` | the database file (default: `<workspace>/.awm/services/reporting/reporting.db`) |
| `AWM_REPORTING_STATE` | where the reconcile lock lives (default: beside the database) |
| `AWM_REPORTING_INTERVAL_S` | seconds between reconcile ticks, which also sweep the tasks (default 60) |
| `AWM_REPORTING_CATCHUP_S` | seconds between board catch-up reads and between journal catch-ups (default 300) |
| `AWM_REPORTING_REMIND_S` | seconds an urgent request stays blocked before Tony gets one reminder (default 3600) |
| `AWM_REPORTING_DM_ACCOUNT`, `AWM_REPORTING_DM_CHANNEL` | the `social` account and channel that carry an urgent request (default: the account and DM that `auth` uses for the password push). `AWM_SOCIAL_PEER` names the node that runs `social` |
| `AWM_REPORTING_TRILIUM_URL` | where Tony opens a note from, without the `#`, for the link in that message (default: this node's edge URL plus `/trilium`) |
| `AWM_REPORTING_NOTIFY_BATCH_S` | seconds a new card waits for others before the representative is woken (default 5) |
| `AWM_REPORTING_NOTIFY_RETRY_S` | seconds before a failed wake is retried (default 30) |
| `AWM_REPORTING_REANNOUNCE_MIN` | minutes a card may stay queued before the representative is told again, "N still waiting" (default 10) |
| `AWM_REPORTING_WORK_PROJECT`, `AWM_REPORTING_WORK_SCOPE` | where delegates start when a card names no scope (default `awm`, `door-work`). The door creates the scope with `scope_create` on a tick when its worktree is missing, and retries on the next tick after a failure |
| `AWM_REPORTING_MESSAGE_BACKLOG_S` | how old a message card may be and still be queued by a catch-up (default 3 days) |

## Task identity

Every hook and agent verb names its session by the pid that the gateway stamps on
the call (`X-Awm-Caller-Pid`), resolved on this node from Claude Code's session
record. `session_id` is only a check against that record. A background session is
keyed by its job id and an interactive one by pid plus start time. A call with no
calling session is refused.

## How the front door keeps its sessions

Each tick calls `cx list`. A session counts as the representative or the
secretary only if the door started it (the door records the job id `cx start`
returns, in the database) and it carries the role's `mode` label. A
renamed session still counts. Another session carrying the label is logged and
ignored. If the queue database is lost, a surviving session is no longer
recognised, and the replacement start is refused while it still holds the name. A missing role is started with the launch config in
`personas.py`, which always passes an explicit `permission`. The door never
renames or takes a session. When `cx list` fails the tick does nothing.

A session counts as alive only when it can work. `reporting status` gives each role
`sessions.<role>` with `alive`, `state` and `reason`. `representative_alive` and
`secretary_alive` stay. The state comes from the `state`, `needs` and `detail`
fields of the role's `cx list` row. Claude Code sets `blocked` and `failed` from
API errors and also from the session's own last line, so the `needs` text decides
which kind it is.

| State | Meaning | What the door does |
|---|---|---|
| `running` | `working`, `done`, a `failed:` line the session wrote itself, or a state the door does not know. Alive | Nothing. |
| `waiting` | `blocked` with a rate limit, overload, server error, a question or any text the session wrote. It clears on the next prompt. Alive | Nothing. |
| `blocked` | A person must act: `auth_required` (login expired), org OAuth not allowed, account on hold, cloud credentials, verification, usage limit | One warning per change of state. A restart cannot fix a login. When it clears, the door announces the queued cards again at once. |
| `failed` | `failed` with the `API error` text | Stops the session, which the door started, and starts a new one. At most one restart per back-off period (60 s, doubling to 15 min). |
| `exited`, `missing` | No live session | Starts one, backing off after a failed start. |
| `unknown` | cx could not answer | Nothing. |

The reconcile loop holds a non-blocking lock on `<state>/reconcile.lock` for
the life of the process, so a second copy of the service reconciles nothing.

## How it wakes the representative

When cards arrive, the door types one line into the representative's session
over the Claude Code daemon's PTY socket (`awm.claudedaemon.job`): "N new
cards, run reporting card_list". The line never carries card text. A session at a modal
prompt, or in any status other than idle or busy, is skipped and retried. A
card that stays queued is announced again after `AWM_REPORTING_REANNOUNCE_MIN`.

## The sessions it starts

The two sessions are named `representative - <node>` and `secretary - <node>` and
run Opus at medium effort. The representative is restricted and ignores user
settings. The secretary is not, so Tony's settings load for it, and the sessions
it starts are normal workers. The door pins each session it starts in
`~/.claude/jobs/pins.json`, under the lock the Claude CLI uses. When a session
replaces another, or the daemon retired the old one, the door runs `claude rm`
on the old job.

## Ops notices

`ops_post(kind, node, title, body)` keeps the latest notice per kind and node as
one managed note in a private "Ops" book under the AWM note. Services and
domestic peers call it. The note sits behind the same Trilium login that the
notice may grant.

## Migrating from the `door` service

The database moves from `.awm/services/door/door.db` to
`.awm/services/reporting/reporting.db` and gains the ledger tables. Run it once,
with the service stopped and before its first start:

    PYTHONPATH=... mamba run -n awm python -m awm.reporting.migrate

It copies the file with SQLite's backup call, leaves the source in place, opens
the copy at the current schema and refuses to overwrite a destination. Stop the
live representative and secretary first and restart them from the new service:
a restart alone keeps the old persona, because the door adopts a live session
that carries the role's mode. Remove the stale `awm-representative` editable
install and rename `AWM_DOOR_*` entries in the node's env file to
`AWM_REPORTING_*`.

## Install

    bash install.sh

It editable-installs the shared components, the board client and this service
into the `awm` env and writes the gitignored `.runtime-env` sidecar.

## Tests

    PYTHONPATH=awm/services/reporting:awm/services/board:awm/service_components/config:awm/service_components/persistence:awm/service_components/gatewayclient:awm/service_components/claudedaemon \
      mamba run -n awm python -m pytest -p no:cacheprovider awm/services/reporting/tests

The `awm` env's editable installs point at the deployed tree, so the explicit
`PYTHONPATH` is what makes the run use this checkout. The board is on the path
for `awm.board.client`.
