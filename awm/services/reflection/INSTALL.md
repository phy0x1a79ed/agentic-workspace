# Installing the `reflection` service

A Python feature service in the `awm.reflection` namespace. It lets a terminal
agent — an interactive Claude Code / OpenCode session, or an awm-spawned agent —
run a slash-command **on itself** by pasting it into its own tmux pane. The
headline case is `/compact`: an agent whose context is filling up can compact
itself, which it otherwise cannot do (the TUI only takes `/compact` as typed
input).

It owns **no database** and depends only on the shared component libraries it
imports (`config`, `gatewayclient`).

## How it works

The service pastes the command into a target tmux pane using bracketed paste and
presses Enter — the exact sequence awm already uses to drive spawned agents. It
sends **no Escape**, so the command *queues* behind the agent's current turn and
runs the instant that turn ends (which is the only safe moment to `/compact`).

**Follow-up prompt (kept alive).** A bare slash command runs at end-of-turn and
then leaves the session idle with nothing to do — for an autonomous agent that is
death. So whenever `send` submits a slash command it also queues a **follow-up
prompt** behind it (default `"Continue with what you were doing."`, override with
`followup`), giving the session a real turn once the command completes. Plain
prompts are their own turn and get no follow-up.

**Modal guard.** Some commands open a blocking modal/picker (`/mcp`, `/status`,
`/config`, `/permissions`, `/agents`, …, and bare `/model`). These *swallow*
pasted input — a follow-up Enter doesn't escape them and for a navigable list it
drills deeper — so they would freeze the session (only a hand-typed Esc recovers).
`send` **refuses** them outright (this cannot be overridden by `confirm`). Run
them by hand. `/model opus` (with an argument) acts directly and is allowed.

Because the service is a separate gateway-spawned process, it does not share the
caller's environment — but the calling agent never has to work around that. The
per-session `awm-mcp` proxy sits inside the caller's own tmux pane and forwards
`$TMUX_PANE` as a header (`X-Awm-Tmux-Pane`); the gateway fills it in as the
default `pane` before the call ever reaches this service. **A normal call
passes no `pane` at all.** `pane` remains an accepted argument only as a manual
override (a human driving `reflection` from a plain shell/CLI with no `awm-mcp`
proxy in front of it, or deliberately targeting a pane other than their own) —
best-effort auto-detection is still the fallback there when `pane` is omitted
and no header is present, but is only reliable when a single agent pane exists.

Before pasting, the resolved pane is also checked to actually be running a
`claude`/`opencode` process — a stale or wrong pane id is refused outright
rather than silently pasted into whatever happens to be there.

Not in tmux (bare terminal, IDE extension, web) → no pane to drive; `send`
returns a clear error.

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

Two verbs, both on MCP + CLI + HTTP:

- `reflection_send` — paste any text/slash command into a pane and submit it.
  Destructive commands (`/clear`, `/quit`, `/exit`) require `confirm=true`; modal
  commands (`/mcp`, `/status`, bare `/model`, …) are refused. A submitted slash
  command is trailed by a `followup` prompt to keep the session alive.
- `reflection_compact` — sugar for `send "/compact"` (with the same follow-up).

Example (the normal case — no pane, no pid, nothing agent-specific to know):

    reflection(verb="compact")
    reflection(verb="send", args={text: "/model opus"})

Manual override (a human at a shell, or deliberately targeting another pane):

    reflection(verb="compact", args={pane: "%32"})

## Not tmux-hosted at all

An agent with no tmux pane at all (an IDE extension session, a claude.ai web
session, an SDK-driven session with no terminal) has nothing for `reflection` to
inject into — there is currently no non-tmux mechanism to push input into a
running Claude Code session from an external process. `reflection` returns a
clear error in this case rather than guessing.

## Iterating

    awm dev shadow awm/services/reflection

execs this same `run.sh` as an overlay against a running sandbox hub.
