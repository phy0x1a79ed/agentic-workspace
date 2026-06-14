# Installing the `dev` service

A Python feature service in the `awm.dev` namespace. It owns everything
`awm dev` does on the bus (start / stop / restart / seed / status) and is the
service that *self-shadows* onto prod when a dev sandbox starts it. It needs the
`awm` conda env to contain its package plus the `gatewayclient` component.

## Install

    bash install.sh

`install.sh` editable-installs `gatewayclient` and this service into the `awm`
env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the env vars the adapter reads:

| Env var | Set by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | gateway | base URL of the running gateway |
| `AWM_SERVICE_NAME` | gateway | this service's name (= `dev`) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |
| `AWM_SHADOW_HUB_URL` | **dev sandbox only** | prod hub url to self-shadow `/svc/dev` onto |

`AWM_SHADOW_HUB_URL` is the dev-only signal: when the dev sandbox's gateway sets
it (to `http://127.0.0.1:7819/`), this service ALSO registers a self-cleaning
overlay of `/svc/dev` on prod, so `awm dev <op>` aimed at prod reaches the live
sandbox. Prod's own gateway never sets it → prod's `dev` is a plain base.

No auth — the registration handshake carries no token.
