# Installing the `discord` service

A Python feature service in the `awm.discord` namespace. It needs the `awm`
conda env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

The Discord bot also needs its token in the workspace env (`<AWM_DIR>/env`, e.g.
`DISCORD_BOT_TOKEN=...`) — see the service's own `config.py` for the variables it
reads.

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
`awm dev shadow awm/services/discord`; it execs this same `run.sh` as an overlay.
