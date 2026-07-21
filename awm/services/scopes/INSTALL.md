# Installing the `scopes` service

A Python feature service in the `awm.scopes` namespace. It needs the `awm`
conda env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`).

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

## Optional dependency: `git-annex`

The data layer (`awm.scopes.data_annex`) needs the `git-annex` **binary**, not a
Python package. It is deliberately optional: without it every data path falls
back to the pre-annex shared symlink rather than failing, so the service starts
and scopes are created either way — they just don't get isolated, versioned
data.

Install it into its own env (it is a large Haskell binary; keeping it out of the
`awm` env avoids a slow solve there):

    mamba create -n annex -c conda-forge git-annex

Resolution order is `AWM_ANNEX_BIN` → `PATH` → the known mamba envs
(`~/lib/miniforge3/envs/{annex,awm}/bin/git-annex`) → `/usr/bin`. The service
runs under systemd's minimal PATH, so **set `AWM_ANNEX_BIN` explicitly** if it
lives anywhere unusual.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_ANNEX_BIN` | — | absolute path to `git-annex`; wins over every other lookup |
| `AWM_DATA_ANNEX` | `1` | global kill switch — `0` forces every project back to the shared symlink |
| `AWM_DATA_ANNEX_AUTOGET_MAX` | `20000` | above this many tracked files a clone is left lazy instead of hardlinking all content in |

Content is hardlinked from `<workspace>/data/`, so that directory and
`<workspace>/projects/` **must be on the same filesystem** — otherwise git-annex
silently falls back to real copies and every scope pays full price for its data.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/scopes`; it execs this same `run.sh` as an overlay.
