# Installing the `rlm-factorio` service

The **Factorio realm** in awm's `rlm-*` family — it owns a self-contained
Factorio appliance (a Docker container running a stdlib supervisor that owns the
engine) and manages realm sessions over the shared realm-family contract. A
Python feature service in the `awm.rlm_factorio` namespace. It needs the `awm`
conda env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`), and it needs **Docker** on the host
(the engine lifecycle is `docker compose`-driven).

> **Status: lifecycle + world ops are LIVE; live-control is stubbed.**
> `acquire` builds/starts the appliance and waits for the engine; `world_new` /
> `world_save` / `world_load` drive the supervisor (sacred-saves invariant —
> only `world_save` writes a named `.zip`); `release` tears the container down
> (the saves volume survives). `observe` / `exec_lua` / `pause` are declared in
> the contract but return an honest stub — they need RCON, which is a later pass
> (see this scope's `.awm/context.md`).

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
| `AWM_SERVICE_NAME` | gateway | this service's name (= folder name, `rlm-factorio`) |
| `AWM_SERVICE_ID` | gateway | assigned on respawn so reconnect targets the same control URL |

No auth — the registration handshake carries no token.

To exercise the service against a throwaway gateway without touching prod, use
`scratchpad/rlm_harness.sh` (boots an isolated gateway pointed at a temp services
tree holding only this service; `--docker` also drives a live appliance round-trip).

## The appliance (Docker)

`appliance/` holds the container that owns the engine, ported from the game-bot
POC:

- `Dockerfile` — Debian + pinned **Factorio 2.0.77** headless + Space Age mods.
- `docker-compose.yml` — parameterized by `FACTORIO_*` env (single-session
  defaults: project `rlm-factorio`, container `rlm-factorio-appliance`, ports
  `12140/udp` game + `12142/tcp` control, volume `rlm-factorio-saves`).
- `supervise.py` — stdlib-only supervisor: `GET /status`, `POST /save|/new|/load`.
- `config/` — server-settings, map-gen, mod-list.

First `acquire` builds the image (downloads a large Factorio tarball — minutes).
**Multiplayer is UDP-only on the game port**; a TCP-only Windows→WSL portproxy
will not carry it — rely on Docker's port publishing / a UDP forward.

## Realm-family contract

Functions are projected into the gateway catalog as `rlm_factorio_<verb>` tools:

- **lifecycle** — `acquire(game, opts?) -> {session_id}` · `release(session_id)` ·
  `reset(session_id)` · `status(session_id?)`
- **perceive** — `observe(session_id) -> {snapshot, screenshot?}` *(stub)*
- **act** — `world_new(session_id, seed?)` · `world_save(session_id, name, overwrite?)` ·
  `world_load(session_id, name)` · `pause(session_id, paused)` *(stub)* ·
  `exec_lua(session_id, code)` *(stub)*
- **emitters** — `rlm.factorio.<event>` carrying `{session_id, kind, data}` (topic
  `factorio`); e.g. `rlm.factorio.world_loaded`, `rlm.factorio.error`.
