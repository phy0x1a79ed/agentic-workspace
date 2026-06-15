# Installing the `orchestrator` service

A Python feature service in the `awm.orchestrator` namespace. It is awm's
**coordination kernel**: it holds the durable plan (a DAG of tasks) and the
deterministic state machine that drives a project to completion across parallel
scopes. It needs the `awm` conda env to contain its package plus the shared
component libraries it imports (`config`, `persistence`, `gatewayclient`).

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

## Surface

Three **public** tools are projected into the MCP catalog (`/tools`):
`orch_task_attach`, `orch_status`, `orch_frontier`.

Four **privileged** plan-mutation ops — `claim`, `deliver`, `fail`,
`decompose_commit` — are intentionally **omitted from the manifest**, so they are
NOT MCP tools. They remain reachable by the agents harness via the catch-all
`POST /svc/orchestrator/fn/<op>` dispatch (which resolves against the `HANDLERS`
dict, not the manifest). This manifest-omission is the worker-honesty mechanism.

To iterate against a running sandbox without installing, use
`awm dev shadow awm/services/orchestrator`; it execs this same `run.sh` as an overlay.
