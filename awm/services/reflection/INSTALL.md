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
caller's environment. **The caller passes its own pane** as the `pane` argument
(get it with `echo $TMUX_PANE`). Best-effort auto-detection is attempted when
`pane` is omitted, but is only reliable when a single agent pane exists.

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

Example (from an agent that knows it is in pane `%32`):

    reflection(verb="compact", args={pane: "%32"})
    reflection(verb="send", args={text: "/model opus", pane: "%32"})

## Note: making the pane implicit (future)

Requiring the caller to pass `$TMUX_PANE` is the price of keeping this a pure
drop-in service. To make the pane implicit, the per-session `awm-mcp` proxy
(which *does* inherit the caller's `TMUX_PANE`) would forward it as a header,
mirroring its existing `AWM_AS`→`X-Awm-As` block — but that edits shared gateway
infrastructure and is intentionally out of scope here.

## Iterating

    awm dev shadow awm/services/reflection

execs this same `run.sh` as an overlay against a running sandbox hub.
