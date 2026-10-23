# Installing the `reporting` service

## Purpose & Contents

This file is the operator's contract for the `reporting` service: what it is,
where it runs, the board it keeps, the hooks that drive it, the environment it
reads, and how to test it. It does not hold the verb list (`awm operations
--help` renders it from the live manifest) or the intake flow, which
`FEDERATION.md` § *Intake* describes. The service publishes into the shared
`operations` tool beside `board` and the gateway's peer book.

## What this is

Two parts in one service.

**Task reporting** runs on every fleet node. Each node keeps a SQLite ledger of
the tasks its sessions work on and shows them on the Swarm board in Trilium. The
ledger is the source of truth and the board only displays it. Claude Code hooks
and three agent verbs (`task_claim`, `task_track` and `request_human`, whose
`kind` is `blocked`, `review` or `approval`) drive it. The reconcile loop closes a task when its
session ends, and a session's journal posts append to its card.

**The front door** runs intake only where `AWM_FRONT_DOOR=1`. It listens to
the federation board for cards addressed to this swarm, keeps them in a SQLite
queue as Proposed, and sends Tony a Discord DM per card. It types wake lines and
watched events into this node's sessions, and it consumes the `/reject` and
`/new-agent` Discord commands. An agent claims a card with `task_claim` and
completes it on the board. The front door never claims or completes a card.

## When it runs

The service is installed on every node. A station (`AWM_NODE_ROLE=station`)
logs a refusal and runs nothing. A fleet node tracks tasks. A fleet node also
runs the front door when `AWM_FRONT_DOOR=1` is set in the node's env file,
`<workspace>/.awm/env`.

**WARNING** Set the flag on exactly one node per swarm. Nothing in code stops a
second front door. Two front doors both DM every card, and both act on every
Discord command.

## The Swarm board

Run `operations board_ensure` once, on any node, and again after a deploy that
changes the board's shape. It makes "Swarm board" under the AWM note, with the
columns Proposed, Planned, Active, Blocked, Pending review, Done and Rejected, a
"Ledger" note and a "By tag" book. It gives Done and Rejected each an "Older" book, labelled
`#swarmOlder`, and the newest-first sort. It also makes the private "Ops" book under AWM, labelled
`#swarmOps`. Every node finds the board and the book by their labels and refuses
while they are missing, so no node ever makes a second copy. Tony copies tasks
into Proposed.

The board makes every child book a column, so the Ledger and By tag show as
extra columns. Label either note `#archived` to hide it.

Each sweep writes a card's effective scope tags (`PROTOCOLS.md` § *Scope
lifecycle*) to its `swarmTags` chip, and gives each tag in use a saved search
under By tag. A tag change never moves a card. **CAUTION** Trilium's search
parser breaks a `%=` regex that holds a comma, and the match then reads as a
substring, so "la" finds "lab". Keep commas out of the tag searches.

A session claims a card in either of two ways. It calls `task_claim(ref)`. Or
a prompt to it holds `claim task <ref>`, or "claim" or "attach" plus a Trilium
note URL, which the prompt hook turns into the same claim with no tool call. A
ref that names a federation card claims that card (`FEDERATION.md` §
*Intake*), and its note joins Planned. A URL outside the board clones that note
into Proposed first.

A session with no card gets an owned card on the first of these events:

1. It writes a plan in plan mode, or its plan is approved. The card opens in
   Planned.
2. It calls `request_human`.
3. It calls `task_track`, which opens the card in Active, or returns the card it
   already has.
4. A job that cx started with the lifecycle brief (cx `INSTALL.md` §
   *A worker gets a lifecycle brief*) sends its first prompt. The card opens in
   Active. The sweep opens it for a briefed job whose first prompt missed the
   gateway. A kept seat never gets the brief, so it stays off the board.

Tony's own sessions get no card for starting. An event that opens a card returns
a notice that names the card and the board's protocol. The hook hands it to the
agent as `additionalContext`.

**CAUTION** A worker can finish inside one sweep, which is why its first prompt
opens the card. That prompt runs before cx writes the job's lineage record, so
cx lists the job from its pending record until then.

A card holds one piece of work. A plan written while the card waits in Pending
review, or after it closed, opens a new card. The reviewed card moves off the
session's key to `<key>/<session id>-<task id>` and keeps its plan and its
column. A plan written in Active or Blocked revises the same card.

- A card is a note under a column. A move adds the new column branch and removes
  only the old one, so a clone on Tony's own board stays. The service never calls
  `note_move`.
- A card is one of two kinds, recorded in the ledger's `owned` column. The
  service makes an **owned** card when a session has no task to claim: a text
  note labelled `#agentManaged` and `#readOnly`, whose body is the record. A
  **claimed** card is Tony's own note. Its body stays his and is never written;
  the record goes in a text child called "Record" with the same two labels. The
  per-node notes under "Ledger" are Markdown children, and the notes under the
  private "Ops" book carry the same labels.
- The record is HTML. A status block comes first (state, agent, scope, peer, job,
  waiting-on reason, started, closed). Plan, Todo, Journal and Removed follow as
  closed `trilium-collapsible` blocks, split by rules; Removed appears only once an item has been removed. A summary is plain text
  and carries a count, as in "Todo — 3/7 done, 1 active". The plan converts
  with `markdown-it-py`, which escapes raw HTML: its first `#` heading is
  dropped and the rest starts at `<h3>`. A journal entry is an `<h4>` and its
  body starts at `<h5>`.
- While a background session holds an open card, the Agent line names it. The
  name links to the session's claude.ai page, or shows `claude attach <job>`
  while Remote Control is off. A change of name or link rewrites only the
  card's body, never its column, so a drag by Tony stands. An interactive
  session gets no Agent line.
- The Todo shows a pending item as an empty box and a completed item as a
  ticked box. An active item is an empty box with a bold subject and "(in
  progress)", because neither Markdown nor Trilium draws a half-state.
- An owned card is titled by the plan's `#` heading, else the job's name when
  `nameSource` is `auto` or `user` and the name does not start with `claimed `, else the
  intent's first line cut at a word boundary to 80 characters. Every sync
  recomputes it, because Claude Code names a session after its first turn. A
  claimed card keeps Tony's title.
- A node writes only the cards of its own tasks. Trilium resolves conflicts per
  note, last write wins, and syncs to deneb about every 60 s.
- A card that Tony deletes from the board closes its task as cancelled. A card
  that Tony drags to another column is recorded as an event and left there. A
  drag to Done or Rejected closes the task.
- Done and Rejected list their cards newest first by `cardStateAt`, which a card
  gets on entering a column. A federation card keeps the federation board's
  value. A card the service made, or a federation card, moves into its column's
  "Older (n)" book 14 days after its task closed there. Tony's own claimed note
  stays. The sweep keeps the count in each book's title.
- **CAUTION** A card with no `cardStateAt` sorts first. `board_ensure` dates
  each undated card in Done and Rejected from its Trilium modified time.
- The sweep reads a card in an Older book as off the board. An open card that
  Tony drags into one closes its task as cancelled.
- The sweep never closes a background task. A job that leaves the daemon's
  roster while its directory under `~/.claude/jobs` remains keeps its card in
  its column, flagged "not running, resumable". An idle retire and a daemon
  death both cause this. A job whose state record says `stopped`, or whose
  directory is gone, moves to Pending review, flagged "stopped". cx writes
  `stopped` on `stop_other`, because `claude stop` does not. The flag clears
  when the job runs again.
- **CAUTION** A daemon restart drops dead workers from `cx list`. Do not read
  a missing row as a finished session.
- A closed task is filed under its node's Ledger note. An interactive session
  that exits closes its task, except a card in Pending review.
- A journal post that names its session (`meta.session`, which the journal
  sets from the gateway's caller pid) joins that session's card only. A post
  with no session joins every open card in its project and scope.

## Hooks

Claude Code hooks report plans, plan approval, todos, prompts and `/clear` to the
local `reporting`. Install them on every fleet node with `awm/scripts/deploy-hooks.sh`.
Run it with `--check` to report drift. A restricted session ignores user settings,
so cx passes it the hook fragment `~/.claude/hooks/awm-reporting-settings.json`
through `--settings`.

A `SessionStart` hook on source `clear` calls `hook_session_clear`, which files
the session's open task under the key `<job>/<old session id>`. The next plan in
that job then opens a card of its own, and the sweep closes the filed task. The
hook ignores `compact` and `resume`, which also fire `SessionStart`. A task
lookup is by exact key, never by prefix.

The prompt hook runs on every prompt and calls the gateway only for a claim or
when the session's task is Blocked. `reporting` writes the marker
`~/.claude/awm-reporting/<session_id>.blocked` when it blocks a task, and the hook
deletes it when a result says the task is no longer blocked.

## Environment

| Env var | Meaning |
|---|---|
| `AWM_FRONT_DOOR` | `1` runs the front door on this node |
| `AWM_SWARM` | this swarm's name. The front door takes in cards addressed to it (default `tony`) |
| `AWM_BOARD_URL`, `AWM_BOARD_TOKEN` | the federation board's door and this swarm's party token. Without them the front door queues nothing |
| `AWM_REPORTING_DB` | the database file (default: `<workspace>/.awm/services/reporting/reporting.db`) |
| `AWM_REPORTING_STATE` | where the reconcile lock lives (default: beside the database) |
| `AWM_REPORTING_INTERVAL_S` | seconds between reconcile ticks, which also sweep the tasks (default 60) |
| `AWM_REPORTING_CATCHUP_S` | seconds between board catch-up reads and between journal catch-ups (default 300) |
| `AWM_REPORTING_REMIND_S` | seconds an urgent request stays blocked before Tony gets one reminder (default 3600) |
| `AWM_REPORTING_DM_ACCOUNT` | the `social` account that carries card DMs and urgent requests (default: the node's operator DM account). `AWM_SOCIAL_PEER` names the node that runs `social` |
| `AWM_REPORTING_QUIET_SWARMS` | swarms, comma- or space-separated, whose cards with this swarm are queued but never DMed to Tony, such as a standing channel a session already reads (default: none). Thread messages never DM Tony either way |
| `AWM_REPORTING_TRILIUM_URL` | where Tony opens a note from, without the `#`, for the link in that message (default: this node's edge URL plus `/trilium`) |
| `AWM_REPORTING_NEW_AGENT_PROJECT`, `AWM_REPORTING_NEW_AGENT_SCOPE` | where `/new-agent` starts a session (default `awm`, `dev`). The lifecycle brief tells the agent to move to its work's scope |
| `AWM_REPORTING_MESSAGE_BACKLOG_S` | how old a message card may be and still be queued by a catch-up (default 3 days) |
| `AWM_APPROVAL_CHANNEL` | `account:channel` where admin approval requests are posted (default on swarm `tony`: `slack-bot:C0C9XNR77ML`, #arbutus). Unset elsewhere, which turns approvals off |
| `AWM_APPROVAL_ADMINS` | `slack-user-id=name,…`, the people whose reply decides a request (default on swarm `tony`: Tony) |
| `AWM_ADMIN_APPROVE` | the approve script (default `~/.ssh/awm-admin-approve`). A node without it takes no approvals |
| `ARB_BUNDLES`, `AWM_APPROVAL_PENDING` | where agents write bundles (default `~/.local/share/arbutus-approvals`) and where the frozen copies wait (default `~/.ssh/admin-approvals/pending`) |

## Task identity

Every hook and agent verb names its session by the pid that the gateway stamps on
the call (`X-Awm-Caller-Pid`), resolved on this node from Claude Code's session
record. `session_id` is only a check against that record. A background session is
keyed by its job id and an interactive one by pid plus start time. A call with no
calling session is refused.

## The front door

The front door keeps its queue, stream cursor, per-card thread cursors and
watchers in the reporting database. It never starts or keeps a session. A
wake line or a watched event goes over the Claude Code daemon's PTY socket
(`awm.claudedaemon.job`) into a background session on this node. A session at a
modal prompt is skipped, and a missed wake line gets one retry.

It consumes `social`'s `command` topic. `social` drops a command from anyone
outside its operators table, so a command that arrives here comes from Tony.

- `/reject <ref> [reason]` rejects the card and posts the reason on its thread.
- `/new-agent [prompt]` starts a session named `agent-xxxx` through cx `start` (the verb
  behind `reflection start_other`) with Remote Control on, and answers with the session's
  `https://claude.ai/code/session_…` link. The answer overwrites the "Starting an agent…" ack.
  It quotes the first three lines of the prompt, up to 300 characters. It attaches a longer
  prompt whole as `prompt.txt`, which Discord shows collapsed. The first prompt holds Tony's
  words, a hint to rename the session with `reflection rename`, and the account and channel
  to answer in. cx adds the lifecycle brief, with `started_by` "Tony from Discord".

The reconcile loop holds a non-blocking lock on `<state>/reconcile.lock` for
the life of the process, so a second copy of the service sweeps nothing.

## Admin approvals

`request_human` with `kind: approval` asks an Arbutus admin to approve a bundle
for the approve script. It runs on the agent's own node, front door or not,
because the approve script and the ssh gate it opens live there. The service
copies the bundle into the pending directory, which the admin-gate hook keeps
sessions out of, and posts it to the approval channel as a quiet thread, so
the channel's seat holder never sees the replies. An admin's `approve` or
`deny` in that post's thread decides it. Approving runs the approve script on
the frozen copy with the admin named as approver.

The thread reply is the only grant. No verb approves.

**CAUTION** Agents can post as Tony through his own Slack account. `social`
marks a message it sent through a person's account as `relayed`, and the
handler ignores it, so a reply only counts when it comes from Tony's Slack
client. Driving his Slack in a browser bypasses `social` and this check.

**CAUTION** The handler hears replies on `social`'s live `message` topic. A
reply sent while the service restarts is lost. The request stays pending
until an admin replies again or it expires after 24 hours.

## Ops notices

`ops_post(kind, node, title, body)` keeps the latest notice per kind and node as
one managed note in the `#swarmOps` book. Services and domestic peers call it.
Each note opens with "Changed: <ISO 8601 time with the node's UTC offset>" and
carries the same time in a `#changed` label, and a replacement updates both.
`auth` posts each password mint there instead of to Discord. The note sits
behind the same Trilium login that the mint grants, so a logged-out Tony reads
it from a session on another node.

## Converting existing cards

`operations convert` is hidden and runs once per node, from the rollout
runbook. It re-renders the node's open cards in the current shape, deletes an
owned card's Record child, adds the labels, retitles, and stamps each Ops notice
that has no `#changed` label from the note's Trilium modified time. It defaults to a dry run that
lists every action and writes nothing. Pass `dry_run: false` to write.

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
