# Installing the `reflection` service

A Python feature service in the `awm.reflection` namespace. It lets an agent run
a slash-command **on itself** by typing it into its own prompt. The headline case
is `/compact`: an agent whose context is filling up can compact itself, which it
otherwise cannot do (the TUI only takes `/compact` as typed input).

It owns **no database** and depends only on the shared component libraries it
imports (`config`, `gatewayclient`).

## How it works

The service types the command into the caller's own prompt using bracketed paste
and presses Enter. It sends **no Escape**, so the command *queues* behind the
agent's current turn and runs the instant that turn ends (which is the only safe
moment to `/compact`).

**Follow-up prompt (kept alive).** A bare slash command runs at end-of-turn and
then leaves the session idle with nothing to do — for an autonomous agent that is
death. So whenever `send` submits a slash command it also promises a **follow-up
prompt** (default `"Continue with what you were doing."`, override with
`followup`), giving the session a real turn once the command completes. Plain
prompts are their own turn and get no follow-up.

The follow-up is **deferred, not co-queued**: co-queuing lets an active agent run
the resume first, on the old context, starving `/compact` of the idle slot it
needs. Instead a watcher waits for the command to visibly finish and injects the
resume then. Two consequences fall out of that, and both have bitten:

- The watcher must not fire on the brief idle beat *before* compaction starts —
  hence the reacted-then-settled test rather than the first idle sample.
- The watcher is a thread, the wait can run for minutes, and the gateway
  restarts this service routinely. So the promise is **written to disk before the
  watcher starts** (`pending.py`) and replayed on boot; without that, a restart
  inside the wait left the session idle forever with nobody able to tell. The
  record carries the caller's `procStart`, and a replay whose pid no longer
  matches is dropped rather than delivered — a pid outlives nothing.
  `reflection_pending` lists what is currently owed.

**Modal guard.** Some commands open a blocking modal/picker (`/mcp`, `/status`,
`/config`, `/permissions`, `/agents`, …, and bare `/model`). These *swallow*
pasted input — a follow-up Enter doesn't escape them and for a navigable list it
drills deeper — so they would freeze the session (only a hand-typed Esc recovers).
`send` **refuses** them outright (this cannot be overridden by `confirm`). Run
them by hand. `/model opus` (with an argument) acts directly and is allowed.

## Who gets typed into

Reflection acts on the caller and on nobody else, so **nothing in the surface
takes a target**. Identity is not accepted as an argument at all; it is observed.
`awm-mcp` runs as a stdio child of the session that calls it, so the gateway
stamps that proxy's parent pid onto every reflection call as `_caller_pid` —
always overwriting, and stripping it when there is no header, so a model cannot
name a different session.

`session_target` turns that pid into a target via Claude Code's own per-session
record at `~/.claude/sessions/<repl-pid>.json`, which says how the session is
hosted:

| `kind` | Backend | Reached via |
|---|---|---|
| `interactive` | `tmux_inject` | the pane whose process subtree contains the pid |
| `bg` | `daemon_inject` | the PTY socket in `~/.claude/daemon/roster.json` |

Both are exact lookups. Anything that does not resolve — no session record, a
recycled pid whose `procStart` disagrees with `/proc`, an interactive session not
under tmux, a background session with no roster entry — **refuses**. There is no
fallback that picks a plausible session, and adding one back would be a bug:

> Reflection once re-scanned the tmux server for "the current agent pane" just
> before injecting a deferred resume, ranking candidates by `#{pane_activity}`.
> That format does not exist, so every candidate tied and the lowest-numbered
> pane always won — delivering resumes into *other agents'* prompts. Rank panes
> by nothing. The caller's identity is the only vote.

Two joins are easy to get wrong and are load-bearing. A background session is
matched on **`jobId`**, not `sessionId`: a `/clear` mints a new session id in
place while the roster keeps the one the job was dispatched with, so joining on
the session id lost such a session permanently — and told it its PTY host had
exited, which sent readers chasing a live process. The match is then validated by
asking whether the roster entry's `bg-pty-host` actually contains the caller,
since that `pid` is the host and never the REPL. And because session records are
keyed by pid, `procStart` must agree with `/proc/<pid>/stat` before a record is
trusted.

Callers with no proxy in front of them (a human at a plain shell) have no
identity and are refused. `reflection_whoami` reports what the service resolved,
which is the quickest way to see why.

## Reaching a background session

A pty has exactly one master. tmux owns it for terminal sessions; `claude
bg-pty-host` owns it for background ones — so the two backends have disjoint
reach by construction, and neither can substitute for the other.

The daemon publishes each session's pty on a unix socket. Frames are a four-byte
big-endian payload length, one type byte, then the payload — **the length
excludes the type byte**, and assuming otherwise misparses everything after the
first frame. Type `0x01` is a JSON control frame, `0x00` is raw pty bytes. Reading
is unauthenticated; writing requires a control frame carrying the session's
`ptyAuth` first, or input is silently dropped. Tokens and socket paths are minted
per worker and do not survive a respawn, so both are read fresh per call.

This is a private protocol read out of the CLI bundle, not a documented
interface. The greeting is shape-checked on connect so a CLI update that moves it
degrades to a clear "background reflection unavailable" rather than misfiring;
the tmux backend is unaffected either way.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Run

You never invoke the service by hand. The gateway discovers this folder (any
folder with a `run.sh` under `awm/services/`), starts it with `bash run.sh`, and
injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

## Surface

Five verbs, all on MCP + CLI + HTTP, none of which takes a target:

- `reflection_send` — type any text/slash command into your own prompt and submit
  it. Destructive commands (`/clear`, `/quit`, `/exit`) require `confirm=true`;
  modal commands (`/mcp`, `/status`, bare `/model`, …) are refused. A submitted
  slash command is trailed by a `followup` prompt to keep the session alive.
- `reflection_compact` — sugar for `send "/compact"` (with the same follow-up).
- `reflection_mode` — put your own session back into bypass-permissions mode.
- `reflection_pending` — list the deferred resumes still owed, node-wide. The
  one verb that is not caller-scoped, because its whole job is to make a *lost*
  promise visible from outside the session that lost it.
- `reflection_whoami` — report which session reflection resolves you to.

The normal call carries nothing agent-specific, on either hosting kind:

    reflection(verb="compact")
    reflection(verb="send", args={text: "/model opus"})

The guard tables live in `guards.py` rather than in either backend, because what
may be injected is a property of the agent TUI and not of the transport — it
would be a nasty surprise if `/clear` were guarded on one path and not the other.

## Permission mode after a plan approval

Approving a plan does not restore the mode a session was launched with. It
restores the mode the session was in immediately *before* it entered plan mode,
and an approval arriving from the phone carries a pre-plan mode of `auto` — so
the session lands in auto, where a classifier gates every action.

No hook output can fix this: the hook contract exposes per-call allow/deny and
permission *rules*, but the session mode is internal state with no external
setter. `reflection_mode` therefore does what a human would — presses the
Shift+Tab permission-mode cycle — driven from a `PostToolUse` hook matching
`ExitPlanMode` (which fires only when the tool actually executed, so an approval
and never a rejection).

It never counts presses. The cycle is `default → acceptEdits → plan →
bypassPermissions → auto → default`, `bypassPermissions` and `auto` appear only
when available in that session, and overshooting parks the session in *plan
mode*. So it reads the mode off the TUI footer, presses once, and reads again;
if bypass is never offered it walks back to where it started and says so. A
footer it cannot read means a modal is covering it — that refuses without
pressing anything, for the same reason the modal guard exists.

Footer strings are TUI copy and can move under a CLI update; they fail safe,
since an unrecognised footer reads as unknown and unknown does not act.

One bounded gap, background sessions only: `default` paints no indicator, and
that backend reads an append-only byte stream rather than a rendered screen, so
a session sitting in `default` can read back as whatever mode last painted one.
Output is discarded before each keypress, which fixes every read during a walk;
only the first read can still be stale, and the worst outcome is that such a
session is left alone rather than switched. Nothing repaints the footer on demand
— an empty bracketed paste and a cursor key were both tried — so closing it
properly means rendering the stream through a terminal emulator. The tmux backend
is unaffected, and that is the path a phone-approved plan actually takes.

## Sessions reflection cannot reach

A session with neither a tmux pane nor a daemon-hosted pty — an IDE extension
session, a claude.ai web session, an SDK-driven session with no terminal — has no
input channel an external process can write to. `reflection` returns a clear
error rather than guessing.

## Iterating

    awm dev shadow awm/services/reflection

execs this same `run.sh` as an overlay against a running sandbox hub.
