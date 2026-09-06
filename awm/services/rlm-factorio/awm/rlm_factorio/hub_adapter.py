"""Hub adapter for the rlm-factorio realm service.

Boots rlm-factorio as a gateway-registered process: stands up its own DB, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect). The realm-family functions are exposed over the control WS
and projected into the gateway catalog as ``rlm_factorio_<verb>`` tools.

This service owns the Factorio appliance (Docker container + stdlib supervisor —
see ``appliance/``). The lifecycle + world verbs are LIVE: ``acquire`` brings the
container up and waits for the engine, ``world_new/save/load`` drive the
supervisor's control surface (sacred-saves invariant preserved — only
``world_save`` ever writes a named ``.zip``), ``release`` tears the container
down, seats first (the saves volume survives).

``join`` allocates a SEAT: a container running the same image in its client
role, which joins the world over the compose network and mints a real
``LuaPlayer``. Seats are what make the realm a LAN party — several agents in one
world, one shared force — and what buy back the capabilities a scripted body
never had. ``leave`` returns one; ``seats`` lists them.

A seat is leased, not given. It records the caller the gateway threads in as its
owner, every verb naming it refreshes a last-seen stamp, and a background reaper
reclaims seats whose container has died or whose owner has gone quiet for an
hour. An agent that dies mid-session must not cost us two cores forever, and
nothing but this service can notice that it did.

Everything an agent does in the world it does AS a seat. The act verbs —
``move`` / ``stop`` / ``mine`` / ``craft`` / ``build`` / ``insert`` / ``take``,
plus the flagged cheats ``teleport`` and ``research`` — and the perceive verbs
``observe`` / ``recipes`` / ``technologies`` all take a ``seat_id``, or a
``session_id`` alone when that session holds exactly one seat. The service, not
the caller, supplies the seat's player name to the world, so a verb cannot be
aimed at a player the caller does not hold.

They all travel the same path: the supervisor runs an in-container RCON client
against the engine, and one control route (``/iface``) names a function of the
baked-in ``game-bot-control`` mod. Adding a capability is a mod function and a
handler, with nothing to plumb in between. The gameplay verbs are reach-gated —
a seat walks to a thing before touching it — and act through the engine's own
mechanics, so mining takes time and crafting counts toward research triggers.

``exec_lua`` is the escape hatch. It takes inline ``code`` or a ``path`` to a
file on this machine, because a real script does not survive shell quoting. It
runs in the SCENARIO context, where the mod's ``storage`` is invisible (Factorio
disables ``load`` in the mod control stage, so there is nowhere else to put it);
mod state is reachable from there through ``remote.call('game_bot', ...)``. The
resolved seat is bound as ``seat`` / ``player`` on the script's first line, so a
file script needs no templating and its line numbers still match.

The ``factorio`` emitter is LIVE: world verbs fire ``world_loaded`` /
``world_saved`` / ``error`` directly, and a background pump drains the mod's
bounded events ring buffer (``spawned`` / ``arrived`` / ``died`` / ...) on a
short interval and re-emits each entry as ``{session_id, kind, tick, data}`` on
the topic — projected up-stack as ``rlm.factorio.<kind>``. Every fired event is
also accumulated in a per-session service-side inbox so a *polling* consumer
never races the pump: ``observe_events`` returns everything since its last call
(consume-once), while the emitter stays the live stream.

Single-session for now: ``acquire`` is idempotent (at most one live appliance;
a second acquire returns the existing session, erroring on a game mismatch). The
session row carries the container/ports so a service respawn re-adopts rather
than duplicating. See :mod:`awm.rlm_factorio.appliance` for the pool seam.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.rlm_factorio.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.rlm_factorio import appliance, dao

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

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- lifecycle ----
        {
            "name": "acquire",
            "tool": "rlm_factorio_acquire",
            "description": (
                "Acquire a Factorio realm session: bring the appliance container "
                "up (building the image on first run) and wait for the engine to "
                "be ready. Returns {session_id}. Idempotent — a second acquire "
                "returns the existing live session (errors on a game mismatch)."
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
                {"name": "opts", "type": "object", "required": False},
            ],
            # First acquire builds the image (large download) before waiting for
            # the engine — well beyond the proxy's 30s default.
            "timeout": 1800.0,
        },
        {
            "name": "release",
            "tool": "rlm_factorio_release",
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
            "tool": "rlm_factorio_reset",
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
            "tool": "rlm_factorio_status",
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
            "tool": "rlm_factorio_join",
            "description": (
                "Join the session's world as a real multiplayer player: start a "
                "seat container, wait for it to connect, and bind it to the "
                "in-game player it minted. Returns {seat_id, player_name, "
                "player_index, owner}. A seat is a full Factorio client, so "
                "its capabilities are a player's -- mining that sustains, "
                "crafting that counts toward research triggers, and a "
                "renderable view. Every act verb below is driven through a "
                "seat. A seat is a LEASE: it is owned by the caller (or by an "
                "explicit 'owner'), any verb naming it holds it, and one left "
                "untouched for an hour is reclaimed. Call 'leave' when done."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "owner", "type": "string", "required": False},
            ],
            # Container start + map transfer + world load; ~25s observed, but a
            # large world transfers for longer.
            "timeout": 300.0,
        },
        {
            "name": "leave",
            "tool": "rlm_factorio_leave",
            "description": (
                "Release a seat: disconnect its player and remove the seat "
                "container. The world and every other seat are untouched."
            ),
            "params": [
                {"name": "seat_id", "type": "string", "required": True},
            ],
            "timeout": 120.0,
        },
        {
            "name": "seats",
            "tool": "rlm_factorio_seats",
            "description": (
                "List seats, optionally for one session: their player name and "
                "index, owner, container state, and whether the player is "
                "currently connected in-world. Also how long since each was "
                "last used -- the clock the reaper reclaims a seat by."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
            "timeout": 60.0,
        },
        # ---- perceive ----
        {
            "name": "observe",
            "tool": "rlm_factorio_observe",
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
                {"name": "radius", "type": "integer", "required": False},
                {"name": "screenshot", "type": "boolean", "required": False},
            ],
        },
        {
            "name": "screenshot",
            "tool": "rlm_factorio_screenshot",
            "description": (
                "Render what the world looks like around a seat and return "
                "{path} -- a PNG on this machine, which you can read as an "
                "image. Centred on the seat unless x,y say otherwise. width/"
                "height (default 1280x720) are rendered offscreen, so image "
                "quality is not limited by the seat's window; zoom < 1 pulls "
                "back for a factory overview, > 1 pushes in. Lit as noon "
                "unless daytime says otherwise (0 = noon, 0.5 = midnight), so "
                "a night shot is still readable. show_entity_info draws the "
                "alt-mode overlay (recipe icons, contents). Returns the "
                "frame geometry too -- position is the centre, and tiles says "
                "how many tiles wide and tall the image covers -- so a pixel "
                "can be reasoned back to a coordinate. NOTE: the engine does "
                "not draw the rendering seat's OWN character, so you will see "
                "bare ground where you are standing; every other player IS "
                "drawn. You are at the centre of the frame."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
                {"name": "width", "type": "integer", "required": False},
                {"name": "height", "type": "integer", "required": False},
                {"name": "zoom", "type": "number", "required": False},
                {"name": "daytime", "type": "number", "required": False},
                {"name": "show_entity_info", "type": "boolean", "required": False},
                {"name": "surface", "type": "string", "required": False},
                {"name": "file", "type": "string", "required": False},
            ],
            "timeout": 120.0,
        },
        {
            "name": "observe_events",
            "tool": "rlm_factorio_observe_events",
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
            "tool": "rlm_factorio_recipes",
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
            "tool": "rlm_factorio_technologies",
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
            "tool": "rlm_factorio_world_new",
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
            "tool": "rlm_factorio_world_save",
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
            "tool": "rlm_factorio_world_load",
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
            "tool": "rlm_factorio_pause",
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
            "name": "exec_lua",
            "tool": "rlm_factorio_exec_lua",
            "description": (
                "Run Lua in the running world. Pass code inline OR path = a "
                "file on this machine (exactly one) -- a real script does not "
                "survive shell quoting, so scripts belong in files. Returns "
                "{output} = whatever the script printed; to get a value back "
                "call rcon.print(...). Runs in the SCENARIO context, so the "
                "mod's storage is invisible -- reach mod state through "
                "remote.call('game_bot', <fn>, {seat=seat}). Your script is "
                "preceded (on the same line, so line numbers still match your "
                "file) by `local seat, player = ...` bound to the resolved "
                "seat, so a file script needs no templating per seat."
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "code", "type": "string", "required": False},
                {"name": "path", "type": "string", "required": False},
            ],
            "timeout": 120.0,
        },
        {
            "name": "blueprint_stamp",
            "tool": "rlm_factorio_blueprint_stamp",
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
                {"name": "force_build", "type": "boolean", "required": False},
                {"name": "surface", "type": "string", "required": False},
            ],
            "timeout": 180.0,
        },
        {
            "name": "blueprint_capture",
            "tool": "rlm_factorio_blueprint_capture",
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
                {"name": "radius", "type": "number", "required": False},
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
            ],
            "timeout": 180.0,
        },
        # ---- act: as a player (a seat drives all of these) ----
        {
            "name": "move",
            "tool": "rlm_factorio_move",
            "description": (
                "Walk the seat to (x,y) via engine pathfinding (routes around "
                "obstacles) and return immediately. Poll observe or watch the "
                "emitter for 'arrived' / 'path_blocked'. " + _TARGET_DOC
            ),
            "params": [
                *_SEAT_TARGET,
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
            ],
        },
        {
            "name": "stop",
            "tool": "rlm_factorio_stop",
            "description": (
                "Halt the seat where it stands: clears its walk target and any "
                "mining order."
            ),
            "params": list(_SEAT_TARGET),
        },
        {
            "name": "teleport",
            "tool": "rlm_factorio_teleport",
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
            ],
        },
        {
            "name": "respawn",
            "tool": "rlm_factorio_respawn",
            "description": (
                "Give a seat a body again, now: skips the death countdown and "
                "ends the freeplay intro cutscene. join already does this, so "
                "this is the after-you-died verb, not the setup one."
            ),
            "params": list(_SEAT_TARGET),
            "timeout": 60.0,
        },
        {
            "name": "mine",
            "tool": "rlm_factorio_mine",
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
            "tool": "rlm_factorio_craft",
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
            "tool": "rlm_factorio_build",
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
            "tool": "rlm_factorio_insert",
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
            "tool": "rlm_factorio_take",
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
            "tool": "rlm_factorio_research",
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
            ],
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
        result = appliance.control_post(row, "/observe/events", {}, timeout=10.0)
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


# ---- lifecycle -----------------------------------------------------------

def _acquire(args: dict) -> dict:
    """Bring the appliance up (idempotent) and return its session id.

    If a live session already owns a running container, adopt it (erroring on a
    game mismatch) rather than starting a second appliance. Otherwise mint a row
    on the fixed single-session coordinates, ``compose up --build``, wait for the
    engine, and mark it ready.
    """
    game = str(args.get("game") or "").strip()
    d = dao.FactorioDAO()

    for row in d.live_sessions():
        env = appliance.compose_env(row)
        if appliance.is_container_running(row["compose_project"] or appliance.PROJECT, env):
            if game and row["game"] and row["game"] != game:
                raise ValueError(
                    f"appliance already bound to game {row['game']!r}; "
                    f"release it before acquiring {game!r}"
                )
            log.info("acquire: adopting live session %s", row["session_id"])
            return {"session_id": row["session_id"], "adopted": True}

    row = d.create_session(
        game,
        container_name=appliance.CONTAINER,
        compose_project=appliance.PROJECT,
        control_port=appliance.CONTROL_PORT,
        game_port=appliance.GAME_PORT,
        rcon_port=appliance.RCON_PORT,
    )
    sid = row["session_id"]
    env = appliance.compose_env(row)
    try:
        log.info("acquire: bringing appliance up for session %s", sid)
        appliance.compose_up(appliance.PROJECT, env)
        st = appliance.wait_ready(row)
    except Exception as e:
        d.set_status(sid, "error")
        raise
    d.set_runtime(sid, status="ready", current_world=st.get("current_world"))
    return {"session_id": sid, "adopted": False}


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
    env = appliance.compose_env(row)
    appliance.compose_down(row["compose_project"] or appliance.PROJECT, env)
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


# ---- seats ---------------------------------------------------------------

# How long to wait for a started seat container to appear in-world as a
# connected player. Container start + map transfer + world load measured ~25s.
SEAT_JOIN_TIMEOUT = float(os.environ.get("AWM_FACTORIO_SEAT_JOIN_TIMEOUT", "240"))


def _require_seat(seat_id: str) -> dict:
    row = dao.FactorioDAO().get_seat(seat_id)
    if row is None:
        raise ValueError(f"unknown seat_id: {seat_id!r}")
    return row


def _lua(row: dict, code: str, *, timeout: float = 20.0) -> str:
    """Run Lua in the session's scenario context, returning its rcon output."""
    result = appliance.control_post(row, "/exec-lua", {"code": code},
                                    timeout=timeout)
    return str(result.get("output") or "").strip()


def _player_index(row: dict, player_name: str) -> int:
    """Resolve a seat's player to an index, or 0 while it is not yet connected.

    The name is asserted by the seat itself before the client starts, so this is
    a lookup rather than a diff of who appeared -- two seats joining at once
    cannot be confused for one another.
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
                f"seat container {container} exited before joining: "
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
        f"seat {seat['seat_id']} did not join within {timeout:.0f}s: "
        f"{appliance.seat_logs(container)[-800:]}"
    )


def _join(args: dict, as_: str | None = None) -> dict:
    """Allocate a seat on a session: start the client, wait for it in-world.

    The gateway threads the calling identity in as ``as_`` (a placed agent's
    placement id; absent for a call that never crossed an edge, i.e. the host's
    own CLI), and that becomes the seat's owner unless the caller named one.
    Ownership is recorded, never enforced: the LAN-party bargain is one shared
    force with no protection between players, and a shell has no identity to
    check against anyway. It exists so `seats` can say whose seat this is and
    so a reclaimed seat can be reported against the agent that lost it.
    """
    sid = args["session_id"]
    row = _require_session(sid)
    if row["status"] in ("stopped", "error"):
        raise ValueError(f"session {sid} is {row['status']}; acquire it first")

    d = dao.FactorioDAO()
    project = row["compose_project"] or appliance.PROJECT
    owner = str(args.get("owner") or as_ or "").strip()
    seat = d.create_seat(sid, owner=owner)
    seat_id = seat["seat_id"]
    container = appliance.seat_container_name(project, seat_id)
    d.set_seat(seat_id, container_name=container)

    try:
        appliance.seat_run(container, appliance.seat_network(project),
                           seat["player_name"],
                           output_dir=appliance.seat_output_dir(seat_id))
        index = _wait_seat_connected(row, seat, container)
        # Connected is not yet able to act: freeplay puts a newly created player
        # through an intro cutscene, during which it has no character. `spawn`
        # ends that and guarantees the seat is embodied before join returns.
        appliance.iface(row, "spawn", {"seat": seat["player_name"]},
                        timeout=60.0)
    except Exception:
        appliance.seat_stop(container)
        d.set_seat(seat_id, status="error")
        raise

    d.set_seat(seat_id, status="ready", player_index=index)
    d.touch_seat(seat_id)
    _fire(sid, "seat_joined", {"seat_id": seat_id,
                               "player_name": seat["player_name"],
                               "player_index": index})
    log.info("join: seat %s connected as %s (index %d) for owner %r",
             seat_id, seat["player_name"], index, owner)
    return {"seat_id": seat_id, "session_id": sid,
            "player_name": seat["player_name"], "player_index": index,
            "owner": owner}


def _leave(args: dict) -> dict:
    seat_id = args["seat_id"]
    seat = _require_seat(seat_id)
    if seat["container_name"]:
        appliance.seat_stop(seat["container_name"])
    dao.FactorioDAO().set_seat(seat_id, status="stopped")
    _fire(seat["session_id"], "seat_left", {"seat_id": seat_id,
                                            "player_name": seat["player_name"]})
    return {"released": True, "seat_id": seat_id}


def _seats(args: dict) -> dict:
    d = dao.FactorioDAO()
    sid = args.get("session_id")
    rows = d.list_seats(sid)
    out = []
    for seat in rows:
        enriched = dict(seat)
        if seat["status"] not in ("stopped", "error"):
            enriched["idle_s"] = round(_age_s(seat["last_seen_at"]), 1)
            enriched["container_running"] = appliance.seat_container_running(
                seat["container_name"])
            session = d.get_session(seat["session_id"])
            connected = False
            if session and session["status"] not in ("stopped", "error"):
                try:
                    connected = bool(_player_index(session, seat["player_name"]))
                except Exception:  # noqa: BLE001 — a dark appliance is not an error here
                    connected = False
            enriched["connected"] = connected
        out.append(enriched)
    return {"seats": out}


# ---- the reaper ----------------------------------------------------------
#
# A leaked seat is not a leaked browser tab. It is a whole simulated peer --
# a couple of gigabytes and a share of the box -- still standing in a world
# nobody is watching, and nothing outside this service can notice: an agent
# that dies mid-session takes its intent with it while its client plays on. So
# the service reclaims seats itself, on a sweep.
#
# It sweeps SEATS ONLY, and only seats it started -- their player names are
# minted here. A session is never touched, so the reaper cannot disconnect a
# human who joined from Steam, and cannot close the world under one.

REAP_POLL_S = float(os.environ.get("AWM_FACTORIO_REAP_POLL_S", "60"))

# How long a seat may go untouched before it is reclaimed. Deliberately
# generous, and 0 disables the check entirely: an agent that thinks for many
# minutes between moves is normal, and every verb that names a seat is a
# heartbeat (they all call touch_seat), so a seat only ages out when its owner
# has genuinely stopped playing.
SEAT_IDLE_S = float(os.environ.get("AWM_FACTORIO_SEAT_IDLE_S", "3600"))

# A join in flight owns no container yet, so it cannot be judged by one. Only a
# join that could not still be running counts as abandoned.
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
    """Stop a seat's container and mark the row stopped.

    Best-effort on the container: a row still claiming one that is already gone
    is exactly the state this exists to clear, so a failed stop must not leave
    the row live or abort the sweep.
    """
    if seat["container_name"]:
        try:
            appliance.seat_stop(seat["container_name"])
        except Exception:  # noqa: BLE001
            log.warning("reap: stopping %s failed",
                        seat["container_name"], exc_info=True)
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
    if not appliance.seat_container_running(seat["container_name"]):
        return "container gone"
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


def _stop_all_seats(reason: str) -> int:
    """Reclaim every live seat. Wired to SIGTERM in main(), not to atexit.

    `awm services stop` SIGTERMs us and Python skips exit hooks on that path, so
    teardown hung off atexit is teardown that never runs.

    The host appliance is deliberately left up: it holds the world, and both
    `acquire` and on_start are built to re-adopt it. Seats are not left up. A
    client is stateless -- a lost one is replaced by joining again, which is the
    rule on_start already follows -- and one that outlives the only process able
    to reclaim it is precisely the leak. A restart therefore costs its agents a
    re-join, which is the right side of that trade.
    """
    seats = dao.FactorioDAO().live_seats()
    for seat in seats:
        _reclaim(seat, reason)
    return len(seats)


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
    dao.FactorioDAO().set_runtime(sid, current_world=result.get("world"))
    _fire(sid, "world_loaded", {"world": result.get("world")})
    return result


# ---- act/perceive: addressed by seat -------------------------------------

def _resolve(args: dict, *, need_seat: bool = True) -> tuple[dict, dict | None]:
    """Resolve (session row, seat row) from ``seat_id`` and/or ``session_id``.

    A seat id alone is enough — the session hangs off it — so an agent that
    remembers one identifier can drive every verb. A session id alone resolves
    to its seat when it has exactly one; with several, ambiguity is an error
    rather than a guess, because guessing means acting as someone else's player.
    """
    d = dao.FactorioDAO()
    seat_id = str(args.get("seat_id") or "").strip()
    sid = str(args.get("session_id") or "").strip()
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
        live = d.live_seats(sid)
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


def _act(fn: str, *keys: str, need_seat: bool = True, lists: tuple = ()):
    """Handler factory: resolve the target, name the seat, call the mod.

    The seat's player name is what the mod resolves against, and it is never
    supplied by the caller — the service assigned it before the client started,
    so a verb cannot be aimed at a player the caller does not hold. ``lists``
    names result fields that must stay arrays even when empty.
    """
    def handler(args: dict) -> dict:
        row, seat = _resolve(args, need_seat=need_seat)
        body = {k: args[k] for k in keys if args.get(k) is not None}
        if seat is not None:
            body["seat"] = seat["player_name"]
            dao.FactorioDAO().touch_seat(seat["seat_id"])
        result = appliance.iface(row, fn, body)
        for key in lists:
            if key in result:
                result[key] = _as_list(result[key])
        return result
    return handler


def _observe(args: dict) -> dict:
    """Look at the world through one seat: {snapshot, screenshot}."""
    row, seat = _resolve(args)
    body: dict[str, Any] = {"seat": seat["player_name"]}
    if args.get("radius") is not None:
        body["radius"] = args["radius"]
    dao.FactorioDAO().touch_seat(seat["seat_id"])
    snapshot = appliance.iface(row, "observe", body)
    for key in _SNAPSHOT_LISTS:
        if key in snapshot:
            snapshot[key] = _as_list(snapshot[key])
    shot = None
    if args.get("screenshot"):
        shot = _screenshot({"seat_id": seat["seat_id"]})["path"]
    return {"snapshot": snapshot, "screenshot": shot}


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
    paused = args.get("paused")
    body = {} if paused is None else {"paused": bool(paused)}
    return appliance.control_post(row, "/pause", body)


# A Lua chunk travels to the engine as one RCON command. The cap is a guard
# against handing the socket a whole file by accident, not a measured engine
# limit.
EXEC_LUA_MAX = 64 * 1024


def _exec_lua(args: dict) -> dict:
    """Run Lua in the scenario context, from an inline string or a file.

    A real script does not survive shell quoting, so ``path`` is the form that
    matters for an agent alternating between MCP and a shell. The seat preamble
    shares the script's first line so a reported line number still matches the
    file the agent wrote.
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
    name = seat["player_name"] if seat else None
    preamble = (f'local seat, player = "{name}", game.players["{name}"]; '
                if name else "local seat, player = nil, nil; ")
    if seat is not None:
        dao.FactorioDAO().touch_seat(seat["seat_id"])
    result = appliance.control_post(row, "/exec-lua", {"code": preamble + code},
                                    timeout=120.0)
    return {**result, "seat": name}


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
    """Render the world through one seat and return a PATH to the PNG.

    A path, not a blob: the caller reads the file and actually sees it, and a
    shell script can pass it onward. The seat renders offscreen at whatever
    resolution is asked for, so its own window stays tiny and unmapped.
    """
    row, seat = _resolve(args)
    out_dir = appliance.seat_output_dir(seat["seat_id"])
    body = {"seat": seat["player_name"]}
    for key in ("x", "y", "width", "height", "zoom", "surface", "daytime",
                "show_entity_info", "file"):
        if args.get(key) is not None:
            body[key] = args[key]
    dao.FactorioDAO().touch_seat(seat["seat_id"])
    result = appliance.iface(row, "screenshot", body)
    path = str(out_dir / str(result["file"]))
    size = _wait_for_file(path)
    # How much world the frame covers, so pixels can be reasoned back to tiles:
    # a tile is 32 px at zoom 1.
    res, zoom = result["resolution"], float(result["zoom"])
    tiles = {"width": res["x"] / (32.0 * zoom), "height": res["y"] / (32.0 * zoom)}
    return {**result, "path": path, "bytes": size, "seat_id": seat["seat_id"],
            "tiles": tiles}


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


def _blueprint_stamp(args: dict) -> dict:
    """Stamp a blueprint at a position, as ghosts (a player's move) or built."""
    row, seat = _resolve(args)
    text = _blueprint_text(args)
    upload_id = seat["seat_id"]
    chunks = [text[i:i + BLUEPRINT_CHUNK]
              for i in range(0, len(text), BLUEPRINT_CHUNK)]
    for n, chunk in enumerate(chunks):
        appliance.iface(row, "bp_put",
                        {"id": upload_id, "data": chunk, "reset": n == 0})
    body = {"seat": seat["player_name"], "id": upload_id,
            "x": args["x"], "y": args["y"]}
    for key in ("direction", "surface", "build", "force_build", "clear"):
        if args.get(key) is not None:
            body[key] = args[key]
    dao.FactorioDAO().touch_seat(seat["seat_id"])
    result = appliance.iface(row, "blueprint_stamp", body, timeout=120.0)
    return {**result, "chunks": len(chunks), "characters": len(text)}


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
    result = appliance.iface(row, "blueprint_capture", body, timeout=120.0)
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
    return out


_move = _act("set_target", "x", "y")
_stop = _act("stop")
_teleport = _act("teleport", "x", "y", "surface")
_respawn = _act("spawn")
_mine = _act("mine", "x", "y", "name", "count")
_craft = _act("craft", "recipe", "count")
_build = _act("build", "name", "x", "y", "direction")
_insert = _act("insert", "x", "y", "name", "count", "target")
_take = _act("take", "x", "y", "name", "count", "target")
# Force-wide, so they need no seat -- but they take one when it is what the
# caller has in hand.
_research = _act("research", "name", need_seat=False)
_recipes = _act("recipes", "search", "limit", need_seat=False,
                lists=("recipes",))
_technologies = _act("technologies", "search", "only_unresearched", "limit",
                     need_seat=False, lists=("technologies",))


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "join": _join,
    "leave": _leave,
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
}


def _on_start() -> None:
    """Stand up the DB, then reconcile stale rows whose container is gone.

    A service respawn (the gateway can restart us) must not leave rows claiming
    'ready' for a container that no longer exists — mark those 'stopped' so the
    next acquire brings a fresh appliance up rather than adopting a ghost.
    """
    dao.init()
    d = dao.FactorioDAO()
    for row in d.live_sessions():
        env = appliance.compose_env(row)
        project = row["compose_project"] or appliance.PROJECT
        if not appliance.is_container_running(project, env):
            log.info("on_start: reconciling stale session %s -> stopped",
                     row["session_id"])
            d.set_status(row["session_id"], "stopped")
            for seat in d.live_seats(row["session_id"]):
                d.set_seat(seat["seat_id"], status="stopped")
    # A seat row whose container is gone is stopped, never adopted: the client
    # is stateless, so a lost one is replaced by joining again, not recovered.
    for seat in d.live_seats():
        if not appliance.seat_container_running(seat["container_name"]):
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

    # Seats are torn down on the SIGNAL, never on adapter.run() simply
    # returning. The adapter also stands down when the gateway rejects us as a
    # duplicate registration, and tearing down there would have a stillborn
    # second copy of the service stop the LIVE copy's seats.
    signalled = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signalled.set)
        except (NotImplementedError, ValueError, RuntimeError):
            log.debug("no signal handler for %s; seats reaped on idle only", sig)

    pump = asyncio.create_task(_events_pump(adapter))
    reaper = asyncio.create_task(_reaper())
    serve = asyncio.create_task(adapter.run())
    stop = asyncio.create_task(signalled.wait())
    ended = None
    try:
        await asyncio.wait({serve, stop}, return_when=asyncio.FIRST_COMPLETED)
        ended = serve if serve.done() else None
        if signalled.is_set():
            n = await asyncio.to_thread(_stop_all_seats, "service shutdown")
            log.info("shutdown: reclaimed %d seat(s)", n)
    finally:
        for task in (pump, reaper, stop, serve):
            task.cancel()
    if ended is not None:
        await ended  # re-raise whatever ended the adapter


if __name__ == "__main__":
    asyncio.run(main())
