# Installing the `rlm-factorio` service

## Purpose & Contents

This file covers installing, building and running the Factorio realm, the seat
model the service is built around, how game calls reach the engine and what they
cost, how a mod change ships, and how a person joins the world from their own
Factorio client.

It does not list verbs or their parameters. `rlm(verb="describe")` answers that
from the running manifest and cannot go stale. What is written here is what
reading the code will not tell you: why the shape is what it is, and which two
things must agree.

## What this service is

A Python feature service in the `awm.rlm_factorio` namespace, in awm's `rlm-*`
realm family beside `rlm-browser`. It owns a Factorio appliance — a Docker
container running a stdlib supervisor that owns the engine — and hands agents
**seats** in the world it hosts.

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
- **seat** — `--mp-connect`, under `Xvfb` with Mesa's software rasterizer.

This is why there is one Dockerfile and not two. It is also the only reason host
and seats cannot drift out of version or mod sync, which is the failure that
bites every hand-built Factorio multiplayer setup. The headless build cannot
serve as a seat at all — its binary has no `--mp-connect`.

## Run

You never start the service by hand. The gateway discovers any folder under
`awm/services/` holding a `run.sh`, starts it with `bash run.sh`, and injects
`AWM_HUB_URL`, `AWM_SERVICE_NAME` and `AWM_SERVICE_ID`. There is no auth — the
registration handshake carries no token.

The service ships `profiles = ["gamebot"]`, so a gateway without that profile
skips it at bootstrap.

To drive it against a throwaway gateway, run `scratchpad/rlm_harness.sh
--docker`. It boots an isolated gateway over a temp services tree holding only
this service, then runs the full live lifecycle: a world, three seats playing in
it at once, a screenshot each, a blueprint round trip, and all three ways a seat
is reclaimed.

## Seats

A seat is a real Factorio client container that joins the host world and mints a
real `LuaPlayer`. Agents act *as* players: mining sustains, hand-crafting counts
toward production statistics so craft-item research triggers fire on their own,
and a seat can render what it sees.

A seat is bound by **name**, not by guessing. Each seat container writes its own
`player-data.json` with a deterministic player name, and the server sets
`require_user_verification: false`, so the service resolves
`game.players["<seat_id>"]` rather than watching for whichever player appeared.
No factorio.com account is needed per seat.

A seat is a **lease**. `join` records the caller the gateway threads in as its
owner — a placed agent's identity, absent for a CLI call — or an explicit `owner`
argument. Ownership is recorded and never enforced: one shared force means there
is no protection between players anyway, and a shell has no identity to check
against. Every verb naming a seat refreshes its last-seen stamp, and a background
reaper reclaims a seat whose container died, whose owner went quiet past
`AWM_FACTORIO_SEAT_IDLE_S`, or whose join never completed.

**CAUTION:** nothing holds a seat except using that seat. Not a teammate's
activity, not the session being busy. An agent that leases a seat and then works
elsewhere for an hour loses it.

The reaper sweeps seats only. It never touches a session, so it cannot
disconnect a person who joined from Steam or close the world under them.

A realm restart leaves every seat running. SIGTERM (`awm services stop`, a
deploy, a gateway restart) ends only the service process, and the next start
adopts each seat whose container still runs. Only the reaper, `leave` and
`release` tear seats down, so a realm deploy never drops the crew. Disabling the
service therefore leaves seats running until `release` or the reaper of the next
start. The host appliance is left up for the same reason: it holds the world, and
both `acquire` and startup reconciliation re-adopt it.

Screenshots are files, not payloads. A seat renders into its own bind-mounted
`script-output` directory and the verb returns a path under
`$AWM_WORKSPACE/.awm/services/rlm-factorio/output/seats/<seat_id>/`. Read the
file to see it.

## Game calls

Every game call goes through one broker process per appliance,
`appliance/rcon_broker.py`, which the realm starts with `docker exec` from the
source in its own tree. A broker change therefore ships with a realm restart and
never needs an image rebuild. The broker holds one RCON socket per connection
key: one per seat, `anon` for a seatless agent call, and `sys` for the realm's
own bookkeeping. One shared socket used to queue the whole crew behind a lock.

**CAUTION:** Factorio answers each command with exactly one packet, even a
100 KB reply. Never wait for more, and never send the Source-style empty
sentinel packet. Factorio does not echo it, so the read hangs.

The throttle meters each key in engine script milliseconds and charges a command
after it runs. Nothing can stop a running command: Factorio's Lua has no clock
and no debug hook, and a time-based stop would desync every peer. So a command
over its budget still completes, and returns `OVER_BUDGET` with its output
attached. A read too large for one command pages through `scan`, whose work cap
counts entities examined, never time.

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

The host, every seat and every desktop client must load identical
`game-bot-control` bytes. Each reads the mod only at start, and seats bind-mount
the mod directory of the realm's tree. So a mod change ships only with a world
reload:

1. Save the world.
2. Advance the realm's tree to the new mod, then restart the realm.
3. Run `world_load` of that save.
4. Run `rejoin` for the session.
5. Install the new zip in every desktop client (§ *Joining the world*).

**WARNING:** between steps 2 and 3, a seat that joins loads the new mod against
the old engine and is refused. Hold all joins for the window.

The realm verbs that need a newer mod (`walk`, `wait`, `throw`, `order`) refuse
an older world with an error naming both versions.

## Sacred saves

**WARNING:** the engine always runs on a private scratch file, `_active.zip`,
never on a named save. A named save is an immutable snapshot. Nothing writes
`<name>.zip` except an explicit `world_save`, which requires a name and refuses
to clobber unless `overwrite` is true.

`world_save` is a live console action and is seamless for connected players.
`world_new` and `world_load` re-exec the engine in place: the container stays up
and every client drops. Both discard unsaved progress, exactly as the desktop UI
does. Snapshot first.

A seat does not reconnect by itself after a reload. Run `rejoin`: it restarts
each seat's client under its own player name, and the same name brings back the
same player, so the seat id, character and inventory all survive. `join` would
mint a new seat and a new player instead.

Saves are ordinary `.zip` files, interchangeable with a desktop client.

**CAUTION:** the mod binds its state at map generation, so a world older than the
mod has no seat state and every seat verb errors against it. The first move on a
pre-existing saves volume is `world_new`.

## Configuration

Defaults suit one session on one host. A test rig must override the first four.

| Env var | Default | Meaning |
|---|---|---|
| `AWM_FACTORIO_PROJECT` | `rlm-factorio` | compose project — namespaces containers and the saves volume |
| `AWM_FACTORIO_CONTAINER` | `rlm-factorio-appliance` | host container name |
| `AWM_FACTORIO_GAME_PORT` | `12140` | published UDP game port |
| `AWM_FACTORIO_CONTROL_PORT` | `12142` | published supervisor control port |
| `AWM_FACTORIO_SEAT_CPUS` | `4.0` | per-seat CPU ceiling |
| `AWM_FACTORIO_SEAT_MEMORY` | `6g` | per-seat memory ceiling |
| `AWM_FACTORIO_SEAT_IDLE_S` | `3600` | idle seconds before the reaper takes a seat (`0` disables) |
| `AWM_FACTORIO_REAP_POLL_S` | `60` | reaper sweep interval |
| `AWM_FACTORIO_SEAT_JOIN_TIMEOUT` | `240` | seconds to wait for a seat to reach the world |
| `AWM_FACTORIO_SCREENSHOT_TIMEOUT` | `60` | seconds to wait for a rendered PNG to settle |
| `AWM_FACTORIO_EVENTS_POLL_S` | `2` | in-world event pump interval |
| `AWM_FACTORIO_SEAT_MS_PER_S` | `50` | script ms per second each key may spend |
| `AWM_FACTORIO_SEAT_BURST_MS` | `250` | script ms a quiet key may spend at once |
| `AWM_FACTORIO_UPS_FLOOR` | `55` | below this UPS, only keys holding half a burst send |
| `AWM_FACTORIO_CMD_BUDGET_MS` | `15` | per-command budget before `OVER_BUDGET` |
| `AWM_FACTORIO_CMD_BUDGET_MAX_MS` | `50` | ceiling a call's `budget_ms` may raise it to |

**WARNING:** the default project owns `rlm-factorio_factorio-saves`, the volume
real worlds live in. A rig that leaves these at their defaults runs against it,
and one `docker compose down -v` in its cleanup destroys them. Set the first four
and scope every teardown to the rig's own project name.

**CAUTION:** size the seat caps to the **join**, not to play. A joining client
builds its sprite atlas across 15 threads with a peak near 3.7 GiB, while a seat
already in the world with its window unmapped sits at about an eighth of a core
and 2.4 GiB. At 2 cores and 4 GiB a 40-second join stretched past five minutes.
Squeezing here saves a running seat nothing, and a peer that cannot keep the
server's tick rate is dropped from the game.

The host container's own ceiling lives in `docker-compose.yml` as `FACTORIO_CPUS`
and `FACTORIO_MEMORY`.

## Joining the world from a desktop or Steam client

`server-settings.json` ships with no password, no account verification, LAN
visibility on and unlimited players. The only barriers are version and mods, and
**all three of these must match the server or the join is refused**:

1. **The exact engine version.** The image is pinned to 2.1.20 on the
   *experimental* branch. In Steam, opt into the matching version under
   *Factorio → Properties → Betas*.
2. **The Space Age expansion.** The server enables `space-age`, `quality`,
   `elevated-rails` and `recycler`. These are DLC and cannot be downloaded, only
   owned.
3. **The `game-bot-control` mod.** It is a private local mod, not on the portal,
   so the server cannot push it. Install it by hand.

To install the mod, build the zip:

    bash appliance/pack-mod.sh

Copy the zip it prints into the client's mods folder. Do not unzip it — Factorio
reads mod zips directly. The folder is `%APPDATA%\Factorio\mods` on Windows and
`~/.factorio/mods` on Linux, under the default
`use-system-read-write-data-directories=true`. Enable it in that folder's
`mod-list.json` with `{"name":"game-bot-control","enabled":true}`.

**CAUTION:** Factorio rescans the mods folder only at startup. Relaunch the
client fully after copying. Bumping the mod's version invalidates every
hand-installed copy, so a version bump means every client reinstalls.

Then connect to `localhost:12140` under *Multiplayer → Connect to address*.
Docker Desktop forwards the published UDP port to the Windows host's loopback.
From another device on the LAN use the host's LAN IP and allow inbound UDP 12140
through its firewall.

**CAUTION:** Factorio multiplayer is UDP-only on the game port. A `netsh
portproxy` Windows-to-WSL forward carries TCP only and will not carry the game.

## Realm-family contract

Verbs are projected into the gateway catalog as `rlm_factorio_<verb>`. On the
collapsed MCP surface they fold under the shared `rlm` domain tool as
`factorio_<verb>` — `rlm(verb="factorio_join", args={…})` — while CLI and HTTP
stay expanded. `rlm(verb="describe")` lists them beside rlm-browser's `browser_*`
verbs.

They group as session lifecycle, seats, perception, world lifecycle, and acting
as a player. Two things about that surface are worth knowing before reading it:

- **Every perceive and act verb is addressed the same way**: by `seat_id`, or by
  `session_id` alone when that session holds exactly one seat. Naming neither is
  a usage error and never a default, because picking would act as somebody else's
  character.
- **`exec_lua` runs in the scenario context**, where the mod's storage is
  invisible — reach mod state through `remote.call('game_bot', …)`. It returns
  what the script *printed*, so a value comes back through `rcon.print(...)` and
  a bare `return` is discarded. It takes a file path as well as an inline string,
  which is the only form a multi-line script survives shell quoting in.
- **`exec_lua cache=true` runs the script in the mod context** as a stored
  script. Its globals last one call, `storage` is a table of its own, and it may
  not register an event handler. A handler added outside `control.lua` is missing
  from the save, so a peer that joins later runs without it and desyncs.

The `factorio` emitter fires `rlm.factorio.<kind>` carrying
`{session_id, kind, tick?, data}`: world and seat lifecycle events from the
verbs, plus the mod's in-world events drained by a background pump.
`observe_events` drains the same buffer on demand and is session-wide, so one
seat's drain takes every seat's events. Let one watcher own it.
