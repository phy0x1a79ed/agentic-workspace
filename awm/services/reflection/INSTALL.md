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
  Destructive commands (`/clear`, `/quit`, `/exit`) require `confirm=true`.
- `reflection_compact` — sugar for `send "/compact"`.

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
