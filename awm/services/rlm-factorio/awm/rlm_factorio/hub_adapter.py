"""Hub adapter for the rlm-factorio realm service.

Boots rlm-factorio as a gateway-registered process: stands up its own DB, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect). The realm-family functions are exposed over the control WS
and projected into the gateway catalog as ``factorio_<verb>`` tools.

This service owns the Factorio appliance (Docker container + stdlib supervisor —
see ``appliance/``). The lifecycle + world verbs are LIVE: ``acquire`` brings the
container up and waits for the engine, ``world_new/save/load`` drive the
supervisor's control surface (sacred-saves invariant preserved — only
``world_save`` ever writes a named ``.zip``), ``release`` tears the container
down, seats first (the saves volume survives).

``join`` allocates a SEAT. A drone seat is a body the world itself holds: the
``game-bot-control`` mod spawns a ``game-bot-drone`` character for it and drives
it every tick, so it walks, mines, crafts and fights by the game's rules with no
client attached. Seats are what make the realm a LAN party — several agents in
one world, one shared force. The camera seat is the one client a world runs: a
container in the image's client role, joined as a spectator, that draws every
screenshot. ``leave`` returns a seat; ``seats`` lists them.

A seat is leased, not given. It records the caller the gateway threads in as its
owner, every verb naming it refreshes a last-seen stamp, and a background reaper
reclaims drones whose owner has gone quiet for an hour, and a camera whose
container has died. Nothing but this service can notice an agent that died
mid-session.

Everything an agent does in the world it does AS a seat. The act verbs —
``move`` / ``stop`` / ``mine`` / ``craft`` / ``build`` / ``insert`` / ``take``,
plus the flagged cheats ``teleport`` and ``research`` — and the perceive verbs
``observe`` / ``recipes`` / ``technologies`` all take a ``seat_id``, or a
``session_id`` alone when that session holds exactly one seat. The service, not
the caller, supplies the seat's player name to the world, so a verb cannot be
aimed at a player the caller does not hold.

They all travel the same path: an RCON broker inside the appliance container
(:mod:`awm.rlm_factorio.broker`) gives each seat its own engine socket, and
each verb names a function of the ``game-bot-control`` mod. Adding a capability
is a mod function and a handler, with nothing to plumb in between. Every
command reports its engine script time; :mod:`awm.rlm_factorio.throttle` meters
each seat by it, and a command over its budget comes back as OVER_BUDGET. The gameplay verbs are reach-gated —
a seat walks to a thing before touching it — and act through the engine's own
mechanics, so mining takes time and crafting counts toward research triggers.

``exec_lua`` is the escape hatch. It takes inline ``code`` or a ``path`` to a
file on this machine, because a real script does not survive shell quoting. It
runs in the SCENARIO context, where the mod's ``storage`` is invisible (Factorio
disables ``load`` in the mod control stage, so there is nowhere else to put it);
mod state is reachable from there through ``remote.call('game_bot', ...)``. The
resolved seat is bound as ``seat`` / ``drone`` on the script's first line, so a
file script needs no templating and its line numbers still match. Under realm
rules the script instead runs as a stored script in the mod, guarded: it reads
the game, and changes it only through ``act``, its seat's drone verbs.

The ``factorio`` emitter is LIVE: world verbs fire ``world_loaded`` /
``world_saved`` / ``error`` directly, and a background pump drains the mod's
bounded events ring buffer (``spawned`` / ``arrived`` / ``died`` / ...) on a
short interval and re-emits each entry as ``{session_id, kind, tick, data}`` on
the topic — projected up-stack as ``rlm.factorio.<kind>``. Every fired event is
also accumulated in a per-session service-side inbox so a *polling* consumer
never races the pump: ``observe_events`` returns everything since its last call
(consume-once), while the emitter stays the live stream.

Every world is a row with a ``kind`` -- ``headless`` (the game), ``lab`` (a
throwaway experiment server) or ``rendered`` (the game with its port published,
so a human can join) -- and ``rules``. The realm runs ``realm`` rules and a lab
runs ``sandbox``; the god verbs (``teleport``, ``research``, built blueprints,
``sbx`` god verbs, cheat tools, ``advance``) are refused under the former unless
the caller holds the admin token. Seats, saves, pause, events and
every other verb work the same on every kind.

``acquire`` is idempotent per world name: a second acquire of a live world
returns it, erroring on a kind or game mismatch. The row carries the
container/ports so a service respawn re-adopts rather than duplicating.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.rlm_factorio.hub_adapter
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.rlm_factorio import appliance, broker, dao, scan, throttle, validate

log = logging.getLogger("awm.rlm_factorio.hub_adapter")

# Every perceive/act verb is addressed the same way: by the seat that is acting,
# by the session when it holds exactly one seat, or by both. Passing only a
# seat_id is enough -- the session is derived from it -- so an agent that
# remembers one identifier can drive the whole surface.
_SEAT_TARGET = [
    {"name": "seat_id", "type": "string", "required": False},
    {"name": "session_id", "type": "string", "required": False},
]

_TARGET_DOC = (
    "Pass seat_id, or session_id alone when the session has exactly one seat."
)

# Rules: the realm runs ``realm`` rules; a lab runs ``sandbox``. A sandbox call
# may ask for ``realm`` rules; ``admin_token`` lifts a realm-rules refusal.
_RULES = [
    {"name": "rules", "type": "string", "required": False,
     "description": "realm, to run a sandbox world's call under realm rules"},
    {"name": "admin_token", "type": "string", "required": False,
     "description": "lifts a refusal under realm rules"},
]

# The longest any blocking verb (wait_until, advance) holds its caller. The
# gateway's timeout for those verbs sits above it.
WAIT_MAX_S = float(os.environ.get("RLM_FACTORIO_WAIT_MAX_S", "600"))

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- lifecycle ----
        {
            "name": "acquire",
            "effect": "write",
            "tool": "factorio_acquire",
            "description": (
                "Acquire a Factorio world: bring its appliance container up "
                "(building the image on first run) and wait for the engine to be "
                "ready. kind is headless (the game, the default), lab (a "
                "throwaway server with the lab mods, sandbox rules and its saves "
                "in saves_dir) or rendered (the game with its port published so "
                "a human can join). name picks the world (default 'realm'; a lab "
                "must name itself). A lab boots on save (a realm save name, or a "
                "path to a .zip) or on a new world with create=true, paused "
                "unless live=true. Past the lab cap a lab acquire fails at once "
                "with LAB_CAP; retry later. Returns {session_id, kind, rules, "
                "name, adopted}. Idempotent per name: a second acquire returns "
                "the live world (errors on a kind or game mismatch)."
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
                {"name": "kind", "type": "string", "required": False,
                 "description": "headless, lab or rendered"},
                {"name": "name", "type": "string", "required": False},
                {"name": "rules", "type": "string", "required": False,
                 "description": "realm or sandbox; a lab defaults to sandbox"},
                {"name": "save", "type": "string", "required": False},
                {"name": "create", "type": "boolean", "required": False},
                {"name": "live", "type": "boolean", "required": False},
            ],
            # First acquire builds the image (large download) before waiting for
            # the engine — well beyond the proxy's 30s default.
            "timeout": 1800.0,
        },
        {
            "name": "release",
            "effect": "write",
            "tool": "factorio_release",
            "description": (
                "Release a session: stop + remove the appliance container. The "
                "named-saves volume is preserved (sacred saves survive)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
            "timeout": 120.0,  # compose down
        },
        {
            "name": "reset",
            "effect": "write",
            "tool": "factorio_reset",
            "description": (
                "Reset a session in place: generate a fresh world (discards live "
                "progress; named saves untouched), keeping the container slot."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
            "timeout": 600.0,  # engine re-exec + map gen
        },
        {
            "name": "status",
            "effect": "read",
            "tool": "factorio_status",
            "description": (
                "Status of one session (pass session_id) or all sessions (omit "
                "it). Returns {sessions: [...]}, each enriched best-effort with "
                "the appliance's live /status (running, ready, saves)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
        },
        # ---- seats ----
        {
            "name": "join",
            "effect": "write",
            "tool": "factorio_join",
            "description": (
                "Join the session's world with a drone: a body the world holds "
                "and drives, spawned at the force's spawn in under a second. "
                "Returns {seat_id, player_name, owner, position}; player_name "
                "is the seat's name in the world. A drone walks over anything "
                "in a straight line, reaches 128 tiles, mines, hand-crafts "
                "(counted toward research triggers), builds, throws capsules "
                "and fights, all at the game's own rates. When it dies its "
                "corpse keeps the inventory and a new drone appears at spawn "
                "10 s later. Every act verb below is driven through a seat. A "
                "seat is a LEASE: it is owned by the caller (or by an explicit "
                "'owner'), any verb naming it holds it, and one left untouched "
                "for an hour is reclaimed. Call 'leave' when done. role=camera "
                "joins the world's renderer, a client with no body; one per "
                "world, never reclaimed for idleness. screenshot starts it on "
                "demand, so joining one is rarely needed."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "owner", "type": "string", "required": False},
                {"name": "role", "type": "string", "required": False,
                 "description": "drone (default) or camera"},
            ],
            # A camera's first start builds the sprite cache, then joins.
            "timeout": 1200.0,
        },
        {
            "name": "rejoin",
            "effect": "write",
            "tool": "factorio_rejoin",
            "description": (
                "Bring seats back after a world reload: a drone the loaded "
                "save lacks spawns again under the same seat, and the camera "
                "reconnects. A drone the save holds keeps its body and "
                "inventory. Pass seat_id for one seat, or session_id for "
                "every live seat (parallel, 1-4, rejoins that many at once). "
                "Returns {seats: [{seat_id, position | player_index | error}]}."
            ),
            "params": [
                {"name": "seat_id", "type": "string", "required": False},
                {"name": "session_id", "type": "string", "required": False},
                {"name": "parallel", "type": "integer", "required": False},
            ],
            "timeout": 3600.0,
        },
        {
            "name": "leave",
            "effect": "write",
            "tool": "factorio_leave",
            "description": (
                "Release a seat: remove its drone, inventory and all, or stop "
                "the camera. The world and every other seat are untouched."
            ),
            "params": [
                {"name": "seat_id", "type": "string", "required": True},
            ],
            "timeout": 120.0,
        },
        {
            "name": "seats",
            "effect": "read",
            "tool": "factorio_seats",
            "description": (
                "List seats, optionally for one session: their name, role and "
                "owner. A drone carries in_world, alive, position and "
                "respawn_at from the world; the camera carries its container "
                "state and whether it is connected. Also how long since each "
                "was last used -- the clock the reaper reclaims a drone by."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
            "timeout": 60.0,
        },
        # ---- perceive ----
        {
            "name": "observe",
            "effect": "write",
            "tool": "factorio_observe",
            "description": (
                "Look at the world through one seat: {snapshot, screenshot}. "
                "The snapshot carries the tick, whether the game is paused, who "
                "else is in the world, and -- from that seat's body -- its "
                "position, health, reach, inventory, crafting queue, current "
                "walk/mine order, and a capped nearby-entity summary (resources "
                "carry amount, teammates carry their seat name). "
                + _TARGET_DOC
                + " screenshot is null unless screenshot=true, which fills it "
                "with the path of a freshly rendered PNG (see the screenshot "
                "verb) at the cost of a render."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "radius", "type": "integer", "required": False,
                 "description": "1-64 tiles"},
                {"name": "screenshot", "type": "boolean", "required": False},
            ],
            "timeout": 1200.0,  # a first screenshot starts the camera
        },
        {
            "name": "screenshot",
            "effect": "write",
            "tool": "factorio_screenshot",
            "description": (
                "Render what the world looks like around a seat and return "
                "{path} -- a PNG on this machine, which you can read as an "
                "image. Centred on the seat's drone unless x,y say otherwise; "
                "with no seat, x,y are required. The world's camera draws it, "
                "and the first screenshot starts the camera (up to a few "
                "minutes, once per world). width/"
                "height (default 1280x720) are rendered offscreen, so image "
                "quality is not limited by the camera's window; zoom < 1 pulls "
                "back for a factory overview, > 1 pushes in. Lit as noon "
                "unless daytime says otherwise (0 = noon, 0.5 = midnight), so "
                "a night shot is still readable. show_entity_info draws the "
                "alt-mode overlay (recipe icons, contents). Returns the "
                "frame geometry too -- position is the centre, and tiles says "
                "how many tiles wide and tall the image covers -- so a pixel "
                "can be reasoned back to a coordinate. Your drone is the dark "
                "construction robot at the centre of the frame."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "width", "type": "integer", "required": False,
                 "description": "16-4096 px"},
                {"name": "height", "type": "integer", "required": False,
                 "description": "16-4096 px"},
                {"name": "zoom", "type": "number", "required": False,
                 "description": "0.03125-8"},
                {"name": "daytime", "type": "number", "required": False,
                 "description": "0-1"},
                {"name": "show_entity_info", "type": "boolean", "required": False},
                {"name": "surface", "type": "string", "required": False},
                {"name": "file", "type": "string", "required": False},
            ],
            "timeout": 1200.0,  # a first screenshot starts the camera
        },
        {
            "name": "observe_events",
            "effect": "write",
            "tool": "factorio_observe_events",
            "description": (
                "Drain the session's pending events: returns {events: [{kind, "
                "tick?, data}, ...]} accumulated since the last call, then "
                "clears them (consume-once inbox -- world events like arrived/"
                "path_blocked/died/world_saved land here AND stream live on "
                "the 'factorio' emitter). Session-wide: one seat's drain takes "
                "every seat's events, so let one watcher own it."
            ),
            "params": list(_SEAT_TARGET),
        },
        {
            "name": "recipes",
            "effect": "read",
            "tool": "factorio_recipes",
            "description": (
                "List the force's unlocked recipes ({name, ingredients, "
                "products, craftable}). craftable is how many the seat could "
                "hand-craft right now -- 0 means either short of ingredients "
                "or not hand-craftable at all (smelting and assembler-only "
                "recipes are in this list and craft will refuse them). "
                "Bounded: pass search (substring) and/or limit (default 40); "
                "truncated:true means narrow the search. The recipe set is "
                "force-wide, so it is every seat's."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "search", "type": "string", "required": False},
                {"name": "limit", "type": "integer", "required": False},
            ],
        },
        {
            "name": "technologies",
            "effect": "read",
            "tool": "factorio_technologies",
            "description": (
                "List technologies ({name, researched, prerequisites}). "
                "Bounded like recipes; only_unresearched=true filters to the "
                "remaining tree."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "search", "type": "string", "required": False},
                {"name": "only_unresearched", "type": "boolean", "required": False},
                {"name": "limit", "type": "integer", "required": False},
            ],
        },
        # ---- act: world lifecycle (sacred saves) ----
        {
            "name": "world_new",
            "effect": "write",
            "tool": "factorio_world_new",
            "description": (
                "Generate a fresh world and re-exec the engine on it. Discards "
                "unsaved live progress; touches no named save. Optional seed. "
                "Connected seats drop out and must join again."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "seed", "type": "integer", "required": False},
            ],
            "timeout": 600.0,  # engine re-exec + map gen
        },
        {
            "name": "world_save",
            "effect": "write",
            "tool": "factorio_world_save",
            "description": (
                "Snapshot the running world to an immutable named .zip (live "
                "flush, seamless for players). Refuses to clobber unless "
                "overwrite=true. The ONLY verb that writes a named save."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "overwrite", "type": "boolean", "required": False},
            ],
            "timeout": 120.0,  # live save flush + copy
        },
        {
            "name": "world_load",
            "effect": "write",
            "tool": "factorio_world_load",
            "description": (
                "Load a named save and re-exec the engine on a copy of it. The "
                "named save is read-only -- loading can never advance it. "
                "Connected seats drop out and must join again."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
            ],
            "timeout": 600.0,  # engine re-exec from the named save
        },
        # ---- act: live control ----
        {
            "name": "pause",
            "effect": "write",
            "tool": "factorio_pause",
            "description": (
                "Pause or resume the live world (game.tick_paused); with no "
                "'paused' given, flip whichever way it currently is. Returns "
                "{paused} with the resulting state. Pausing stops every seat, "
                "including any human in the world. Omitting it is the only way "
                "to resume from a shell: the CLI renders a boolean as an "
                "on-only flag, so paused=false cannot be typed."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "paused", "type": "boolean", "required": False},
            ],
        },
        {
            "name": "advance",
            "effect": "write",
            "tool": "factorio_advance",
            "description": (
                "Run a paused world forward by ticks at up to speed (default "
                "1000x), then pause it again. Sandbox rules only. Returns "
                "{tick, ticks, wall_s}, or {error: 'TIMEOUT', ...} once "
                "timeout_s (capped at the service maximum) passes; the world is "
                "paused either way."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "ticks", "type": "integer", "required": True},
                {"name": "speed", "type": "number", "required": False},
                {"name": "timeout_s", "type": "number", "required": False},
                *_RULES,
            ],
            "timeout": WAIT_MAX_S + 60.0,
        },
        {
            "name": "wait_until",
            "effect": "read",
            "tool": "factorio_wait_until",
            "description": (
                "Block until a condition holds in the world, or until timeout_s "
                "passes. timeout_s is required and capped at the service maximum "
                "(the reply names the cap used). condition is one of: "
                "{entities_working: {unit_numbers | area, surface?}} -- every "
                "entity is working; {ghosts_built: {area | ids, surface?}} -- no "
                "ghost is left; {tool, args?, path?, op, value?} -- a read tool's "
                "reply at path compares true (op eq, ne, lt, le, gt, ge, truthy, "
                "falsy). Add for_s to require the condition to hold that long. "
                "Polls every poll_s (default 2). Returns {met: true, waited_s, "
                "state} or {error: 'TIMEOUT', waited_s, state}; state is the "
                "last thing observed."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "condition", "type": "object", "required": True},
                {"name": "timeout_s", "type": "number", "required": True},
                {"name": "poll_s", "type": "number", "required": False},
                *_RULES,
            ],
            "timeout": WAIT_MAX_S + 60.0,
        },
        {
            "name": "exec_lua",
            "effect": "write",
            "tool": "factorio_exec_lua",
            "description": (
                "Run Lua in the running world. Pass code inline OR path = a "
                "file on this machine (exactly one) -- a real script does not "
                "survive shell quoting, so scripts belong in files. Returns "
                "{output} = whatever the script printed; to get a value back "
                "call rcon.print(...). `seat` is the resolved seat's name and "
                "`drone` its body (nil while it waits to respawn). UNDER "
                "REALM RULES the script runs guarded: it may read anything, "
                "and every write -- a property assignment, teleport, insert, "
                "create_entity, remote, debug -- fails with an error naming "
                "the call. It changes the world only through `act`, the "
                "seat's drone verbs: act.walk{x=,y=}, act.mine, act.craft, "
                "act.build, act.ghost, act.unghost, act.insert, act.take, "
                "act.throw, act.stop, act.wait, act.observe, act.recipes, "
                "act.drone(). Under sandbox rules it runs in the SCENARIO "
                "context, where the mod's storage is invisible -- reach mod "
                "state through remote.call('game_bot', <fn>, {seat=seat}). "
                "The binding shares your script's first line, so line "
                "numbers still match your file. "
                "Returns cost_ms, the engine script time the command used. A "
                "command over its budget (15 ms by default; one tick is "
                "16.7 ms) returns ok:false with an OVER_BUDGET error: it RAN, "
                "its effects stand and its output is attached, so never "
                "re-run it -- split the work, survey with scan, or pass "
                "budget_ms for a write you know is heavy. Output past "
                "max_output (16 KB by default) is truncated with a marker. "
                "Each seat draws from its own script-time allowance; a seat "
                "that overspends waits for it to refill (see load)."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "code", "type": "string", "required": False},
                {"name": "path", "type": "string", "required": False},
                {"name": "max_output", "type": "integer", "required": False,
                 "description": "characters of output to return, 256-1048576"},
                {"name": "budget_ms", "type": "number", "required": False,
                 "description": "raise this command's budget, up to 50 ms"},
                {"name": "cache", "type": "boolean", "required": False,
                 "description": (
                     "store the script in the world and run it by name, so "
                     "repeat calls send a few dozen bytes instead of the whole "
                     "text (mod >= 0.5; ignored on older worlds). A stored "
                     "script runs in the MOD context: its globals last one "
                     "call, `storage` is a table of its own, and it may not "
                     "register event handlers")},
                *_RULES,
            ],
            "timeout": 120.0,
        },
        # ---- orders (mod >= 0.5): one blocking call instead of a poll loop ----
        {
            "name": "walk",
            "effect": "write",
            "tool": "factorio_walk",
            "description": (
                "Walk the seat's drone straight to (x,y) -- it flies over water, "
                "cliffs and buildings -- and block until the walk ends. Returns the order: status arrived, threat, "
                "blocked, died, stopped or superseded, with result details "
                "(where it stopped; for threat the reason enemy / worm / hp, "
                "the enemy and its distance). The guard stops the seat by "
                "itself within one tick when an enemy comes within "
                "guard_radius (default 24), a worm could reach it, or health "
                "falls under guard_hp (default 0.5 of max); guard=false walks "
                "blind, which is how a seat retreats. status running means "
                "timeout (default 120 s, max 600) passed first: the walk goes "
                "on, and order waits on it. block=false returns the order id "
                "at once. Needs mod >= 0.5. " + _TARGET_DOC
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "guard", "type": "boolean", "required": False},
                {"name": "guard_radius", "type": "number", "required": False,
                 "description": "tiles, 0-64; 0 ignores nearby enemies"},
                {"name": "guard_hp", "type": "number", "required": False,
                 "description": "share of max health, 0-1; 0 ignores health"},
                {"name": "guard_worms", "type": "boolean", "required": False},
                {"name": "timeout", "type": "number", "required": False,
                 "description": "seconds to block, 1-600"},
                {"name": "block", "type": "boolean", "required": False},
            ],
            "timeout": 660.0,
        },
        {
            "name": "wait",
            "effect": "write",
            "tool": "factorio_wait",
            "description": (
                "Block until a condition holds in the world, checked inside "
                "the engine every `every` ticks (default 30). Returns the "
                "order: status met, timeout, gone (the entity was removed) or "
                "error, with result.value. cond is one of: ghosts (x1,y1,x2,y2 "
                "or x,y,radius; met when at most `max` ghosts, default 0, "
                "remain), items (the entity at x,y, or the seat when x,y are "
                "absent; item plus exactly one of at_least / at_most), status "
                "(the entity at x,y; status = a name like working or "
                "no_power, or a list; negate inverts), products (the crafting "
                "machine at x,y finishes `delta` more products), order (another "
                "order id ends), ticks (`ticks` pass). name narrows the entity "
                "at x,y. timeout is seconds (default 60, max 600). block=false "
                "returns the order id at once. Needs mod >= 0.5. " + _TARGET_DOC
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "cond", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "x1", "type": "number", "required": False},
                {"name": "y1", "type": "number", "required": False},
                {"name": "x2", "type": "number", "required": False},
                {"name": "y2", "type": "number", "required": False},
                {"name": "radius", "type": "number", "required": False},
                {"name": "surface", "type": "string", "required": False},
                {"name": "name", "type": "string", "required": False},
                {"name": "item", "type": "string", "required": False},
                {"name": "at_least", "type": "integer", "required": False},
                {"name": "at_most", "type": "integer", "required": False},
                {"name": "status", "type": "array", "required": False},
                {"name": "negate", "type": "boolean", "required": False},
                {"name": "delta", "type": "integer", "required": False},
                {"name": "order", "type": "integer", "required": False},
                {"name": "ticks", "type": "integer", "required": False},
                {"name": "max", "type": "integer", "required": False},
                {"name": "every", "type": "integer", "required": False,
                 "description": "ticks between checks, 5-216000"},
                {"name": "timeout", "type": "number", "required": False,
                 "description": "seconds to block, 1-600"},
                {"name": "block", "type": "boolean", "required": False},
            ],
            "timeout": 660.0,
        },
        {
            "name": "throw",
            "effect": "write",
            "tool": "factorio_throw",
            "description": (
                "Throw `count` capsules (default 1, max 100) of `item` from "
                "the seat's own inventory, one per capsule cooldown, at x,y or "
                "at the seat's feet; blocks until done. Returns the order: "
                "status done, out_of_items, no_items, refused or stopped, "
                "with result.thrown. Keeps defenders or distractors up without "
                "a script per capsule. Needs mod >= 0.5. " + _TARGET_DOC
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "item", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "timeout", "type": "number", "required": False,
                 "description": "seconds to block, 1-600"},
                {"name": "block", "type": "boolean", "required": False},
            ],
            "timeout": 660.0,
        },
        {
            "name": "order",
            "effect": "read",
            "tool": "factorio_order",
            "description": (
                "Read a walk, wait or throw order by id; with timeout "
                "(seconds, max 600) block until it ends. Needs mod >= 0.5."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "order", "type": "integer", "required": True},
                {"name": "timeout", "type": "number", "required": False},
            ],
            "timeout": 660.0,
        },
        {
            "name": "blueprint_stamp",
            "effect": "write",
            "tool": "factorio_blueprint_stamp",
            "description": (
                "Stamp a blueprint string at (x,y) on the shared force. Pass "
                "blueprint inline OR path = a file on this machine (exactly "
                "one) -- a blueprint string is long and base64-shaped, which "
                "is what shell quoting destroys. Places GHOSTS by default, "
                "which is what a player does; build=true revives them "
                "immediately, pays no items, and is flagged cheated:true. "
                "Returns {entities, placed, skipped}: stamping onto occupied "
                "ground fails per entity, and skipped>0 means the module "
                "landed incomplete. Stamping is ALL OR NOTHING: if one "
                "entity is obstructed the engine places ZERO, so a module "
                "that fails entirely usually means a single tree in the way. "
                "blocked/blocked_at say how many and where; clear=true "
                "removes trees and rocks in the footprint first, as a "
                "player's construction bots would. chunk_generated=false is "
                "the other way to place nothing -- the target is map the "
                "engine has not generated yet. This is the precision verb: "
                "design something, then put it down exactly."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "blueprint", "type": "string", "required": False},
                {"name": "path", "type": "string", "required": False},
                {"name": "direction", "type": "string", "required": False},
                {"name": "build", "type": "boolean", "required": False},
                {"name": "clear", "type": "boolean", "required": False},
                {"name": "build_mode", "type": "string", "required": False,
                 "description": "ghost (default) or built; built is refused under realm rules"},
                {"name": "force_build", "type": "boolean", "required": False},
                {"name": "surface", "type": "string", "required": False,
                 "description": "needed on a lab (default nauvis): no seat to take it from"},
                {"name": "force", "type": "string", "required": False,
                 "description": "force that owns the ghosts on a lab (default player)"},
                {"name": "budget_ms", "type": "number", "required": False,
                 "description": "raise this command's budget, up to 50 ms"},
                *_RULES,
            ],
            "timeout": 180.0,
        },
        {
            "name": "blueprint_capture",
            "effect": "write",
            "tool": "factorio_blueprint_capture",
            "description": (
                "Capture a region of the world as a blueprint string, so one "
                "agent can design something and hand it to another. Give "
                "radius (a square around the seat, or around x,y) or the "
                "corners x1,y1,x2,y2. Returns {path} -- a file, which is what "
                "blueprint_stamp --path wants; the string itself is inlined "
                "only when it is short. save_as writes it where you choose. "
                "include_tiles captures concrete/bricks as well as entities."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "radius", "type": "number", "required": False,
                 "description": "1-200 tiles"},
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "x1", "type": "number", "required": False},
                {"name": "y1", "type": "number", "required": False},
                {"name": "x2", "type": "number", "required": False},
                {"name": "y2", "type": "number", "required": False},
                {"name": "label", "type": "string", "required": False},
                {"name": "include_tiles", "type": "boolean", "required": False},
                {"name": "save_as", "type": "string", "required": False},
                {"name": "surface", "type": "string", "required": False},
                {"name": "budget_ms", "type": "number", "required": False,
                 "description": "raise this command's budget, up to 50 ms"},
            ],
            "timeout": 180.0,
        },
        {
            "name": "scan",
            "effect": "read",
            "tool": "factorio_scan",
            "description": (
                "Survey entities in an area, in pages. Give the corners "
                "x1,y1,x2,y2, or radius around x,y (or around the seat). "
                "Filters: name and type (one or a list), force. fields picks "
                "what each entity carries (default name, position): name, "
                "type, position, direction, status, recipe, unit_number, "
                "health, ghost_name, amount, contents, force. Returns up to "
                "limit entities (default 200, max 5000) plus next, a cursor: "
                "pass it back with the same filters for the next page, until "
                "next is null (complete:true). count_only:true returns just "
                "the count. The realm splits the work into commands small "
                "enough never to stall a tick, so a big area takes longer, "
                "never fails. Pages are not a snapshot: something built or "
                "removed between pages can be missed or seen twice. Use this "
                "instead of an exec_lua find_entities_filtered over a large "
                "area. " + _TARGET_DOC + " A seat is optional."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x1", "type": "number", "required": False},
                {"name": "y1", "type": "number", "required": False},
                {"name": "x2", "type": "number", "required": False},
                {"name": "y2", "type": "number", "required": False},
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "radius", "type": "number", "required": False,
                 "description": "1-4096 tiles"},
                {"name": "surface", "type": "string", "required": False},
                {"name": "name", "type": "array", "required": False},
                {"name": "type", "type": "array", "required": False},
                {"name": "force", "type": "string", "required": False},
                {"name": "fields", "type": "array", "required": False},
                {"name": "limit", "type": "integer", "required": False,
                 "description": "1-5000, default 200"},
                {"name": "cursor", "type": "string", "required": False},
                {"name": "count_only", "type": "boolean", "required": False},
            ],
            "timeout": 120.0,
        },
        {
            "name": "load",
            "effect": "read",
            "tool": "factorio_load",
            "description": (
                "The game-call meters: server UPS, the limits in force, and "
                "per connection (each seat, 'anon' for seatless calls) the "
                "engine script ms/s it is spending, its allowance level and "
                "debt, calls queued and in flight, over-budget count, and the "
                "last time the throttle held it back and why. A seat in debt "
                "waits; this is where to see it."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
        },
        # ---- act: as a player (a seat drives all of these) ----
        {
            "name": "move",
            "effect": "write",
            "tool": "factorio_move",
            "description": (
                "Start the seat's drone walking straight to (x,y) and return "
                "the walk order at once: walk with block=false. Poll order or "
                "observe, or watch the emitter for 'arrived'. " + _TARGET_DOC
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
            ],
        },
        {
            "name": "stop",
            "effect": "write",
            "tool": "factorio_stop",
            "description": (
                "Halt the seat where it stands: clears its walk target and any "
                "mining order."
            ),
            "params": list(_SEAT_TARGET),
        },
        {
            "name": "teleport",
            "effect": "write",
            "tool": "factorio_teleport",
            "description": (
                "Put the seat at (x,y) instantly. A CHEAT and flagged as one "
                "(cheated:true) -- precise placement is the thing an agent is "
                "allowed to be better at than a human, but travel is not. Use "
                "move to get somewhere."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "surface", "type": "string", "required": False},
                *_RULES,
            ],
        },
        {
            "name": "respawn",
            "effect": "write",
            "tool": "factorio_respawn",
            "description": (
                "Give a seat a drone again, now, at its force's spawn: skips "
                "the 10 s respawn delay after a death. A seat whose drone is "
                "alive is left where it is."
            ),
            "params": list(_SEAT_TARGET),
            "timeout": 60.0,
        },
        {
            "name": "mine",
            "effect": "write",
            "tool": "factorio_mine",
            "description": (
                "Mine the resource/tree/rock nearest (x,y) into the seat's own "
                "inventory (must be within reach -- move there first). This is "
                "real mining: the engine extracts at the game's own rate and "
                "decrements the patch itself, so it takes time. count = units "
                "to extract (default: until the target is gone). Returns what "
                "the order started; watch observe.mining and the inventory."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "name", "type": "string", "required": False},
                {"name": "count", "type": "integer", "required": False},
            ],
        },
        {
            "name": "craft",
            "effect": "write",
            "tool": "factorio_craft",
            "description": (
                "Queue a handcraft on the seat's real crafting queue (consumes "
                "ingredients, ticks down, yields into inventory). Because it is "
                "the real queue it counts toward production statistics, which "
                "is what makes craft-item research triggers fire on their own. "
                "Returns {queued}; watch progress via observe's crafting list."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "recipe", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
            ],
        },
        {
            "name": "build",
            "effect": "write",
            "tool": "factorio_build",
            "description": (
                "Place an item from the seat's inventory as an entity at (x,y) "
                "(within reach, collision-checked; the item is consumed only on "
                "success). direction: north/east/south/west (default north)."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "name", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "direction", "type": "string", "required": False},
            ],
        },
        {
            "name": "insert",
            "effect": "write",
            "tool": "factorio_insert",
            "description": (
                "Move items from the seat's inventory into the entity at (x,y) "
                "(within reach). The engine routes to the right slot -- coal "
                "into a furnace lands in fuel, ore in the smelt slot. Optional "
                "target narrows by entity name. Returns {inserted, target}."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
                {"name": "target", "type": "string", "required": False},
            ],
        },
        {
            "name": "take",
            "effect": "write",
            "tool": "factorio_take",
            "description": (
                "Take items from the entity at (x,y) into the seat's inventory "
                "(within reach; e.g. plates out of a furnace). Default count = "
                "all available. Returns {taken, from}."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
                {"name": "target", "type": "string", "required": False},
            ],
        },
        {
            "name": "research",
            "effect": "write",
            "tool": "factorio_research",
            "description": (
                "Unlock a technology directly (CHEAT path, flagged "
                "cheated:true). Seats craft through the real queue now, so "
                "craft-item trigger technologies advance on their own -- prefer "
                "earning them. One shared force means this unlocks the tech for "
                "EVERY seat and any human in the world. Prerequisites are NOT "
                "auto-unlocked."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "name", "type": "string", "required": True},
                *_RULES,
            ],
        },
        # ---- tool library and lab mod (mod >= 0.6) ----
        {
            "name": "tool",
            "effect": "write",
            "tool": "factorio_tool",
            "description": (
                "Run a library tool by name with args. Returns {ok, r} where r "
                "is the tool's table, or {ok:false, error: no_tool | "
                "tool_error, detail}. Needs game-bot-control 0.6. Under realm "
                "rules every tool runs guarded like exec_lua: a read tool "
                "reads, an act tool also gets `act` for the seat, and tools "
                "with effect cheat, or that use a cheat lib, are refused. See "
                "tool_list."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "name", "type": "string", "required": True},
                {"name": "args", "type": "object", "required": False},
                {"name": "budget_ms", "type": "number", "required": False},
                *_RULES,
            ],
            "timeout": 130.0,
        },
        {
            "name": "tool_list",
            "effect": "read",
            "tool": "factorio_tool_list",
            "description": (
                "List the world's tools: name, kind, effect, summary, size, "
                "calls, errors, last_error, defined. Needs game-bot-control 0.6."
            ),
            "params": list(_SEAT_TARGET),
        },
        {
            "name": "tool_sync",
            "effect": "write",
            "tool": "factorio_tool_sync",
            "description": (
                "Define the tool files (RLM_FACTORIO_TOOLS, every *.lua) in the "
                "world, libs first. Pass names to define only those. Returns "
                "{defined, errors}. Needs game-bot-control 0.6."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "names", "type": "array", "required": False},
            ],
            "timeout": 120.0,
        },
        {
            "name": "sbx",
            "effect": "write",
            "tool": "factorio_sbx",
            "description": (
                "Call a verb of the fxlab-sbx lab mod: remote.call('sbx', verb, "
                "args). Needs a world that loads it: a lab. Under "
                "realm rules (rules='realm') only the reads (sandbox_list, verbs, "
                "slice_list, slice_ports, slice_fingerprint, slice_area_of_base, "
                "slice_timing, bonuses, wave_status, ledger, view_shot, "
                "view_booth) and sbx_mode mode=standard run; the rest is refused."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "verb", "type": "string", "required": True},
                {"name": "args", "type": "object", "required": False},
                *_RULES,
            ],
            "timeout": 130.0,
        },
    ],
    "emitters": [
        {
            "topic": "factorio",
            "description": (
                "Fires on a realm-side world event. Payload {session_id, kind, "
                "tick?, data} — projected as rlm.factorio.<kind>. Kinds: "
                "'world_loaded'/'world_saved'/'error' (fired by the world "
                "verbs), 'seat_joined'/'seat_left' (fired by the seat verbs), "
                "plus the mod's in-world events ('spawned', 'arrived', "
                "'died', ...) drained by a background pump."
            ),
        },
    ],
    "sessions": [],
}

# ---- emit plumbing ---------------------------------------------------------
#
# Handlers run sync in worker threads (ServiceAdapter dispatches via
# asyncio.to_thread), so firing the emitter means hopping back onto the
# adapter's loop. main() binds these globals before serving.

_ADAPTER: ServiceAdapter | None = None
_LOOP: asyncio.AbstractEventLoop | None = None

# How often the background pump drains the mod's ring buffer per ready session.
EVENTS_POLL_S = float(os.environ.get("AWM_FACTORIO_EVENTS_POLL_S", "2.0"))


# Per-session consume-once inbox: every fired event lands here too, so a
# polling consumer (observe_events) never races the pump for the world buffer.
# Best-effort telemetry — a service respawn starts it empty.
_EVENTS_LOCK = threading.Lock()
_EVENT_BUF: dict[str, list[dict]] = {}
_EVENT_BUF_CAP = 256


def _fire(session_id: str, kind: str, data: dict | None = None,
          tick: int | None = None) -> None:
    """Fire one event: append to the session's inbox and emit on the 'factorio'
    topic (threadsafe, best-effort — emit is live signalling, never durable
    delivery). Callable from sync handlers and pump threads alike."""
    event: dict[str, Any] = {"kind": kind, "data": data or {}}
    if tick is not None:
        event["tick"] = tick
    with _EVENTS_LOCK:
        buf = _EVENT_BUF.setdefault(session_id, [])
        buf.append(event)
        del buf[:-_EVENT_BUF_CAP]
    adapter, loop = _ADAPTER, _LOOP
    if adapter is None or loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            adapter.emit("factorio", {"session_id": session_id, **event}), loop)
    except Exception:  # noqa: BLE001 — never let telemetry break the verb
        log.debug("emit %s dropped", kind, exc_info=True)


def _drain_events(row: dict) -> list[dict]:
    """Drain the appliance's ring buffer; [] when the engine/RCON isn't ready
    (a re-exec window, or the container just came up) — the pump retries."""
    try:
        result, _ = _iface(row, "drain_events", {}, key=SYS, metered=False,
                           timeout=10.0)
    except Exception:  # noqa: BLE001
        return []
    events = result.get("events") or []
    return events if isinstance(events, list) else []


def _collect_and_fire(row: dict) -> None:
    """Move everything in the appliance's ring buffer into the inbox + emitter.
    Sync — the pump calls it via to_thread; observe_events calls it directly."""
    for ev in _drain_events(row):
        _fire(row["session_id"], ev.get("kind") or "event",
              ev.get("data") or {}, tick=ev.get("tick"))


async def _events_pump(adapter: ServiceAdapter) -> None:
    """Drain each ready session's ring buffer into the inbox + 'factorio' topic,
    forever. Runs beside adapter.run(); all blocking I/O is offloaded."""
    while True:
        try:
            rows = await asyncio.to_thread(
                lambda: [r for r in dao.FactorioDAO().live_sessions()
                         if r["status"] == "ready"])
            for row in rows:
                await asyncio.to_thread(_collect_and_fire, row)
        except Exception:  # noqa: BLE001 — keep pumping across transient faults
            log.debug("events pump iteration failed", exc_info=True)
        await asyncio.sleep(EVENTS_POLL_S)

def _require_session(session_id: str) -> dict:
    """Look up a session or raise — used by act/perceive verbs."""
    row = dao.FactorioDAO().get_session(session_id)
    if row is None:
        raise ValueError(f"unknown session_id: {session_id!r}")
    return row


# ---- game calls ------------------------------------------------------------
#
# Every game call names a connection key: a seat's player name, the world and
# calling session for a seatless call, or SYS for the realm's own bookkeeping
# (join polling, the events pump, the UPS sampler). Each key is its own engine
# socket and its own throttle meter; SYS alone is unmetered. Each world has its
# own throttle, so one world's UPS never holds another's callers.

SYS = "sys"
ANON = "anon"
SCANNER = scan.Scanner()
UPS_POLL_S = 1.0
HANDLER_THREADS = int(os.environ.get("AWM_FACTORIO_HANDLER_THREADS", "64"))
# A seatless caller's socket closes once it has gone this long unused.
CONN_IDLE_S = float(os.environ.get("AWM_FACTORIO_CONN_IDLE_S", "600"))

_CALLER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rlm_factorio_caller", default=None)
_THROTTLES: dict[str, throttle.Throttle] = {}
_THROTTLES_LOCK = threading.Lock()
_CONN_USED: dict[tuple[str, str], float] = {}


def _throttle(row: dict) -> throttle.Throttle:
    container = row.get("container_name") or appliance.CONTAINER
    with _THROTTLES_LOCK:
        t = _THROTTLES.get(container)
        if t is None:
            t = _THROTTLES[container] = throttle.Throttle()
        return t


def _key(seat: dict | None, row: dict) -> str:
    """The connection and throttle key: the seat, or the seatless caller's own
    on this world."""
    if seat:
        return seat["player_name"]
    return f"{row['container_name']}/{_CALLER.get() or ANON}"


def _run(row: dict, key: str, code: str, *, timeout: float = 60.0,
         metered: bool = True) -> dict:
    """Run Lua on ``key``'s socket. Returns {output, cost_ms}."""
    deadline = time.monotonic() + timeout
    gate = _throttle(row)
    if "/" in key:
        _CONN_USED[(row["container_name"], key)] = time.monotonic()
    if metered:
        try:
            gate.admit(key, deadline)
        except throttle.Throttled as exc:
            raise appliance.ApplianceError(str(exc)) from None
    try:
        result = broker.for_row(row).lua(
            key, code, timeout=max(1.0, deadline - time.monotonic()))
    except Exception:
        if metered:
            gate.release(key)
        raise
    if metered:
        gate.charge(key, result["cost_ms"])
    return result


def _close_idle_conns() -> list[str]:
    """Close the sockets of seatless callers that have gone quiet."""
    now = time.monotonic()
    closed = []
    for (container, key), used in list(_CONN_USED.items()):
        if now - used < CONN_IDLE_S:
            continue
        _CONN_USED.pop((container, key), None)
        conn = broker.existing(container)
        if conn is None:
            continue
        try:
            conn.close_conn(key)
            closed.append(key)
        except Exception:  # noqa: BLE001
            log.debug("closing idle socket %s failed", key, exc_info=True)
    return closed


def _iface_code(fn: str, args: dict, mod: str = "game_bot") -> str:
    """``remote.call(mod, fn, args)`` with args carried as JSON, so no
    argument ever needs a Lua-literal encoding."""
    blob = json.dumps(args).replace("\\", "\\\\").replace("'", "\\'")
    return ("local h=helpers "
            f"local r=remote.call('{mod}','{fn}',h.json_to_table('{blob}') or {{}}) "
            "rcon.print(h.table_to_json(r))")


def _iface(row: dict, fn: str, args: dict, *, key: str, timeout: float = 60.0,
           metered: bool = True, mod: str = "game_bot") -> tuple[dict, float | None]:
    """Call one remote function of the engine. Returns (result, cost_ms)."""
    res = _run(row, key, _iface_code(fn, args, mod), timeout=timeout, metered=metered)
    out = res["output"].strip()
    try:
        return json.loads(out), res["cost_ms"]
    except ValueError:
        # A Lua error comes back as the engine's plain error text.
        raise appliance.ApplianceError(out or f"{fn}: empty reply") from None


def _over_budget(row: dict, key: str, cost_ms: float | None,
                 budget_ms: float) -> str | None:
    """The OVER_BUDGET message, or None. Sandbox rules count the overrun and
    refuse nothing: lab fixtures run heavy Lua on purpose."""
    if cost_ms is None or cost_ms <= budget_ms:
        return None
    _throttle(row).note_over_budget(key)
    if row.get("rules") == "sandbox":
        return None
    log.info("over budget: %s used %.1f ms (budget %.0f)", key, cost_ms, budget_ms)
    return throttle.over_budget_message(cost_ms, budget_ms)


def _budgeted(row: dict, key: str, result: Any, cost_ms: float | None,
              budget_ms: float) -> Any:
    """A verb's result, or the OVER_BUDGET envelope that still carries it."""
    msg = _over_budget(row, key, cost_ms, budget_ms)
    if msg is None:
        return result
    return {"ok": False, "ran": True, "error": msg, "cost_ms": cost_ms,
            "result": result}


def _close_conn(seat: dict) -> None:
    """Close a departing seat's engine socket. Best-effort."""
    session = dao.FactorioDAO().get_session(seat["session_id"])
    if session is None:
        return
    try:
        broker.for_row(session).close_conn(seat["player_name"])
    except Exception:  # noqa: BLE001
        log.debug("closing %s's socket failed", seat["player_name"], exc_info=True)


def _sample_ups() -> None:
    """Feed each ready world's tick to its own throttle. A paused world reports
    no UPS, so its stillness never reads as overload."""
    for row in dao.FactorioDAO().live_sessions():
        if row["status"] != "ready":
            continue
        gate = _throttle(row)
        try:
            out = _run(row, SYS, 'rcon.print(game.tick .. " " .. tostring(game.tick_paused))',
                       timeout=5.0, metered=False)["output"].split()
            gate.note_tick(int(out[0]), out[1] == "true")
        except Exception:  # noqa: BLE001 — a dark engine just means no gate
            gate.forget_ups()


async def _ups_sampler() -> None:
    """Feed the throttles' UPS gates, forever."""
    while True:
        await asyncio.sleep(UPS_POLL_S)
        try:
            await asyncio.to_thread(_sample_ups)
        except Exception:  # noqa: BLE001
            log.debug("UPS sample failed", exc_info=True)


# ---- lifecycle -----------------------------------------------------------

MAX_LABS = int(os.environ.get("RLM_FACTORIO_MAX_LABS", "3"))
_ACQUIRE_LOCK = threading.Lock()


def _stop_row(d: dao.FactorioDAO, row: dict) -> None:
    d.set_status(row["session_id"], "stopped")
    for seat in d.live_seats(row["session_id"]):
        d.set_seat(seat["seat_id"], status="stopped")


def _acquire(args: dict) -> dict:
    """Bring a world up (idempotent per name) and return its session id.

    A live world of the same name is adopted, erroring on a kind or game
    mismatch. Otherwise mint a row on fresh coordinates, bring its compose
    project up and wait for the engine. A lab past the cap is refused at once
    with LAB_CAP, so the caller decides how long to wait.
    """
    game = str(args.get("game") or "").strip()
    kind = args.get("kind") or "headless"
    lab = kind == "lab"
    name = args.get("name") or ("" if lab else appliance.REALM_NAME)
    if not name:
        raise ValueError("a lab needs a name")
    rules = args.get("rules") or ("sandbox" if lab else "realm")
    if kind == "rendered" and rules == "sandbox":
        raise ValueError("a rendered world runs realm rules")
    save, create = args.get("save"), bool(args.get("create"))
    if save and create:
        raise ValueError("pass save or create, not both")
    if lab and not (save or create):
        raise ValueError("a lab boots on a save, or on a new world with create=true")
    live = args.get("live")
    live = not lab if live is None else bool(live)
    d = dao.FactorioDAO()

    with _ACQUIRE_LOCK:
        rows = d.live_sessions()
        for row in list(rows):
            if row["name"] != name or (row["kind"] == "lab") != lab:
                continue
            if not appliance.is_container_running(row):
                _stop_row(d, row)
                rows.remove(row)
                continue
            if row["kind"] != kind:
                raise ValueError(
                    f"world {name!r} is live as {row['kind']}; release it "
                    f"before acquiring it as {kind}")
            if game and row["game"] and row["game"] != game:
                raise ValueError(
                    f"world {name!r} is bound to game {row['game']!r}; "
                    f"release it before acquiring {game!r}")
            log.info("acquire: adopting live session %s", row["session_id"])
            return {"session_id": row["session_id"], "adopted": True,
                    "kind": row["kind"], "rules": row["rules"], "name": name}
        labs = [r for r in rows if r["kind"] == "lab"]
        if lab and len(labs) >= MAX_LABS:
            raise appliance.ApplianceError(
                f"LAB_CAP: {len(labs)} labs running, cap {MAX_LABS}; retry later")
        taken = {p for r in rows for p in (r["control_port"], r["game_port"])}
        row = d.create_session(
            game, rcon_port=appliance.RCON_PORT, kind=kind, rules=rules,
            name=name, mods=appliance.world_mods(kind),
            **appliance.world_coords(kind, name, taken))
    sid = row["session_id"]
    boot = {"paused": not live, "create": create}
    try:
        if save:
            boot["save"] = appliance.stage_save(row, str(save))
        log.info("acquire: bringing %s world %s up for session %s", kind, name, sid)
        appliance.compose_up(row, boot)
        st = appliance.wait_ready(row)
    except Exception:
        d.set_status(sid, "error")
        try:
            appliance.compose_down(row)
        except Exception:  # noqa: BLE001 — the bring-up error is the one to report
            log.debug("acquire: tearing down %s failed", sid, exc_info=True)
        raise
    d.set_runtime(sid, status="ready", current_world=st.get("current_world"))
    out = {"session_id": sid, "adopted": False, "kind": kind, "rules": rules,
           "name": name}
    if kind == "rendered":
        out["game_port"] = row["game_port"]
    if lab:
        out["saves_dir"] = str(appliance.saves_dir(row))
    return out


def _release(args: dict) -> dict:
    """Tear the session down, seats first.

    Seats are attached to the session's compose network, so a seat still
    running would block the network's removal — and a seat outliving its world
    is a leaked core either way.
    """
    sid = args["session_id"]
    row = _require_session(sid)
    d = dao.FactorioDAO()
    released_seats = []
    for seat in d.live_seats(sid):
        if seat["container_name"]:
            appliance.seat_stop(seat["container_name"])
        d.set_seat(seat["seat_id"], status="stopped")
        released_seats.append(seat["seat_id"])
    broker.drop(row["container_name"])
    with _THROTTLES_LOCK:
        _THROTTLES.pop(row["container_name"], None)
    _forget_mod_state(sid)
    appliance.compose_down(row)
    d.set_status(sid, "stopped")
    return {"released": True, "session_id": sid, "seats": released_seats}


def _reset(args: dict) -> dict:
    sid = args["session_id"]
    row = _require_session(sid)
    result = _world_op(sid, row, "reset", "/new", {})
    dao.FactorioDAO().set_runtime(sid, status="ready", current_world=None)
    _fire(sid, "world_loaded", {"world": None})
    return {"session_id": sid, "status": "ready", "result": result}


def _status(args: dict) -> dict:
    d = dao.FactorioDAO()
    sid = args.get("session_id")
    rows = [d.get_session(sid)] if sid else d.list_sessions()
    rows = [r for r in rows if r]
    out = []
    for row in rows:
        enriched = dict(row)
        if row["status"] not in ("stopped", "error"):
            live = appliance.status(row)
            if live is not None:
                enriched["appliance"] = live
        out.append(enriched)
    return {"sessions": out}


# ---- seats ----------------------------------------------------------------

# How long to wait for a started camera container to appear in-world as a
# connected player. Container start + map transfer + world load measured ~25s.
SEAT_JOIN_TIMEOUT = float(os.environ.get("AWM_FACTORIO_SEAT_JOIN_TIMEOUT", "240"))

CAMERA_NAME = os.environ.get("AWM_FACTORIO_CAMERA_NAME", "fxcam")
DRONE_MOD = (0, 7, 0)
_CAMERA_LOCKS: dict[str, threading.RLock] = {}
_CAMERA_LOCKS_LOCK = threading.Lock()


def _require_seat(seat_id: str) -> dict:
    row = dao.FactorioDAO().get_seat(seat_id)
    if row is None:
        raise ValueError(f"unknown seat_id: {seat_id!r}")
    return row


def _lua(row: dict, code: str, *, timeout: float = 20.0) -> str:
    """Run the realm's own Lua in the scenario context, returning its output."""
    return _run(row, SYS, code, timeout=timeout, metered=False)["output"].strip()


def _player_index(row: dict, player_name: str) -> int:
    """Resolve the camera's player to an index, or 0 while it is not yet connected.

    The name is asserted by the camera itself before the client starts, so this
    is a lookup rather than a diff of who appeared.
    """
    name = player_name.replace('"', '')
    out = _lua(row, f'local p = game.players["{name}"] '
                    f'rcon.print((p and p.connected) and p.index or 0)')
    try:
        return int(out.split()[0])
    except (ValueError, IndexError):
        return 0


def _wait_seat_connected(row: dict, seat: dict, container: str,
                         timeout: float = SEAT_JOIN_TIMEOUT) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not appliance.seat_container_running(container):
            raise appliance.ApplianceError(
                f"camera container {container} exited before joining: "
                f"{appliance.seat_logs(container)[-800:]}"
            )
        try:
            index = _player_index(row, seat["player_name"])
        except Exception:  # noqa: BLE001 — RCON is unavailable during a re-exec
            index = 0
        if index:
            return index
        time.sleep(2.0)
    raise appliance.ApplianceError(
        f"camera {seat['seat_id']} did not join within {timeout:.0f}s: "
        f"{appliance.seat_logs(container)[-800:]}"
    )


def _park_camera(row: dict, seat: dict, timeout: float = SEAT_JOIN_TIMEOUT) -> None:
    """Make the camera a spectator with no character, so it changes nothing the
    world simulates. A lab parks it in the lab mod's view booth, off every play
    surface. Retried while the server has not yet created the joined player."""
    fn, mod = ("view_booth", "sbx") if row["kind"] == "lab" else ("camera", "game_bot")
    deadline = time.monotonic() + timeout
    while True:
        res, _ = _iface(row, fn, {"player": seat["player_name"]},
                        key=SYS, metered=False, mod=mod)
        if not (isinstance(res, dict) and res.get("ok") is False):
            return
        if res.get("error") != "not_found" or time.monotonic() > deadline:
            raise appliance.ApplianceError(
                f"camera {seat['seat_id']} could not park: {res.get('error')}")
        time.sleep(0.5)


def _start_camera(row: dict, seat: dict, container: str) -> int:
    """Run the camera's client, wait for it in-world, and park it."""
    appliance.camera_run(container, appliance.seat_network(row["compose_project"]),
                         seat["player_name"],
                         output_dir=appliance.seat_output_dir(seat["seat_id"]),
                         mods=appliance.row_mods(row))
    index = _wait_seat_connected(row, seat, container)
    _park_camera(row, seat)
    return index


def _require_drones(row: dict) -> None:
    version = _mod_version(row)
    if version < DRONE_MOD:
        raise appliance.ApplianceError(
            "drone seats need game-bot-control 0.7; this world runs "
            f"{'.'.join(map(str, version))}, and 0.7 arrives with the next "
            "appliance restart")


def _spawn(row: dict, seat: dict) -> dict:
    """Give a drone seat its body; the mod creates the seat on first call."""
    res, _ = _iface(row, "spawn", {"seat": seat["player_name"]}, key=SYS,
                    metered=False)
    return res


def _despawn(seat: dict) -> None:
    """Remove a drone from its world. Best-effort: a world that is down or has
    already lost the seat is the outcome we wanted."""
    row = dao.FactorioDAO().get_session(seat["session_id"])
    if row is None or row["status"] in ("stopped", "error"):
        return
    try:
        _iface(row, "despawn", {"seat": seat["player_name"]}, key=SYS,
               metered=False, timeout=20.0)
    except Exception:  # noqa: BLE001
        log.debug("despawning %s failed", seat["player_name"], exc_info=True)


def _camera_lock(sid: str) -> threading.RLock:
    with _CAMERA_LOCKS_LOCK:
        return _CAMERA_LOCKS.setdefault(sid, threading.RLock())


def _join(args: dict, as_: str | None = None) -> dict:
    """Allocate a seat on a session: spawn a drone, or start the camera.

    The gateway threads the calling identity in as ``as_`` (a placed agent's
    placement id; absent for a call that never crossed an edge, i.e. the host's
    own CLI), and that becomes the seat's owner unless the caller named one.
    Ownership is recorded, never enforced: the LAN-party bargain is one shared
    force with no protection between players, and a shell has no identity to
    check against anyway. It exists so `seats` can say whose seat this is and
    so a reclaimed seat can be reported against the agent that lost it.

    The camera joins under the camera's fixed name, so a save seeded with it
    loads unchanged.
    """
    sid = args["session_id"]
    row = _require_session(sid)
    if row["status"] in ("stopped", "error"):
        raise ValueError(f"session {sid} is {row['status']}; acquire it first")
    role = args.get("role") or "drone"
    owner = str(args.get("owner") or as_ or "").strip()
    d = dao.FactorioDAO()
    if role == "camera":
        with _camera_lock(sid):
            if any(s["role"] == "camera" for s in d.live_seats(sid)):
                raise ValueError(f"session {sid} already has a camera seat")
            seat = d.create_seat(sid, owner=owner, role=role, player_name=CAMERA_NAME)
            container = appliance.seat_container_name(row["compose_project"],
                                                      seat["seat_id"])
            d.set_seat(seat["seat_id"], container_name=container)
            try:
                index = _start_camera(row, seat, container)
            except Exception:
                appliance.seat_stop(container)
                d.set_seat(seat["seat_id"], status="error")
                raise
            d.set_seat(seat["seat_id"], status="ready", player_index=index)
        out = {"player_index": index,
               "output_dir": str(appliance.seat_output_dir(seat["seat_id"]))}
    else:
        _require_drones(row)
        seat = d.create_seat(sid, owner=owner, role=role)
        try:
            spawned = _spawn(row, seat)
        except Exception:
            d.set_seat(seat["seat_id"], status="error")
            raise
        d.set_seat(seat["seat_id"], status="ready")
        out = {"position": spawned.get("position"), "surface": spawned.get("surface")}
    seat_id = seat["seat_id"]
    d.touch_seat(seat_id)
    _fire(sid, "seat_joined", {"seat_id": seat_id, "player_name": seat["player_name"],
                               "role": role})
    log.info("join: %s seat %s as %s for owner %r", role, seat_id,
             seat["player_name"], owner)
    return {"seat_id": seat_id, "session_id": sid, "player_name": seat["player_name"],
            "owner": owner, "role": role, **out}


def _camera_for(row: dict) -> dict:
    """The world's camera seat, started now when it has none."""
    sid = row["session_id"]
    d = dao.FactorioDAO()
    with _camera_lock(sid):
        for seat in d.live_seats(sid):
            if seat["role"] == "camera":
                return seat
        joined = _join({"session_id": sid, "role": "camera", "owner": "screenshot"})
        return d.get_seat(joined["seat_id"])


def _rejoin_one(row: dict, seat: dict) -> dict:
    d = dao.FactorioDAO()
    if seat["role"] == "drone":
        try:
            spawned = _spawn(row, seat)
        except Exception as exc:  # noqa: BLE001
            return {"seat_id": seat["seat_id"], "error": str(exc)}
        d.touch_seat(seat["seat_id"])
        return {"seat_id": seat["seat_id"], "position": spawned.get("position"),
                "created": spawned.get("created")}
    container = seat["container_name"] or appliance.seat_container_name(
        row["compose_project"], seat["seat_id"])
    _close_conn(seat)
    appliance.seat_stop(container)
    try:
        index = _start_camera(row, seat, container)
    except Exception as exc:  # noqa: BLE001
        appliance.seat_stop(container)
        d.set_seat(seat["seat_id"], status="error")
        return {"seat_id": seat["seat_id"], "error": str(exc)}
    d.set_seat(seat["seat_id"], status="ready", container_name=container,
               player_index=index)
    d.touch_seat(seat["seat_id"])
    _fire(row["session_id"], "seat_joined", {"seat_id": seat["seat_id"],
                                             "player_name": seat["player_name"],
                                             "role": "camera", "rejoined": True})
    return {"seat_id": seat["seat_id"], "player_index": index}


def _rejoin(args: dict) -> dict:
    """Bring seats back under their own names after a world reload.

    A loaded save holds the drones it was saved with, and the mod respawns
    any it lacks under the same seat name; the camera's client dropped with
    the reload and starts again. ``parallel`` (default 1) bounds how many
    rejoin at once.
    """
    if args.get("seat_id"):
        seats = [_require_seat(args["seat_id"])]
        row = _require_session(seats[0]["session_id"])
    elif args.get("session_id"):
        row = _require_session(args["session_id"])
        seats = dao.FactorioDAO().live_seats(row["session_id"])
    else:
        raise ValueError("rejoin needs seat_id, or session_id for every live seat")
    with ThreadPoolExecutor(max_workers=int(args.get("parallel") or 1)) as pool:
        return {"seats": list(pool.map(lambda seat: _rejoin_one(row, seat), seats))}


def _end_seat(seat: dict) -> None:
    """Take a seat out of its world: despawn the drone, or stop the camera."""
    _close_conn(seat)
    if seat["role"] == "drone":
        _despawn(seat)
    elif seat["container_name"]:
        appliance.seat_stop(seat["container_name"])


def _leave(args: dict) -> dict:
    seat_id = args["seat_id"]
    seat = _require_seat(seat_id)
    _end_seat(seat)
    dao.FactorioDAO().set_seat(seat_id, status="stopped")
    _fire(seat["session_id"], "seat_left", {"seat_id": seat_id,
                                            "player_name": seat["player_name"]})
    return {"released": True, "seat_id": seat_id}


def _world_seats(session: dict | None) -> dict[str, dict] | None:
    """The world's own drone registry by seat name, or None when it is dark."""
    if session is None or session["status"] in ("stopped", "error"):
        return None
    try:
        res, _ = _iface(session, "seats", {}, key=SYS, metered=False, timeout=10.0)
    except Exception:  # noqa: BLE001 — a dark appliance is not an error here
        return None
    return {s["seat"]: s for s in _as_list(res.get("seats"))}


def _seats(args: dict) -> dict:
    d = dao.FactorioDAO()
    sid = args.get("session_id")
    worlds: dict[str, dict[str, dict] | None] = {}
    out = []
    for seat in d.list_seats(sid):
        enriched = dict(seat)
        if seat["status"] not in ("stopped", "error"):
            enriched["idle_s"] = round(_age_s(seat["last_seen_at"]), 1)
            session = d.get_session(seat["session_id"])
            if seat["role"] == "drone":
                if seat["session_id"] not in worlds:
                    worlds[seat["session_id"]] = _world_seats(session)
                world = worlds[seat["session_id"]]
                body = (world or {}).get(seat["player_name"])
                enriched["in_world"] = None if world is None else body is not None
                for key in ("alive", "position", "surface", "health", "respawn_at"):
                    if body and body.get(key) is not None:
                        enriched[key] = body[key]
            else:
                enriched["container_running"] = appliance.seat_container_running(
                    seat["container_name"])
                connected = False
                if session and session["status"] not in ("stopped", "error"):
                    try:
                        connected = bool(_player_index(session, seat["player_name"]))
                    except Exception:  # noqa: BLE001
                        connected = False
                enriched["connected"] = connected
        out.append(enriched)
    return {"seats": out}


# ---- the reaper ----------------------------------------------------------
#
# A leaked drone is a body still standing in a world nobody is watching, in
# the way of every other seat and holding whatever it carried, and nothing
# outside this service can notice: an agent that dies mid-session takes its
# intent with it. So the service reclaims seats itself, on a sweep.
#
# It sweeps SEATS ONLY, and only seats it started -- their names are minted
# here. A session is never touched, so the reaper cannot disconnect a human who
# joined from Steam, and cannot close the world under one.

REAP_POLL_S = float(os.environ.get("AWM_FACTORIO_REAP_POLL_S", "60"))

# How long a seat may go untouched before it is reclaimed. Deliberately
# generous, and 0 disables the check entirely: an agent that thinks for many
# minutes between moves is normal, and every verb that names a seat is a
# heartbeat (they all call touch_seat), so a seat only ages out when its owner
# has genuinely stopped playing.
SEAT_IDLE_S = float(os.environ.get("AWM_FACTORIO_SEAT_IDLE_S", "3600"))

# A camera join in flight owns no container yet, so it cannot be judged by one.
# Only a join that could not still be running counts as abandoned.
STALE_JOIN_S = SEAT_JOIN_TIMEOUT * 2


def _age_s(stamp: str) -> float:
    """Seconds since an ISO stamp, or 0.0 ('just now') when it will not parse --
    an unreadable row is left alone rather than reaped on a guess."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())


def _reclaim(seat: dict, reason: str) -> None:
    """Take a seat out of its world and mark the row stopped.

    Best-effort on the world side: a row still claiming a body or container
    that is already gone is exactly the state this exists to clear, so a failed
    stop must not leave the row live or abort the sweep.
    """
    try:
        _end_seat(seat)
    except Exception:  # noqa: BLE001
        log.warning("reap: ending %s failed", seat["seat_id"], exc_info=True)
    dao.FactorioDAO().set_seat(seat["seat_id"], status="stopped")
    log.info("reap: reclaimed seat %s (%s), owner=%r",
             seat["seat_id"], reason, seat["owner"])
    _fire(seat["session_id"], "seat_left",
          {"seat_id": seat["seat_id"], "player_name": seat["player_name"],
           "reaped": True, "reason": reason})


def _reap_reason(seat: dict) -> str | None:
    """Why this seat should be reclaimed, or None to leave it alone."""
    if seat["status"] == "joining":
        # No container to judge yet -- only elapsed time can say it is stuck.
        if _age_s(seat["created_at"]) > STALE_JOIN_S:
            return "join never completed"
        return None
    if seat["role"] != "drone":
        if not appliance.seat_container_running(seat["container_name"]):
            return "container gone"
        return None
    if SEAT_IDLE_S > 0 and _age_s(seat["last_seen_at"]) > SEAT_IDLE_S:
        return f"idle > {SEAT_IDLE_S:.0f}s"
    return None


def _reap_once() -> list[dict]:
    """One sweep. Returns what it reclaimed, so a test can assert on it."""
    reclaimed = []
    for seat in dao.FactorioDAO().live_seats():
        reason = _reap_reason(seat)
        if reason:
            _reclaim(seat, reason)
            reclaimed.append({"seat_id": seat["seat_id"], "reason": reason})
    _close_idle_conns()
    return reclaimed


async def _reaper() -> None:
    """Sweep forever, beside the events pump. Sleeps first: on_start has just
    reconciled, and a sweep racing a join in the same second helps nobody."""
    while True:
        await asyncio.sleep(REAP_POLL_S)
        try:
            await asyncio.to_thread(_reap_once)
        except Exception:  # noqa: BLE001 -- a bad sweep must not end the reaper
            log.debug("reap sweep failed", exc_info=True)


# ---- act: world lifecycle ------------------------------------------------

def _world_op(sid: str, row: dict, op: str, path: str, body: dict) -> dict:
    """Run one supervisor world op, firing an 'error' event on failure (the
    exception still propagates so the caller sees the failure too)."""
    try:
        return appliance.control_post(row, path, body)
    except Exception as e:
        _fire(sid, "error", {"op": op, "error": str(e)})
        raise


def _world_new(args: dict) -> dict:
    sid = args["session_id"]
    row = _require_session(sid)
    body = {"seed": args["seed"]} if args.get("seed") is not None else {}
    result = _world_op(sid, row, "world_new", "/new", body)
    _forget_mod_state(sid)
    dao.FactorioDAO().set_runtime(sid, current_world=None)
    _fire(sid, "world_loaded", {"world": None, "seed": args.get("seed")})
    return result


def _world_save(args: dict) -> dict:
    sid = args["session_id"]
    row = _require_session(sid)
    body = {"name": args["name"], "overwrite": bool(args.get("overwrite", False))}
    result = _world_op(sid, row, "world_save", "/save", body)
    dao.FactorioDAO().set_runtime(sid, current_world=result.get("saved"))
    _fire(sid, "world_saved", {"name": result.get("saved"),
                               "replaced": result.get("replaced")})
    return result


def _world_load(args: dict) -> dict:
    sid = args["session_id"]
    row = _require_session(sid)
    result = _world_op(sid, row, "world_load", "/load", {"name": args["name"]})
    _forget_mod_state(sid)
    dao.FactorioDAO().set_runtime(sid, current_world=result.get("world"))
    _fire(sid, "world_loaded", {"world": result.get("world")})
    return result


# ---- rules -------------------------------------------------------------------

# The only lab-mod verbs a realm-rules call may run: reads, and standard mode.
SBX_REALM_VERBS = frozenset({
    "sandbox_list", "verbs", "slice_list", "slice_ports", "slice_fingerprint",
    "slice_area_of_base", "slice_timing", "bonuses", "wave_status", "ledger",
    "view_shot", "view_booth"})


def _admin_ok(token: Any) -> bool:
    """Whether ``token`` is the admin token. No token file means no override."""
    path = os.path.expanduser(os.environ.get(
        "RLM_FACTORIO_ADMIN_TOKEN_FILE", "~/.config/rlm-factorio/admin-token"))
    try:
        expected = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not expected or not isinstance(token, str):
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def _realm_rules(row: dict, args: dict) -> bool:
    """Whether this call runs under realm rules and has no admin override."""
    if row.get("rules") == "sandbox" and args.get("rules") != "realm":
        return False
    return not _admin_ok(args.get("admin_token"))


def _refuse_under_realm(row: dict, args: dict, what: str) -> None:
    if _realm_rules(row, args):
        raise appliance.ApplianceError(f"refused under realm rules: {what}")


# ---- act/perceive: addressed by seat -------------------------------------

def _resolve(args: dict, *, need_seat: bool = True) -> tuple[dict, dict | None]:
    """Resolve (session row, seat row) from ``seat_id`` and/or ``session_id``.

    A seat id alone is enough — the session hangs off it — so an agent that
    remembers one identifier can drive every verb. A session id alone resolves
    to its drone when it has exactly one; with several, ambiguity is an error
    rather than a guess, because guessing means acting as someone else's drone.
    The camera is never the acting seat.
    """
    seat_id = str(args.get("seat_id") or "").strip()
    sid = str(args.get("session_id") or "").strip()
    d = dao.FactorioDAO()
    seat: dict | None = None

    if seat_id:
        seat = d.get_seat(seat_id)
        if seat is None:
            raise ValueError(f"unknown seat_id: {seat_id!r}")
        if sid and seat["session_id"] != sid:
            raise ValueError(
                f"seat {seat_id} belongs to session {seat['session_id']}, "
                f"not {sid}"
            )
        sid = seat["session_id"]
    elif sid:
        live = [s for s in d.live_seats(sid) if s["role"] == "drone"]
        if len(live) == 1:
            seat = live[0]
        elif len(live) > 1 and need_seat:
            ids = ", ".join(r["seat_id"] for r in live)
            raise ValueError(
                f"session {sid} has {len(live)} seats — pass seat_id ({ids})"
            )
    else:
        raise ValueError("pass seat_id, or session_id")

    row = d.get_session(sid)
    if row is None:
        raise ValueError(f"unknown session_id: {sid!r}")
    if seat is not None and seat["role"] != "drone":
        if need_seat:
            raise ValueError(f"seat {seat['seat_id']} is the camera; it draws and cannot act")
        seat = None
    if need_seat and seat is None:
        raise ValueError(f"session {sid} has no seat — join it first")
    return row, seat


# Lua has one table type, so an empty list and an empty map encode identically:
# `{}`. A consumer iterating snapshot.players would get a dict on the one tick
# nobody else is around, which is exactly when it must not break.
_SNAPSHOT_LISTS = ("players", "nearby", "crafting")


def _as_list(value: Any) -> list:
    """Coerce Lua's table encoding back to a list (sparse arrays included)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        try:
            return [value[k] for k in sorted(value, key=int)]
        except (TypeError, ValueError):
            return list(value.values())
    return []


def _act(fn: str, *keys: str, need_seat: bool = True, lists: tuple = (),
         god: bool = False):
    """Handler factory: resolve the target, name the seat, call the mod.

    The seat's player name is what the mod resolves against, and it is never
    supplied by the caller — the service assigned it before the client started,
    so a verb cannot be aimed at a player the caller does not hold. ``lists``
    names result fields that must stay arrays even when empty. ``god`` marks a
    verb that realm rules refuse.
    """
    def handler(args: dict) -> dict:
        row, seat = _resolve(args, need_seat=need_seat)
        if god:
            _refuse_under_realm(row, args, fn)
        body = {k: args[k] for k in keys if args.get(k) is not None}
        if seat is not None:
            body["seat"] = seat["player_name"]
            dao.FactorioDAO().touch_seat(seat["seat_id"])
        key = _key(seat, row)
        result, cost = _iface(row, fn, body, key=key)
        for name in lists:
            if name in result:
                result[name] = _as_list(result[name])
        return _budgeted(row, key, result, cost, throttle.CMD_BUDGET_MS)
    return handler


def _observe(args: dict) -> dict:
    """Look at the world through one seat: {snapshot, screenshot}."""
    row, seat = _resolve(args)
    body: dict[str, Any] = {"seat": seat["player_name"]}
    if args.get("radius") is not None:
        body["radius"] = args["radius"]
    dao.FactorioDAO().touch_seat(seat["seat_id"])
    key = _key(seat, row)
    snapshot, cost = _iface(row, "observe", body, key=key)
    for name in _SNAPSHOT_LISTS:
        if name in snapshot:
            snapshot[name] = _as_list(snapshot[name])
    shot = None
    if args.get("screenshot"):
        shot = _screenshot({"seat_id": seat["seat_id"]})["path"]
    return _budgeted(row, key, {"snapshot": snapshot, "screenshot": shot}, cost,
                     throttle.CMD_BUDGET_MS)


def _observe_events(args: dict) -> dict:
    row, _ = _resolve(args, need_seat=False)
    sid = row["session_id"]
    _collect_and_fire(row)          # freshness: drain the world buffer now too
    with _EVENTS_LOCK:
        events = _EVENT_BUF.pop(sid, [])
    return {"events": events}


def _pause(args: dict) -> dict:
    """Pause, resume, or — with no `paused` given — flip the world.

    The flip is not a convenience: the CLI renders a boolean param as a single
    on-only flag, so `paused=false` is unsayable from a shell. Toggling is how
    a script resumes, and the reply says which state it landed in.
    """
    row, _ = _resolve(args, need_seat=False)
    _throttle(row).forget_ups()
    paused = args.get("paused")
    body = {} if paused is None else {"paused": bool(paused)}
    return appliance.control_post(row, "/pause", body)


# A Lua chunk travels to the engine as one RCON command. The cap is a guard
# against handing the socket a whole file by accident, not a measured engine
# limit.
EXEC_LUA_MAX = 64 * 1024
EXEC_OUTPUT_DEFAULT = 16 * 1024
_EXEC_LOG_LOCK = threading.Lock()


def _cap_output(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    return (output[:limit] + f"\n...[truncated: {limit} of {len(output)} characters "
            "shown; pass max_output to raise the cap, or page with factorio_scan]")


def _log_exec(row: dict, seat: dict | None, code: str) -> None:
    """Append one line about an ``exec_lua`` call to the exec log. Never raises."""
    try:
        path = Path(os.path.expanduser(os.environ.get(
            "RLM_FACTORIO_EXEC_LOG", "~/.local/state/rlm-factorio/exec-lua.jsonl")))
        raw = code.encode("utf-8")
        line = json.dumps({
            "time": datetime.now(timezone.utc).isoformat(),
            "session_id": row["session_id"],
            "seat_id": seat["seat_id"] if seat else None,
            "size": len(raw),
            "sha1": hashlib.sha1(raw).hexdigest(),
            "head": code[:200],
        }) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _EXEC_LOG_LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        log.warning("exec log write failed", exc_info=True)


def _exec_lua(args: dict) -> dict:
    """Run Lua from an inline string or a file.

    A real script does not survive shell quoting, so ``path`` is the form that
    matters for an agent alternating between MCP and a shell. The seat preamble
    shares the script's first line so a reported line number still matches the
    file the agent wrote. Under realm rules the script runs guarded, as a stored
    script in the mod, because only the mod can hand it a read-only game.
    """
    row, seat = _resolve(args, need_seat=False)
    code = args.get("code")
    path = args.get("path")
    if bool(code) == bool(path):
        raise ValueError("exec_lua needs exactly one of 'code' or 'path'")
    if path:
        src = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(src):
            raise ValueError(f"no such lua file: {src}")
        code = open(src, encoding="utf-8").read()
    if len(code) > EXEC_LUA_MAX:
        raise ValueError(
            f"lua script is {len(code)} bytes, over the {EXEC_LUA_MAX} limit"
        )
    _log_exec(row, seat, code)
    name = seat["player_name"] if seat else None
    preamble = (f'local seat = "{name}" local drone = remote.call("game_bot", "drone", '
                f'{{seat = seat}}); ' if name else "local seat, drone = nil, nil; ")
    if seat is not None:
        dao.FactorioDAO().touch_seat(seat["seat_id"])
    key = _key(seat, row)
    if _realm_rules(row, args):
        if _mod_version(row) < GUARD_MOD:
            raise appliance.ApplianceError(
                "refused under realm rules: exec_lua runs guarded, which needs "
                "game-bot-control 0.7")
        res = _run_stored(row, key, code, name, guarded=True)
    elif args.get("cache") and _mod_version(row) >= ORDERS_MOD:
        res = _run_stored(row, key, code, name)
    else:
        res = _run(row, key, preamble + code, timeout=120.0)
    output = _cap_output(res["output"], int(args.get("max_output") or EXEC_OUTPUT_DEFAULT))
    result = {"output": output, "seat": name, "cost_ms": res["cost_ms"]}
    msg = _over_budget(row, key, res["cost_ms"], throttle.command_budget(args.get("budget_ms")))
    if msg:
        result.update(ok=False, ran=True, error=msg)
    return result


# ---- mod 0.5: orders and stored scripts ------------------------------------
#
# A walk, wait or throw runs inside the engine across many ticks; the realm
# starts it and polls its order record, so the caller makes one blocking call
# instead of a polling loop. A stored script crosses the wire once and then runs
# by name, so a long script stops paying for its length on every call.

ORDERS_MOD = (0, 5, 0)
ORDER_POLL_S = 0.25
ORDER_WAIT_MAX_S = 600.0
MOD_VERSION_TTL_S = 60.0
LAB_MOD_VERSION_TTL_S = 5.0
_MOD_LOCK = threading.Lock()
_MOD_VERSIONS: dict[str, tuple[float, tuple[int, ...]]] = {}
_DEFINED: dict[str, set[str]] = {}


def _forget_mod_state(sid: str) -> None:
    with _MOD_LOCK:
        _MOD_VERSIONS.pop(sid, None)
        _DEFINED.pop(sid, None)


def _mod_version(row: dict) -> tuple[int, ...]:
    sid = row["session_id"]
    ttl = LAB_MOD_VERSION_TTL_S if row.get("kind") == "lab" else MOD_VERSION_TTL_S
    with _MOD_LOCK:
        hit = _MOD_VERSIONS.get(sid)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    out = _run(row, SYS, "rcon.print(script.active_mods['game-bot-control'] or '0')",
               timeout=10.0, metered=False)["output"].strip()
    try:
        version = tuple(int(part) for part in out.split("."))
    except ValueError:
        version = (0,)
    with _MOD_LOCK:
        _MOD_VERSIONS[sid] = (time.monotonic(), version)
    return version


def _require_orders(row: dict) -> None:
    version = _mod_version(row)
    if version < ORDERS_MOD:
        raise appliance.ApplianceError(
            "needs the game-bot-control mod >= 0.5; this world runs "
            f"{'.'.join(map(str, version))}, and 0.5 arrives with the next world "
            "restart")


def _await_order(row: dict, key: str, order_id: int, timeout: float,
                 seat: dict | None) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        res, _ = _iface(row, "order_status", {"order": order_id}, key=key)
        order = _as_list(res.get("orders"))[0]
        if order.get("status") != "running" or time.monotonic() + ORDER_POLL_S >= deadline:
            return order
        if seat is not None:
            dao.FactorioDAO().touch_seat(seat["seat_id"])
        time.sleep(ORDER_POLL_S)


def _order_verb(fn: str, *keys: str, need_seat: bool = True, default_timeout: float):
    """Handler factory: start a mod order, then block until it ends or times out."""
    def handler(args: dict) -> dict:
        row, seat = _resolve(args, need_seat=need_seat)
        _require_orders(row)
        body = {k: args[k] for k in keys if args.get(k) is not None}
        if seat is not None:
            body["seat"] = seat["player_name"]
            dao.FactorioDAO().touch_seat(seat["seat_id"])
        key = _key(seat, row)
        timeout = min(float(args.get("timeout") or default_timeout), ORDER_WAIT_MAX_S)
        if fn == "wait":
            body["timeout"] = max(1, min(216000, int(timeout * 60)))
        started, _ = _iface(row, fn, body, key=key)
        if args.get("block") is False:
            return started
        # A wait ends itself at its own deadline; give that verdict time to land.
        grace = 2.0 if fn == "wait" else 0.0
        return _await_order(row, key, int(started["order"]), timeout + grace, seat)
    return handler


def _order(args: dict) -> dict:
    row, seat = _resolve(args, need_seat=False)
    _require_orders(row)
    key = _key(seat, row)
    timeout = min(float(args.get("timeout") or 0), ORDER_WAIT_MAX_S)
    return _await_order(row, key, int(args["order"]), timeout, seat)


_walk = _order_verb("walk", "x", "y", "guard", "guard_radius", "guard_hp",
                    "guard_worms", default_timeout=120.0)
_wait = _order_verb("wait", "cond", "x", "y", "x1", "y1", "x2", "y2", "radius",
                    "surface", "name", "item", "at_least", "at_most", "status",
                    "negate", "delta", "order", "ticks", "max", "every",
                    need_seat=False, default_timeout=60.0)
_throw = _order_verb("throw", "item", "count", "x", "y", default_timeout=60.0)


def _lua_json(value: Any) -> str:
    """``value`` as a Lua expression, decoded from JSON in the engine."""
    blob = json.dumps(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"helpers.json_to_table('{blob}')"


def _run_stored(row: dict, key: str, code: str, seat_name: str | None,
                *, guarded: bool = False) -> dict:
    """Run ``code`` as a stored script, defining it first when the world lacks it.

    The seat binding is an argument, not text, so every seat shares one stored
    copy of a script.
    """
    body = "local seat = ... local drone = act and act.drone() " + code
    name = "x:" + hashlib.sha1(body.encode()).hexdigest()[:20]
    seat_lua = json.dumps(seat_name) if seat_name else "nil"
    run = (f"remote.call('game_bot','script_run',{{name='{name}',args={seat_lua},"
           f"seat={seat_lua}{',guarded=true' if guarded else ''}}})")
    sid = row["session_id"]
    with _MOD_LOCK:
        known = name in _DEFINED.get(sid, ())
    if known:
        res = _run(row, key, run, timeout=120.0)
        if f"unknown script: {name}" not in res["output"]:
            return res
    define = ("remote.call('game_bot','script_define',"
              + _lua_json({"name": name, "code": body}) + ") ")
    res = _run(row, key, define + run, timeout=120.0)
    if not res["output"].startswith("Cannot execute command"):
        with _MOD_LOCK:
            _DEFINED.setdefault(sid, set()).add(name)
    return res


# take_screenshot returns before the renderer has written anything, and the file
# grows as it is written, so "it exists" is not "it is readable".
SCREENSHOT_TIMEOUT = float(os.environ.get("AWM_FACTORIO_SCREENSHOT_TIMEOUT", "60"))


def _wait_for_file(path: str, timeout: float = SCREENSHOT_TIMEOUT) -> int:
    """Wait for a file to appear and stop growing; return its size."""
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        if size > 0 and size == last:
            return size
        last = size
        time.sleep(0.25)
    raise appliance.ApplianceError(
        f"screenshot never settled at {path} within {timeout:.0f}s"
        + ("" if last > 0 else " (the file was never written)")
    )


def _screenshot(args: dict) -> dict:
    """Render the world around a drone, or at x,y, and return a PATH to the PNG.

    A path, not a blob: the caller reads the file and actually sees it, and a
    shell script can pass it onward. The world's camera draws it offscreen at
    whatever resolution is asked for, so its own window stays tiny and unmapped.
    """
    row, seat = _resolve(args, need_seat=False)
    if seat is None and (args.get("x") is None or args.get("y") is None):
        raise ValueError("screenshot needs a drone seat, or x and y")
    camera = _camera_for(row)
    body: dict[str, Any] = {"camera": camera["player_name"]}
    if seat is not None:
        body["seat"] = seat["player_name"]
        dao.FactorioDAO().touch_seat(seat["seat_id"])
    for key in ("x", "y", "width", "height", "zoom", "surface", "daytime",
                "show_entity_info", "file"):
        if args.get(key) is not None:
            body[key] = args[key]
    dao.FactorioDAO().touch_seat(camera["seat_id"])
    result, _ = _iface(row, "screenshot", body, key=_key(seat, row))
    path = str(appliance.seat_output_dir(camera["seat_id"]) / str(result["file"]))
    size = _wait_for_file(path)
    # How much world the frame covers, so pixels can be reasoned back to tiles:
    # a tile is 32 px at zoom 1.
    res, zoom = result["resolution"], float(result["zoom"])
    tiles = {"width": res["x"] / (32.0 * zoom), "height": res["y"] / (32.0 * zoom)}
    return {**result, "path": path, "bytes": size,
            "seat_id": seat["seat_id"] if seat else None, "tiles": tiles}


# One RCON command carries the whole chunk, and the channel fragments a large
# payload, so a blueprint string is uploaded in pieces the mod concatenates.
BLUEPRINT_CHUNK = 2000


def _blueprint_text(args: dict) -> str:
    """The blueprint string, inline or from a file. Files matter here: a
    blueprint string is long and base64-shaped, exactly what shell quoting
    mangles."""
    text = args.get("blueprint")
    path = args.get("path")
    if bool(text) == bool(path):
        raise ValueError("blueprint_stamp needs exactly one of 'blueprint' or 'path'")
    if path:
        src = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(src):
            raise ValueError(f"no such blueprint file: {src}")
        text = open(src, encoding="utf-8").read()
    return text.strip()


def _upload_id(row: dict) -> str:
    """A seatless upload id: the mod caps ids at 64 characters."""
    return "s" + hashlib.sha1(row["session_id"].encode()).hexdigest()[:16]


def _blueprint_stamp(args: dict) -> dict:
    """Stamp a blueprint at a position, as ghosts (a player's move) or built."""
    row, seat = _resolve(args, need_seat=False)
    if seat is None and row["rules"] != "sandbox":
        raise ValueError(f"session {row['session_id']} needs a seat to stamp — join it first")
    built = bool(args.get("build")) or args.get("build_mode") == "built"
    if built:
        _refuse_under_realm(row, args, "blueprint_stamp build_mode=built")
    text = _blueprint_text(args)
    upload_id = seat["seat_id"] if seat else _upload_id(row)
    chunks = [text[i:i + BLUEPRINT_CHUNK]
              for i in range(0, len(text), BLUEPRINT_CHUNK)]
    key = _key(seat, row)
    for n, chunk in enumerate(chunks):
        _iface(row, "bp_put", {"id": upload_id, "data": chunk, "reset": n == 0},
               key=key)
    body = {"id": upload_id, "x": args["x"], "y": args["y"]}
    if seat is not None:
        body["seat"] = seat["player_name"]
    for name in ("direction", "surface", "build", "force_build", "clear", "force"):
        if args.get(name) is not None:
            body[name] = args[name]
    if seat is None:
        body.setdefault("surface", "nauvis")
    if built:
        body["build"] = True
    if seat is not None:
        dao.FactorioDAO().touch_seat(seat["seat_id"])
    result, cost = _iface(row, "blueprint_stamp", body, key=key, timeout=120.0)
    return _budgeted(row, key, {**result, "chunks": len(chunks), "characters": len(text),
                           "cost_ms": cost},
                     cost, throttle.command_budget(args.get("budget_ms")))


def _blueprint_capture(args: dict) -> dict:
    """Capture a region as a blueprint string, returned as a file path.

    The string comes out through the engine's script-output rather than back
    over RCON, which fragments a large payload — and a path is what the other
    agent's stamp wants anyway.
    """
    row, seat = _resolve(args)
    body = {"seat": seat["player_name"]}
    for key in ("x", "y", "radius", "x1", "y1", "x2", "y2", "surface",
                "include_tiles", "label"):
        if args.get(key) is not None:
            body[key] = args[key]
    dao.FactorioDAO().touch_seat(seat["seat_id"])
    key = _key(seat, row)
    result, cost = _iface(row, "blueprint_capture", body, key=key, timeout=120.0)
    over = _over_budget(row, key, cost, throttle.command_budget(args.get("budget_ms")))
    src = appliance.session_output_dir(row["session_id"]) / str(result["file"])
    _wait_for_file(str(src))
    text = open(src, encoding="utf-8").read().strip()
    out = {**result, "path": str(src), "characters": len(text)}
    if args.get("save_as"):
        dest = os.path.abspath(os.path.expanduser(str(args["save_as"])))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        out["path"] = dest
    # Small enough to hand straight to another agent; a big one stays a path.
    if len(text) <= BLUEPRINT_CHUNK:
        out["blueprint"] = text
    if over:
        return {"ok": False, "ran": True, "error": over, "cost_ms": cost, "result": out}
    return out


def _scan(args: dict) -> dict:
    """Survey entities in pages; see :mod:`awm.rlm_factorio.scan`."""
    row, seat = _resolve(args, need_seat=False)
    key = _key(seat, row)
    if seat is not None:
        dao.FactorioDAO().touch_seat(seat["seat_id"])

    def run(code: str) -> tuple[str, float | None]:
        res = _run(row, key, code, timeout=60.0)
        return res["output"], res["cost_ms"]

    def seat_position() -> dict:
        name = seat["player_name"]
        out = _run(row, key, f'local d = remote.call("game_bot", "drone", {{seat = "{name}"}}) '
                             f'if not d then error("seat {name} has no drone") end '
                             f'rcon.print(helpers.table_to_json(d.position))')["output"]
        try:
            return json.loads(out)
        except ValueError:
            raise ValueError(out.strip() or f"seat {name}: no position") from None

    try:
        return SCANNER.scan(key, args, run, seat_position if seat else None)
    except scan.ScanError as exc:
        raise ValueError(str(exc)) from None


def _load(args: dict) -> dict:
    """Each live world's throttle meters, with each key tied back to its seat."""
    d = dao.FactorioDAO()
    worlds = {}
    for row in d.live_sessions():
        snap = _throttle(row).snapshot()
        by_player = {s["player_name"]: s["seat_id"]
                     for s in d.live_seats(row["session_id"])}
        for name, meter in snap["keys"].items():
            meter["seat_id"] = by_player.get(name)
            meter["scan_work_cap"] = SCANNER.caps.get(name)
        worlds[row["session_id"]] = {"name": row["name"], "kind": row["kind"], **snap}
    return {"worlds": worlds}


# ---- mod 0.6: the tool library and the lab mod -------------------------------
#
# A tool is a Lua file under RLM_FACTORIO_TOOLS (CONTRACT.md § Tools). tool_sync
# defines the files in the world's registry; tool runs one by name.

TOOLS_MOD = (0, 6, 0)
GUARD_MOD = (0, 7, 0)  # tool_call and script_run take `guarded` from here on
TOOL_KEYS = ("tool", "kind", "effect", "summary", "args", "returns")
TOOL_KINDS = ("call", "lib")
TOOL_EFFECTS = ("read", "act", "cheat")
_TOOL_HEADER = re.compile(r"^---\s*([a-z]+):\s*(.*?)\s*$")


def _tools_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get(
        "RLM_FACTORIO_TOOLS",
        "~/agentic_workspace/projects/game-bot/factorio/research/tools")))


def _require_tools(row: dict) -> None:
    version = _mod_version(row)
    if version < TOOLS_MOD:
        raise appliance.ApplianceError(
            "needs game-bot-control 0.6; this world runs "
            f"{'.'.join(map(str, version))}")


def _tool_header(code: str) -> dict:
    """The ``--- key: value`` lines at the top of a tool file."""
    out = {}
    for line in code.splitlines():
        m = _TOOL_HEADER.match(line)
        if not m:
            break
        out[m[1]] = m[2]
    return out


def _tool_problems(stem: str, header: dict) -> list[str]:
    errs = [f"missing header key {k}" for k in TOOL_KEYS if k not in header]
    if "tool" in header and header["tool"] != stem:
        errs.append(f"tool {header['tool']!r} does not match the file name {stem!r}")
    if "kind" in header and header["kind"] not in TOOL_KINDS:
        errs.append(f"kind must be one of {', '.join(TOOL_KINDS)}")
    if "effect" in header and header["effect"] not in TOOL_EFFECTS:
        errs.append(f"effect must be one of {', '.join(TOOL_EFFECTS)}")
    return errs


def _tool_listing(row: dict, key: str) -> dict:
    result, _ = _iface(row, "tool_list", {}, key=key)
    result["tools"] = _as_list(result.get("tools"))
    return result


def _tool_list(args: dict) -> dict:
    """The world's tool registry: name, kind, effect, summary, calls, errors."""
    row, seat = _resolve(args, need_seat=False)
    _require_tools(row)
    return _tool_listing(row, _key(seat, row))


def _tool(args: dict) -> dict:
    """Run a library tool: {ok, r} or the mod's error.

    Under realm rules the mod runs the tool guarded and refuses a cheat tool,
    and a cheat lib it uses, for the whole call.
    """
    row, seat = _resolve(args, need_seat=False)
    _require_tools(row)
    name = args.get("name")
    if not name:
        raise ValueError("tool needs a name")
    key = _key(seat, row)
    body = {"name": name, "args": args.get("args") or {}}
    if _realm_rules(row, args):
        if _mod_version(row) < GUARD_MOD:
            raise appliance.ApplianceError(
                "refused under realm rules: this world's mod cannot guard tools "
                "before game-bot-control 0.7")
        body.update(deny=["cheat"], guarded=True)
    if seat is not None:
        body["seat"] = seat["player_name"]
        dao.FactorioDAO().touch_seat(seat["seat_id"])
    result, cost = _iface(row, "tool_call", body, key=key, timeout=120.0)
    if result.get("error") == "denied":
        what = str(result.get("detail") or f"cheat tool {name}").removesuffix(" refused")
        raise appliance.ApplianceError(f"refused under realm rules: {what}")
    return _budgeted(row, key, result, cost, throttle.command_budget(args.get("budget_ms")))


def _tool_uses(header: dict) -> list[str]:
    return [u.strip() for u in header.get("uses", "").split(",") if u.strip()]


def _reaches_cheat(name: str, found: dict[str, tuple[dict, str]]) -> str | None:
    """The cheat lib that ``name``'s ``uses:`` chain reaches among ``found``."""
    seen: set[str] = set()
    todo = _tool_uses(found[name][0])
    while todo:
        lib = todo.pop()
        if lib in seen or lib not in found:
            continue
        seen.add(lib)
        if found[lib][0]["effect"] == "cheat":
            return lib
        todo.extend(_tool_uses(found[lib][0]))
    return None


def _lib_order(found: dict[str, tuple[dict, str]]) -> tuple[list[str], list[str]]:
    """Libs ordered so each follows the libs it uses, then the call tools.

    Returns (order, cyclic): a lib on a ``uses:`` cycle is left out of the order.
    """
    libs = sorted(n for n, (h, _) in found.items() if h["kind"] == "lib")
    order: list[str] = []
    state: dict[str, int] = {}
    cyclic: set[str] = set()

    def visit(name: str, path: list[str]) -> bool:
        if state.get(name) == 2:
            return name not in cyclic
        if state.get(name) == 1:
            cyclic.update(path[path.index(name):])
            return False
        state[name] = 1
        ok = True
        for dep in _tool_uses(found[name][0]):
            if dep in found and found[dep][0]["kind"] == "lib":
                ok = visit(dep, path + [name]) and ok
        state[name] = 2
        if ok and name not in cyclic:
            order.append(name)
        else:
            cyclic.add(name)
        return ok

    for name in libs:
        visit(name, [])
    calls = sorted(n for n, (h, _) in found.items() if h["kind"] != "lib")
    return order + calls, sorted(cyclic)


def _tool_sync(args: dict) -> dict:
    """Define the tool files in the world, libs first. {defined, errors}.

    Libs go in dependency order. A tool whose ``uses:`` chain reaches a cheat
    lib while the tool is not itself cheat is not defined.
    """
    row, seat = _resolve(args, need_seat=False)
    _require_tools(row)
    wanted = args.get("names")
    found: dict[str, tuple[dict, str]] = {}
    errors: list[dict] = []
    root = _tools_dir()
    for path in sorted(root.rglob("*.lua")) if root.is_dir() else []:
        stem = path.stem
        if wanted is not None and stem not in wanted:
            continue
        try:
            code = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append({"name": stem, "error": f"cannot read {path}: {exc}"})
            continue
        header = _tool_header(code)
        problems = _tool_problems(stem, header)
        if stem in found:
            problems.append("duplicate tool name")
        if len(code) > EXEC_LUA_MAX:
            problems.append(f"{len(code)} characters, over {EXEC_LUA_MAX}")
        if problems:
            errors.append({"name": stem, "error": "; ".join(problems)})
        else:
            found[stem] = (header, code)
    for name in wanted or ():
        if name not in found and not any(e["name"] == name for e in errors):
            errors.append({"name": name, "error": f"no tool file named {name} under {root}"})
    for name in sorted(found):
        cheat = _reaches_cheat(name, found)
        if cheat and found[name][0]["effect"] != "cheat":
            errors.append({"name": name, "error": (
                f"effect {found[name][0]['effect']} but its uses chain reaches "
                f"cheat lib {cheat}")})
    rejected = {e["name"] for e in errors}
    ready = {n: v for n, v in found.items() if n not in rejected}
    order, cyclic = _lib_order(ready)
    for name in cyclic:
        errors.append({"name": name, "error": "lib cycle through uses"})
    key = _key(seat, row)
    defined = []
    for name in order:
        header, code = ready[name]
        try:
            _iface(row, "tool_define",
                   {"name": name, "code": code, "kind": header["kind"],
                    "effect": header["effect"], "summary": header["summary"]},
                   key=key, timeout=60.0)
            defined.append(name)
        except appliance.ApplianceError as exc:
            errors.append({"name": name, "error": str(exc)})
    return {"defined": defined, "errors": errors}


def _sbx(args: dict) -> dict:
    """Call a verb of the fxlab-sbx lab mod, which a lab loads."""
    row, seat = _resolve(args, need_seat=False)
    verb = args.get("verb")
    if not verb:
        raise ValueError("sbx needs a verb")
    body = args.get("args") or {}
    allowed = verb in SBX_REALM_VERBS or (
        verb == "sbx_mode" and body.get("mode") == "standard")
    if not allowed:
        _refuse_under_realm(row, args, f"sbx {verb}")
    result, _ = _iface(row, verb, body, key=_key(seat, row), timeout=120.0, mod="sbx")
    return result


# ---- blocking verbs: advance and wait_until ----------------------------------

ADVANCE_POLL_S = 0.25


def _advance(args: dict) -> dict:
    """Run the world ``ticks`` forward at up to ``speed``, then pause it.

    It slows down over the last few seconds of game time to land close to the
    target tick, and pauses again even when it fails partway.
    """
    row, seat = _resolve(args, need_seat=False)
    _refuse_under_realm(row, args, "advance")
    key = _key(seat, row)
    speed = float(args.get("speed") or 1000.0)
    timeout = min(float(args.get("timeout_s") or WAIT_MAX_S), WAIT_MAX_S)

    def lua(code: str) -> str:
        return _run(row, key, code, timeout=30.0)["output"].strip()

    start = int(lua("rcon.print(game.tick)"))
    target = start + int(args["ticks"])
    t0 = time.monotonic()
    now, timed_out = start, False
    lua(f"game.speed = {speed} game.tick_paused = false")
    try:
        while True:
            now = int(lua("rcon.print(game.tick)"))
            left = target - now
            if left <= 0:
                break
            if time.monotonic() - t0 > timeout:
                timed_out = True
                break
            if left < 600:
                lua(f"game.speed = {max(1.0, min(speed, left / 30))}")
            time.sleep(ADVANCE_POLL_S)
    finally:
        now = int(lua("game.tick_paused = true game.speed = 1 rcon.print(game.tick)"))
        _throttle(row).forget_ups()
    out = {"tick": now, "ticks": now - start, "wall_s": round(time.monotonic() - t0, 2)}
    if timed_out:
        out.update(error="TIMEOUT", timeout_s=timeout)
    return out


WAIT_POLL_MIN_S = 0.5

_WORKING_LUA = """local c = %s
local ents, missing = {}, {}
if c.unit_numbers then
  for _, u in pairs(c.unit_numbers) do
    local e = game.get_entity_by_unit_number(u)
    if e and e.valid then ents[#ents + 1] = e else missing[#missing + 1] = u end
  end
else
  local surface = game.surfaces[c.surface or "nauvis"]
  for _, e in pairs(surface.find_entities_filtered{area = c.area, force = "player"}) do
    if e.status then ents[#ents + 1] = e end
  end
end
local names = {}
for k, v in pairs(defines.entity_status) do names[v] = k end
local working, idle = 0, {}
for _, e in pairs(ents) do
  if e.status == defines.entity_status.working then working = working + 1
  elseif #idle < 10 then
    idle[#idle + 1] = {unit_number = e.unit_number, name = e.name,
                       status = names[e.status] or tostring(e.status)}
  end
end
rcon.print(helpers.table_to_json({total = #ents + #missing, working = working,
                                  idle = idle, missing = missing}))"""

_GHOSTS_LUA = """local c = %s
if c.ids then
  local left = {}
  for _, u in pairs(c.ids) do
    local e = game.get_entity_by_unit_number(u)
    if e and e.valid and e.type == "entity-ghost" then left[#left + 1] = u end
  end
  rcon.print(helpers.table_to_json({remaining = #left, ids = left}))
else
  local n = game.surfaces[c.surface or "nauvis"].count_entities_filtered{
    area = c.area, type = "entity-ghost"}
  rcon.print(helpers.table_to_json({remaining = n}))
end"""

_OPS = {
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "truthy": lambda a, b: bool(a), "falsy": lambda a, b: not a,
}


def _at_path(value: Any, path: str) -> Any:
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(value, list):
            value = value[int(part)] if part.isdigit() and int(part) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _wait_probe(row: dict, seat: dict | None, args: dict):
    """The condition as a callable returning (holds, state)."""
    cond = args["condition"]
    key = _key(seat, row)
    if "entities_working" in cond or "ghosts_built" in cond:
        working = "entities_working" in cond
        spec = cond["entities_working" if working else "ghosts_built"]
        code = (_WORKING_LUA if working else _GHOSTS_LUA) % _lua_json(spec)

        def probe() -> tuple[bool, dict]:
            state = json.loads(_run(row, key, code, timeout=30.0)["output"])
            for name in ("idle", "missing", "ids"):
                if name in state:
                    state[name] = _as_list(state[name])
            if working:
                return state["total"] > 0 and state["working"] == state["total"], state
            return state["remaining"] == 0, state
        return probe
    if not cond.get("tool"):
        raise ValueError("condition needs entities_working, ghosts_built or tool")
    _require_tools(row)
    listed = {t.get("name"): t for t in _tool_listing(row, key)["tools"]}
    tool = listed.get(cond["tool"])
    if tool is None:
        raise ValueError(f"no tool named {cond['tool']!r} in this world; run tool_sync")
    if tool.get("effect") != "read":
        raise ValueError(f"wait_until polls read tools only; {cond['tool']} is {tool.get('effect')}")
    op = _OPS.get(cond.get("op") or "truthy")
    if op is None:
        raise ValueError(f"op must be one of {', '.join(_OPS)}")
    call = {k: args[k] for k in ("session_id", "seat_id", "rules", "admin_token")
            if args.get(k) is not None}
    call.update(name=cond["tool"], args=cond.get("args") or {})

    def probe() -> tuple[bool, Any]:
        state = _tool(call)
        try:
            return bool(op(_at_path(state, cond.get("path")), cond.get("value"))), state
        except TypeError:
            return False, state
    return probe


def _wait_until(args: dict) -> dict:
    """Poll a condition until it holds for ``for_s``, or until the timeout."""
    if args.get("timeout_s") is None:
        raise ValueError(f"wait_until needs timeout_s (at most {WAIT_MAX_S:.0f})")
    row, seat = _resolve(args, need_seat=False)
    timeout = min(float(args["timeout_s"]), WAIT_MAX_S)
    poll = max(WAIT_POLL_MIN_S, float(args.get("poll_s") or 2.0))
    hold = float(args["condition"].get("for_s") or 0.0)
    probe = _wait_probe(row, seat, args)
    d = dao.FactorioDAO()
    t0 = time.monotonic()
    since = None
    while True:
        current = d.get_session(row["session_id"])
        if current is None or current["status"] in ("stopped", "error"):
            raise appliance.ApplianceError(
                f"world {row['session_id']} was released mid-wait")
        holds, state = probe()
        now = time.monotonic()
        since = (since or now) if holds else None
        waited = round(now - t0, 2)
        if since is not None and now - since >= hold:
            return {"met": True, "waited_s": waited, "state": state, "timeout_s": timeout}
        if now - t0 + poll > timeout:
            return {"error": "TIMEOUT", "waited_s": waited, "state": state,
                    "timeout_s": timeout}
        if seat is not None:
            d.touch_seat(seat["seat_id"])
        time.sleep(poll)


_move = _act("walk", "x", "y")
_stop = _act("stop")
_teleport = _act("teleport", "x", "y", "surface", god=True)
_respawn = _act("spawn")
_mine = _act("mine", "x", "y", "name", "count")
_craft = _act("craft", "recipe", "count")
_build = _act("build", "name", "x", "y", "direction")
_insert = _act("insert", "x", "y", "name", "count", "target")
_take = _act("take", "x", "y", "name", "count", "target")
# Force-wide, so they need no seat -- but they take one when it is what the
# caller has in hand.
_research = _act("research", "name", need_seat=False, god=True)
_recipes = _act("recipes", "search", "limit", need_seat=False,
                lists=("recipes",))
_technologies = _act("technologies", "search", "only_unresearched", "limit",
                     need_seat=False, lists=("technologies",))


def _checked(verb: str, handler):
    """Validate a verb's arguments before its handler sees them."""
    wants_as = len(inspect.signature(handler).parameters) >= 2

    def run(args: dict, as_: str | None = None):
        args = validate.check(verb, args or {})
        token = _CALLER.set(as_)
        try:
            return handler(args, as_) if wants_as else handler(args)
        finally:
            _CALLER.reset(token)
    return run


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "join": _join,
    "leave": _leave,
    "rejoin": _rejoin,
    "seats": _seats,
    "observe": _observe,
    "screenshot": _screenshot,
    "blueprint_stamp": _blueprint_stamp,
    "blueprint_capture": _blueprint_capture,
    "observe_events": _observe_events,
    "recipes": _recipes,
    "technologies": _technologies,
    "world_new": _world_new,
    "world_save": _world_save,
    "world_load": _world_load,
    "pause": _pause,
    "advance": _advance,
    "wait_until": _wait_until,
    "exec_lua": _exec_lua,
    "move": _move,
    "stop": _stop,
    "teleport": _teleport,
    "respawn": _respawn,
    "mine": _mine,
    "craft": _craft,
    "build": _build,
    "insert": _insert,
    "take": _take,
    "research": _research,
    "scan": _scan,
    "load": _load,
    "walk": _walk,
    "wait": _wait,
    "throw": _throw,
    "order": _order,
    "tool": _tool,
    "tool_list": _tool_list,
    "tool_sync": _tool_sync,
    "sbx": _sbx,
}
HANDLERS = {verb: _checked(verb, fn) for verb, fn in HANDLERS.items()}


def _on_start() -> None:
    """Stand up the DB, then reconcile stale rows whose container is gone.

    A service respawn (the gateway can restart us) must not leave rows claiming
    'ready' for a container that no longer exists — mark those 'stopped' so the
    next acquire brings a fresh appliance up rather than adopting a ghost.
    """
    dao.init()
    d = dao.FactorioDAO()
    for row in d.live_sessions():
        if not appliance.is_container_running(row):
            log.info("on_start: reconciling stale session %s -> stopped",
                     row["session_id"])
            _stop_row(d, row)
    # A camera whose container is gone is stopped, never adopted: the client is
    # stateless, so a lost one is replaced by starting again. A drone lives in
    # its world, so it stands or falls with its session.
    for seat in d.live_seats():
        if seat["role"] != "drone" and not appliance.seat_container_running(
                seat["container_name"]):
            log.info("on_start: reconciling stale seat %s -> stopped",
                     seat["seat_id"])
            d.set_seat(seat["seat_id"], status="stopped")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    global _ADAPTER, _LOOP
    adapter = ServiceAdapter(
        "rlm-factorio", API_MANIFEST, HANDLERS, on_start=_on_start,
    )
    _ADAPTER = adapter
    loop = _LOOP = asyncio.get_running_loop()
    # Every handler runs in this pool, and a seat held by the throttle holds a
    # thread while it waits, so the pool must outnumber the seats comfortably.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=HANDLER_THREADS))

    # A shutdown leaves seats in their worlds. A restart adopts them in
    # on_start, so a deploy costs the crew nothing; the reaper and
    # leave/release remain the only ways a seat ends.
    signalled = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signalled.set)
        except (NotImplementedError, ValueError, RuntimeError):
            log.debug("no signal handler for %s", sig)

    pump = asyncio.create_task(_events_pump(adapter))
    reaper = asyncio.create_task(_reaper())
    sampler = asyncio.create_task(_ups_sampler())
    serve = asyncio.create_task(adapter.run())
    stop = asyncio.create_task(signalled.wait())
    ended = None
    try:
        await asyncio.wait({serve, stop}, return_when=asyncio.FIRST_COMPLETED)
        ended = serve if serve.done() else None
        if signalled.is_set():
            log.info("shutdown: leaving %d seat(s) running for the next start",
                     len(dao.FactorioDAO().live_seats()))
    finally:
        for task in (pump, reaper, sampler, stop, serve):
            task.cancel()
        broker.stop_all()
    if ended is not None:
        await ended  # re-raise whatever ended the adapter


if __name__ == "__main__":
    asyncio.run(main())
