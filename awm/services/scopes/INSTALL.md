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

## Optional dependency: `dvc`

The data layer (`awm.scopes.data_dvc`) shells out to the `dvc` **binary**. It is
deliberately optional: without it every data path falls back to the shared
symlink rather than failing, so the service starts and scopes are created either
way — they just don't get versioned, isolated data.

Install it into its own env (keeping it out of the `awm` env avoids a slow solve
there):

    mamba create -n dvc -c conda-forge dvc

Resolution order is `AWM_DVC_BIN` → `PATH` → the known mamba envs
(`~/lib/miniforge3/envs/{dvc,awm}/bin/dvc`) → `/usr/{local/,}bin`. The service
runs under systemd's minimal PATH, so **set `AWM_DVC_BIN` explicitly** if it
lives anywhere unusual.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_DVC_BIN` | — | absolute path to `dvc`; wins over every other lookup |
| `AWM_DATA_DVC` | `1` | global kill switch — `0` forces every project back to the shared symlink |

The cache at `<workspace>/data/.dvc_cache` and `<workspace>/projects/` **must be
on the same filesystem**: materialised files are hardlinks into that cache, and
across a filesystem boundary DVC silently falls back to real copies, so every
scope pays full price for its data.

The off-site half of the cache — the append-only sync to chinook and the
per-scope selective restore — belongs to the `dvc` *service*, not here. See
`awm/services/dvc/INSTALL.md`.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/scopes`; it execs this same `run.sh` as an overlay.
