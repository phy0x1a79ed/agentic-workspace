# Installing the `rlm-factorio` service

## Purpose & Contents

This file covers installing, building and running the Factorio realm, the two
kinds of world it hosts and their headless toggle, the seat model the service is
built around, how game calls reach the engine and what they cost, how a mod
change ships, and how a person joins the world from their own Factorio client.

It does not list verbs or their parameters. `awm factorio --help` answers that
from the running manifest and cannot go stale. What is written here is what
reading the code will not tell you: why the shape is what it is, and which two
things must agree.

## What this service is

A Python feature service in the `awm.rlm_factorio` namespace, in awm's `rlm-*`
realm family beside `rlm-browser`. It owns Factorio **worlds**, each a Docker
appliance — a container running a stdlib supervisor that owns the engine — and
hands agents **seats** in them.

It needs the `awm` conda env to hold its package and the shared component
libraries it imports (`config`, `persistence`, `gatewayclient`). It needs Docker
on the host, because the engine lifecycle is `docker compose`-driven.

## Install

    bash install.sh

`install.sh` editable-installs the component libraries and this service into the
`awm` env. Override the env with `AWM_ENV=<name>`. It also writes a gitignored
`.runtime-env` sidecar baking `AWM_PYTHON` to the env's absolute interpreter, so
the gateway can respawn the service under systemd's minimal PATH, where `mamba`
does not exist.

## Build the appliance image

    bash appliance/build.sh

The image carries the **full** Factorio build, not the headless one, and the full
build is behind factorio.com account auth. Put credentials in
`appliance/secrets/factorio_creds` (gitignored, mode 600):

    FACTORIO_USER=<factorio.com username>
    FACTORIO_TOKEN=<service token from that account>

The token is the same one a desktop install keeps in its `player-data.json`.
`build.sh` passes it as a BuildKit secret mount, so it reaches no layer and no
log line. Point `FACTORIO_CREDS` at another file to use one.

**CAUTION:** the download variant is `expansion`, not `alpha`. `alpha/linux64`
ships only `base` and `core`, so a client built from it is refused with
`ModsMismatch` the moment it tries to join a Space Age world. The expansion
tarball is roughly 4.4 GB and the image about 10 GB. Every container shares the
layer, so that is paid once.

`acquire` calls `build.sh` itself when the image is missing. Running it by hand
first is only a way to see the download fail early.

## One image, two roles

The same image and the same binary serves both roles, switched by
`FACTORIO_ROLE` in the entrypoint.

- **host** — `--start-server`. The full binary initialises no graphics in server
  mode, so the host needs no display.
- **seat** — `--mp-connect`, under `Xvfb` with Mesa's software rasterizer. Only
  the camera runs this role (§ *Seats*).

This is why there is one Dockerfile and not two. It is also the only reason the
host and the camera cannot drift out of version or mod sync, which is the failure
that bites every hand-built Factorio multiplayer setup. The headless build cannot
serve as the camera at all — its binary has no `--mp-connect`.

## Worlds

A world is one appliance of one of two kinds:

- **game** — the realm and any other long-lived world. It runs realm rules by
  default, at 4 cores and 6 GiB.
- **lab** — a throwaway world for experiments. It loads the lab mods from
  `RLM_FACTORIO_LAB_MODS`, the lab server and map-gen settings in
  `appliance/config/`, and a saves folder bound from
  `worlds/<session_id>/saves`. A realm save is copied in and never written. A lab
  runs sandbox rules by default, at 2 cores and 4 GiB.

Either kind is headless by default and publishes only its control port.
`acquire` with `headless=false` also publishes the world's UDP game port, so a
person can join (§ *Joining the world*). The toggle is independent of the rules.
A sandbox lab a person watches is the usual reason to turn it off.

Each world runs under its own compose project, with a generated override at
`$AWM_WORKSPACE/.awm/services/rlm-factorio/worlds/<session_id>/compose.json`.
The project namespaces the volume, network and containers. That is what lets
several worlds run at once and keeps a lab off the realm's saves volume. The realm
keeps the default project name and ports.

**CAUTION:** a live world cannot change kind or toggle. An `acquire` that passes
a `headless` value the live world does not have fails. To join the realm from a
client, save it, `release` it and `acquire` it again with `headless=false`.

Past `RLM_FACTORIO_MAX_LABS` live labs, a lab `acquire` fails at once with
`LAB_CAP`. It does not wait, because a wait would outlast the gateway's call
timeout. The caller retries.

The supervisor is bind-mounted from `appliance/supervise.py`, so a supervisor
change ships with a world restart, not an image rebuild. It boots on
`BOOT_SAVE` or `BOOT_NEW` and reports `ready` only once the boot pause is set,
so no call sees a lab run before it is paused.

`advance` and `wait_until` block in the service and poll the engine, one
throttled read per poll. One call lasts at most `RLM_FACTORIO_WAIT_MAX_S`. The
manifest gives the gateway 60 s more than that for both verbs. **CAUTION:** keep
the two in step, or the gateway cuts a long wait short and the caller sees a 504
while the wait runs on.

## Run

You never start the service by hand. The gateway discovers any folder under
`awm/services/` holding a `run.sh`, starts it with `bash run.sh`, and injects
`AWM_HUB_URL`, `AWM_SERVICE_NAME` and `AWM_SERVICE_ID`. There is no auth — the
registration handshake carries no token.

The service ships `profiles = ["gamebot"]`, so a gateway without that profile
skips it at bootstrap.

To drive it against a throwaway gateway, run `scratchpad/rlm_harness.sh
--docker`. It boots an isolated gateway over a temp services tree holding only
this service, then runs the live lifecycle against Docker.

## Seats

A seat is a body an agent acts through. It has one of two roles.

- **drone** (the default) — a `game-bot-drone` character that the
  `game-bot-control` mod creates and drives every tick. No client and no container
  stand behind it, so a join takes under a second. It collides with nothing,
  walks in a straight line, and reaches, builds and mines 128 tiles out. Every
  other rule is the game's own: mining takes time, a craft counts toward
  production statistics and fires craft-item triggers, and a drone that dies
  drops a corpse with its inventory and respawns at the force's spawn.
- **camera** — the one client a world runs. It renders every screenshot and
  never plays. A drone's `screenshot` starts it on demand.

A drone is bound by **name**. The service names the seat, and the mod keys its
state by that name, so a verb cannot reach a body the caller does not hold.

**CAUTION:** the mod pushes a `speed_violation` event when a drone moves faster
than its running speed in one tick. Only a script that moves the body directly
causes one, so treat the event as a broken rule, not as noise.

A seat is a **lease**. `join` records the caller the gateway threads in as its
owner — a placed agent's identity, absent for a CLI call — or an explicit `owner`
argument. Ownership is recorded and never enforced: one shared force means there
is no protection between players anyway, and a shell has no identity to check
against. Every verb naming a seat refreshes its last-seen stamp. A background
reaper reclaims a drone whose owner went quiet past `AWM_FACTORIO_SEAT_IDLE_S`,
a camera whose container died, and a join that never completed.

**CAUTION:** nothing holds a seat except using that seat. Not a teammate's
activity, not the session being busy. An agent that leases a seat and then works
elsewhere for an hour loses it.

The camera joins under one fixed name, `AWM_FACTORIO_CAMERA_NAME` (default
`fxcam`), as a spectator with no character. A lab parks it in the lab mod's view
booth, off every play surface. The camera mounts its sprite caches read-only from
`output/camera-cache/<key>/`, where the key holds the image tag and every loaded
mod's version. The service builds a missing cache once, in a client container
with no network, in about a minute.

**CAUTION:** a client that finds a cache built for other mods rewrites it. On a
read-only mount that write stalls the join. Keep every loaded mod in the key.

The reaper never touches a session, so it cannot disconnect a person who joined
from Steam or close the world under them.

A service restart leaves every seat in place. Drones live in the world, and the
next start adopts each camera whose container still runs. Only the reaper,
`leave` and `release` end seats, so a deploy never drops the crew. The host
appliance stays up for the same reason: it holds the world, and both `acquire`
and startup reconciliation re-adopt it.

Screenshots are files, not payloads. The camera renders into its own bind-mounted
`script-output` directory, and the verb returns a path under
`$AWM_WORKSPACE/.awm/services/rlm-factorio/output/seats/<seat_id>/`. Read the
file to see it.

## Game calls

Every game call goes through one broker process per appliance,
`appliance/rcon_broker.py`, which the realm starts with `docker exec` from the
source in its own tree. A broker change therefore ships with a realm restart and
never needs an image rebuild. The broker holds one RCON socket per connection
key: one per seat, `<container>/<caller>` for each seatless caller on each
world, and `sys` for the realm's own bookkeeping. One shared socket used to
queue the whole crew behind a lock. A seatless socket closes after
`AWM_FACTORIO_CONN_IDLE_S` of quiet.

**CAUTION:** Factorio answers each command with exactly one packet, even a
100 KB reply. Never wait for more, and never send the Source-style empty
sentinel packet. Factorio does not echo it, so the read hangs.

The throttle meters each key in engine script milliseconds and charges a command
after it runs. Nothing can stop a running command: Factorio's Lua has no clock
and no debug hook, and a time-based stop would desync every peer. So a command
over its budget still completes, and returns `OVER_BUDGET` with its output
attached. A read too large for one command pages through `scan`, whose work cap
counts entities examined, never time. Under sandbox rules an overrun counts
against its key but returns no error, because lab fixtures run heavy Lua on
purpose. Each world has its own throttle and UPS gate.

## The cost of command text

The engine sends a command's text to every peer before it runs the command, in
per-tick segments whose size `server-settings.json` sets. It is 1000-1400 bytes
here, up from Factorio's default of 25-100.

**CAUTION:** the image bakes `appliance/config/server-settings.json`, and the
engine reads it only when it starts. To change a running appliance, `docker cp`
the file to `/factorio/config/server-settings.json` and reload the world. Then
rebuild the image, or the next appliance comes up with the old file.

Segment size is not the whole cost. On a test rig with 3 seats on a fresh map, the
larger segments cut a 4 KB command from 550 ms to 50 ms. On the live world, with
10 seats on a large base, a bare command takes about 150 ms. Text costs about
0.85 ms per compressed byte there under either setting, and the cause is not yet
known. `exec_lua cache=true` stores a script in the world once and then runs it
by name, which avoids the cost for any script run more than once.

## Changing the mod

The host, the camera and every desktop client must load identical
`game-bot-control` bytes, data stage included. The drone prototype and its
sprites live in the data stage. Each reads the mod only when its engine starts,
and the host and the camera bind-mount the mod directory of the realm's tree. So
a mod change ships only with a world reload, which restarts the engine:

1. Save the world.
2. Advance the realm's tree to the new mod. A deploy does this.
3. Run `world_load` of that save.
4. Run `rejoin` for the session.
5. Install the new zip in every desktop client (§ *Joining the world*).

**WARNING:** between steps 2 and 3, a client that joins loads the new mod
against the old engine and is refused. Hold all joins for the window.

A new mod version changes the camera cache key, so the first camera after the
change builds a new cache. A verb that needs a newer mod than the world runs,
`join` of a drone among them, fails with an error naming both versions.

## Sacred saves

**WARNING:** the engine always runs on a private scratch file, `_active.zip`,
never on a named save. A named save is an immutable snapshot. Nothing writes
`<name>.zip` except an explicit `world_save`, which requires a name and refuses
to clobber unless `overwrite` is true.

`world_save` is a live console action and is seamless for connected players.
`world_new` and `world_load` re-exec the engine in place: the container stays up
and every client drops. Both discard unsaved progress, exactly as the desktop UI
does. Snapshot first.

Run `rejoin` after a reload. A loaded save holds the drones it was saved with,
and `rejoin` respawns any the save lacks under the same seat name. The camera's
client drops with the reload, and `rejoin` starts it again. `join` would mint a
new seat instead.

Saves are ordinary `.zip` files, interchangeable with a desktop client.

**CAUTION:** the mod binds its state at map generation, so a world older than the
mod has no seat state and every seat verb errors against it. The first move on a
pre-existing saves volume is `world_new`.

## Configuration

Defaults suit one realm on one host. A test rig must override the first four.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_FACTORIO_PROJECT` | `rlm-factorio` | compose project — namespaces containers and the saves volume |
| `AWM_FACTORIO_CONTAINER` | `rlm-factorio-appliance` | host container name |
| `AWM_FACTORIO_GAME_PORT` | `12140` | the realm's UDP game port, published only when not headless |
| `AWM_FACTORIO_CONTROL_PORT` | `12142` | published supervisor control port |
| `AWM_FACTORIO_SEAT_IDLE_S` | `3600` | idle seconds before the reaper takes a drone (`0` disables) |
| `AWM_FACTORIO_REAP_POLL_S` | `60` | reaper sweep interval |
| `AWM_FACTORIO_SEAT_JOIN_TIMEOUT` | `240` | seconds to wait for the camera to reach the world |
| `AWM_FACTORIO_CACHE_BUILD_TIMEOUT` | `900` | seconds to wait for a camera cache build |
| `AWM_FACTORIO_SCREENSHOT_TIMEOUT` | `60` | seconds to wait for a rendered PNG to settle |
| `AWM_FACTORIO_EVENTS_POLL_S` | `2` | in-world event pump interval |
| `AWM_FACTORIO_SEAT_MS_PER_S` | `50` | script ms per second each key may spend |
| `AWM_FACTORIO_SEAT_BURST_MS` | `250` | script ms a quiet key may spend at once |
| `AWM_FACTORIO_UPS_FLOOR` | `55` | below this UPS, only keys holding half a burst send |
| `AWM_FACTORIO_CMD_BUDGET_MS` | `15` | per-command budget before `OVER_BUDGET` |
| `AWM_FACTORIO_CMD_BUDGET_MAX_MS` | `50` | ceiling a call's `budget_ms` may raise it to |
| `AWM_FACTORIO_CONN_IDLE_S` | `600` | quiet seconds before a seatless socket closes |
| `AWM_FACTORIO_CAMERA_NAME` | `fxcam` | player name of every camera |
| `RLM_FACTORIO_MAX_LABS` | `3` | live labs before `acquire` refuses with `LAB_CAP` |
| `RLM_FACTORIO_WAIT_MAX_S` | `600` | longest single `advance` or `wait_until` |
| `RLM_FACTORIO_LAB_MODS` | game-bot `research/lab/fxlab/mods` | folder holding the lab mods |

**WARNING:** the default project owns `rlm-factorio_factorio-saves`, the volume
real worlds live in. A rig that leaves these at their defaults runs against it,
and one `docker compose down -v` in its cleanup destroys them. Set the first four
and scope every teardown to the rig's own project name.

**CAUTION:** the camera runs at 2 cores and 4 GiB only because it mounts a
prebuilt sprite cache. A client that builds its own atlas peaks near 3.7 GiB
across 15 threads, and at 2 cores a 40-second join stretched past five minutes.
The cache build therefore runs at 4 cores and 6 GiB.

The service sets the host container's ceiling per kind through `FACTORIO_CPUS`
and `FACTORIO_MEMORY`: 2 cores and 4 GiB for a lab, 4 cores and 6 GiB otherwise.

## Joining the world from a desktop or Steam client

A world publishes its game port only when acquired with `headless=false`
(§ *Worlds*). The realm's game port is 12140. Every other world takes its own,
and `acquire` returns it as `game_port`.

`server-settings.json` ships with no password, no account verification, LAN
visibility on and unlimited players. The only barriers are version and mods, and
**all three of these must match the server or the join is refused**:

1. **The exact engine version.** The image is pinned to 2.1.20 on the
   *experimental* branch. In Steam, opt into the matching version under
   *Factorio → Properties → Betas*.
2. **The Space Age expansion.** The server enables `space-age`, `quality`,
   `elevated-rails` and `recycler`. These are DLC and cannot be downloaded, only
   owned.
3. **The private mods.** `game-bot-control` is a local mod, not on the portal,
   so the server cannot push it. A lab also runs `fxlab-sbx` and `fxlab-census`
   from `RLM_FACTORIO_LAB_MODS`. Install each by hand.

To install the mods, build the zips. Name an output folder and each lab mod
directory to include the lab mods:

    bash appliance/pack-mod.sh
    bash appliance/pack-mod.sh dist appliance/mods/game-bot-control \
        "$RLM_FACTORIO_LAB_MODS"/fxlab-sbx "$RLM_FACTORIO_LAB_MODS"/fxlab-census

Copy each zip it prints into the client's mods folder. Do not unzip it — Factorio
reads mod zips directly. The folder is `%APPDATA%\Factorio\mods` on Windows and
`~/.factorio/mods` on Linux, under the default
`use-system-read-write-data-directories=true`. Enable each in that folder's
`mod-list.json`, for example `{"name":"game-bot-control","enabled":true}`.

**CAUTION:** Factorio rescans the mods folder only at startup. Relaunch the
client fully after copying. Bumping the mod's version invalidates every
hand-installed copy, so a version bump means every client reinstalls.

Then connect to `localhost:<game_port>` under *Multiplayer → Connect to
address*. On the realm a person plays a normal character. In a lab the lab mod
removes the joining character, so a person watches and builds without a body,
with cheat mode on under sandbox rules. Docker Desktop forwards the published UDP port to the Windows host's
loopback. From another device on the LAN use the host's LAN IP and allow inbound
UDP on the game port through its firewall.

**CAUTION:** Factorio multiplayer is UDP-only on the game port. A `netsh
portproxy` Windows-to-WSL forward carries TCP only and will not carry the game.

## Realm-family contract

Verbs are projected into the gateway catalog as `factorio_<verb>`. The service is
`tier = "off"`, so the `factorio` tool is not on the MCP surface. CLI and HTTP
reach every verb (`awm factorio join`), and `awm factorio --help` lists them.

They group as session lifecycle, seats, perception, world lifecycle, and acting
as a player. These facts about that surface are worth knowing before reading it:

- **Every perceive and act verb is addressed the same way**: by `seat_id`, or by
  `session_id` alone when that session holds exactly one drone. Naming neither is
  a usage error and never a default, because picking would act as somebody else's
  character.
- **Under realm rules, agent Lua runs guarded.** Without the admin token,
  `exec_lua` and every tool run with a read-only view of the game. Any write or
  any method outside the read set fails with an error naming the call. The script
  changes the world only through `act`, the drone verbs of its own seat.
- **`exec_lua` runs in the scenario context** under other rules, where the mod's
  storage is invisible — reach mod state through `remote.call('game_bot', …)`. It returns
  what the script *printed*, so a value comes back through `rcon.print(...)` and
  a bare `return` is discarded. It takes a file path as well as an inline string,
  which is the only form a multi-line script survives shell quoting in. The
  resolved seat is bound as `seat` and its body as `drone`.
- **`exec_lua cache=true` runs the script in the mod context** as a stored
  script. Its globals last one call, `storage` is a table of its own, and it may
  not register an event handler. A handler added outside `control.lua` is missing
  from the save, so a peer that joins later runs without it and desyncs.

The `factorio` emitter fires `rlm.factorio.<kind>` carrying
`{session_id, kind, tick?, data}`: world and seat lifecycle events from the
verbs, plus the mod's in-world events drained by a background pump.
`observe_events` drains the same buffer on demand and is session-wide, so one
seat's drain takes every seat's events. Let one watcher own it.
