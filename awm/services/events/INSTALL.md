# Installing the `events` service

A Python feature service in the `awm.events` namespace. It needs the `awm` conda
env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`). It has **no** third-party runtime
deps — the cron/interval parsing is stdlib-only (`awm/events/cron.py`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the only three env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/events`; it execs this same `run.sh` as an overlay.

## What it does

- **functions:** `schedule{game, cron}`, `unschedule{game}`, `list` — projected
  into the gateway catalog as `events_schedule` / `events_unschedule` /
  `events_list`.
- **emitter:** `schedule.tick{game}` — fired on cadence (jittered), fanned out
  on `/svc/events/emit/schedule.tick`. The `agents` service subscribes to wake a
  bot for that game.

### Cron syntax

Two forms are accepted by the `cron` field:

- **Standard 5-field cron** — `min hour dom month dow` with `*`, `*/step`,
  ranges (`a-b`), and lists (`a,b,c`). Minute granularity, local time. Example:
  `*/5 * * * *` (every 5 minutes).
- **Interval** — `@every <n><unit>` where unit is `s`, `m`, or `h`. Sub-minute
  granularity, useful for demos/tests. Example: `@every 30s`.
