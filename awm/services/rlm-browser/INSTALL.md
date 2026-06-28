# Installing the `rlm-browser` service

The first **realm service** in awm's `rlm-*` family — a Chrome/CDP browser
session pool that web-game effectors drive through the hub. A Python feature
service in the `awm.rlm_browser` namespace. It needs the `awm` conda env to
contain its package plus the shared component libraries it imports (`config`,
`persistence`, `gatewayclient`).

> **Status: skeleton.** Handlers are stubs — `acquire` mints a fake `session_id`,
> `observe` returns a placeholder snapshot, act verbs return acks. Real Chrome/CDP
> and the VPN netns + kill-switch come in later passes (see this scope's
> `.awm/context.md`).

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
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name, `rlm-browser`) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/rlm-browser`; it execs this same `run.sh` as an overlay.

## Realm-family contract

Functions are projected into the gateway catalog as `rlm_browser_<verb>` tools:

- **lifecycle** — `acquire(game, opts?) -> {session_id}` · `release(session_id)` ·
  `reset(session_id)` · `status(session_id?)`
- **perceive** — `observe(session_id) -> {snapshot, screenshot?}`
- **act** — `navigate` / `click` / `type` / `key` / `wait`
- **emitters** — `rlm.browser.<event>` carrying `{session_id, kind, data}` (topic
  `browser`); e.g. `rlm.browser.captcha_detected`, `rlm.browser.error`.
