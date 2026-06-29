# Installing the `rlm-factorio` service

The **Factorio realm** in awm's `rlm-*` family — it owns a self-contained
Factorio appliance (a Docker container running a stdlib supervisor that owns the
engine) and manages realm sessions over the shared realm-family contract. A
Python feature service in the `awm.rlm_factorio` namespace. It needs the `awm`
conda env to contain its package plus the shared component libraries it imports
(`config`, `persistence`, `gatewayclient`), and it needs **Docker** on the host
(the engine lifecycle is `docker compose`-driven).

> **Status: lifecycle + world ops + live-control are LIVE; emitters pending.**
> `acquire` builds/starts the appliance and waits for the engine; `world_new` /
> `world_save` / `world_load` drive the supervisor (sacred-saves invariant —
> only `world_save` writes a named `.zip`); `release` tears the container down
> (the saves volume survives). `observe` / `exec_lua` / `pause` and the player-body
> verbs (`body_spawn` / `body_move` / `body_stop`) are LIVE over RCON, backed by
> the baked-in `game-bot-control` mod. The `factorio` emitter is declared but not
> fired yet (the events pass — see this scope's `.awm/context.md`).

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

- `Dockerfile` — Debian + pinned **Factorio 2.1.8** headless + Space Age mods.
- `docker-compose.yml` — parameterized by `FACTORIO_*` env (single-session
  defaults: project `rlm-factorio`, container `rlm-factorio-appliance`, ports
  `12140/udp` game + `12142/tcp` control, volume `rlm-factorio-saves`).
- `supervise.py` — stdlib-only supervisor: `GET /status`, `POST /save|/new|/load`.
- `config/` — server-settings, map-gen, mod-list.

First `acquire` builds the image (downloads a large Factorio tarball — minutes).
**Multiplayer is UDP-only on the game port**; a TCP-only Windows→WSL portproxy
will not carry it — rely on Docker's port publishing / a UDP forward.

## Joining the world as a desktop / Steam client

A human can connect their own Factorio client to the running appliance and play
alongside the agent. `server-settings.json` ships with **no password, no
account verification, LAN visibility on, unlimited players**, so the only
barriers are version + mods.

**Three things must match the server, or the join is refused:**

1. **Exact engine version.** The image is pinned (currently **2.1.8**, the
   *experimental* branch). The client must be the same build — in Steam,
   *Factorio → Properties → Betas →* opt into the matching version.
2. **The Space Age expansion.** The server enables `space-age` + `quality` +
   `elevated-rails` + `recycler` (all DLC). The client must **own** the
   expansion — these mods can't be downloaded, only owned.
3. **The `game-bot-control` mod.** This is the catch: it's a **private local
   mod, not on the mod portal**, so Factorio **cannot auto-sync it on join** (the
   server doesn't push non-portal mod files). The client must install it by hand.

### Installing `game-bot-control` on a client

Package the mod into a portal-shaped zip (top folder `game-bot-control_<ver>/`
containing `info.json` + `control.lua`) — emitted to `appliance/dist/`:

    python3 - <<'PY'
    import json, zipfile
    src = "appliance/mods/game-bot-control"
    ver = json.load(open(f"{src}/info.json"))["version"]
    dist = f"game-bot-control_{ver}"
    with zipfile.ZipFile(f"appliance/dist/{dist}.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("info.json", "control.lua"):
            z.write(f"{src}/{f}", f"{dist}/{f}")
    PY

Then drop the zip into the client's **mods folder** (do *not* unzip — Factorio
reads mod zips directly) and enable it. The folder location is whatever the
client's `config-path.cfg` resolves to; with the default
`use-system-read-write-data-directories=true` it's the OS user-data dir:

- **Windows:** `%APPDATA%\Factorio\mods` (`C:\Users\<you>\AppData\Roaming\Factorio\mods`)
- **Linux:** `~/.factorio/mods`

Enable it in that folder's `mod-list.json` (`{"name":"game-bot-control","enabled":true}`)
— Factorio only rescans the mods folder at **startup**, so fully relaunch the
client after copying. The mod is pure control-stage script (no prototypes), so
its checksum matches whether the server runs it from a directory and the client
from a zip.

### Connect

With version + DLC + mod aligned: *Multiplayer → Connect to address →*
**`localhost:12140`** (Docker Desktop forwards the published UDP port to the
Windows host's loopback). From another LAN device, use the host's LAN IP +
`:12140` and allow inbound **UDP 12140** through the host firewall. The agent
body is a separate `character`; a joining human spawns as their own player.

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
