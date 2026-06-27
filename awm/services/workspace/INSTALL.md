# Installing the `workspace` service

A Python feature service in the `awm.workspace` namespace. It owns the **DAG
execution unit** — the directory a placed agent runs in: a workdir holding the
brief (`CONTEXT.md`), read-only input pre-readings (`inputs/<name>`), and
deliverable staging (`deliverable/<contract>/`). It is the runtime sandbox the
orchestrator's agents service provisions placements through, deliberately
decoupled from git and the CLI-only `scopes` service (no git, no branch, no
channel).

It needs the `awm` conda env to contain its package plus the shared component
libraries it imports (`config`, `persistence`, `gatewayclient`).

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env (override with `AWM_ENV=<name>`) and writes a gitignored `.runtime-env`
sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so the gateway can
respawn the service under systemd's minimal PATH (where `mamba` is not present).

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder (any folder with a `run.sh` under `awm/services/`), starts it with
`bash run.sh`, and injects the three env vars the adapter reads (`AWM_HUB_URL`,
`AWM_SERVICE_NAME`, `AWM_SERVICE_ID`). No auth — the registration handshake
carries no token.

Units live under `<AWM_DIR>/services/workspace/units/<project>/<unit_slug>/`.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/workspace`; it execs this same `run.sh` as an overlay.

## Operations

| Tool | Meaning |
|---|---|
| `workspace_create` | provision (or re-activate) a unit dir + materialize pre-readings |
| `workspace_retain` | free a unit but keep its contents (mark `idle`) |
| `workspace_destroy` | remove a unit dir + row (terminal cleanup) |
| `workspace_resolve` | return a unit's path + state (recover a workdir on respawn) |
