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
down (the saves volume survives).

The perceive/extra-act verbs ``observe`` / ``exec_lua`` / ``pause`` are LIVE over
RCON: the supervisor runs an in-container RCON client against the engine, and the
baked-in ``game-bot-control`` mod owns a script-controlled character body. The
body verbs ``body_spawn`` / ``body_move`` / ``body_stop`` drive that character
(one-shot move + poll ``observe`` to watch it converge); the body persists across
save/load because it lives in the mod's ``storage``.

The gameplay verbs ``body_mine`` / ``body_craft`` / ``body_build`` /
``body_insert`` / ``body_take`` act through the same mod interface (all
reach-gated — the body must walk within reach first). ``research`` unlocks a
technology via the cheat path (flagged ``cheated: true`` — script-crafted items
never fire trigger-tech counters, so there is no legit path yet). ``recipes`` /
``technologies`` are bounded catalog queries for planning what to craft next.

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
import threading
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.rlm_factorio import appliance, dao

log = logging.getLogger("awm.rlm_factorio.hub_adapter")

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
        # ---- perceive ----
        {
            "name": "observe",
            "tool": "rlm_factorio_observe",
            "description": (
                "Observe a session: returns {snapshot, screenshot} of the live "
                "world over RCON. The snapshot carries tick, paused, and (if a "
                "body is spawned) its position/health/reach/inventory, the "
                "crafting queue, and a capped nearby-entity summary (resources "
                "include amount). screenshot is null (not captured)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "radius", "type": "integer", "required": False},
            ],
        },
        {
            "name": "observe_events",
            "tool": "rlm_factorio_observe_events",
            "description": (
                "Drain the session's pending events: returns {events: [{kind, "
                "tick?, data}, ...]} accumulated since the last call, then "
                "clears them (consume-once inbox — world events like arrived/"
                "path_blocked/died/world_saved land here AND stream live on "
                "the 'factorio' emitter)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        # ---- act: world lifecycle (sacred saves) ----
        {
            "name": "world_new",
            "tool": "rlm_factorio_world_new",
            "description": (
                "Generate a fresh world and re-exec the engine on it. Discards "
                "unsaved live progress; touches no named save. Optional seed."
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
                "named save is read-only — loading can never advance it."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
            ],
            "timeout": 600.0,  # engine re-exec from the named save
        },
        # ---- act: live control (RCON-backed) ----
        {
            "name": "pause",
            "tool": "rlm_factorio_pause",
            "description": (
                "Pause/unpause the live world (game.tick_paused) over RCON. "
                "Returns {paused} with the resulting state."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "paused", "type": "boolean", "required": True},
            ],
        },
        {
            "name": "exec_lua",
            "tool": "rlm_factorio_exec_lua",
            "description": (
                "Run a lua script in the running world via RCON /silent-command. "
                "Returns {output} = whatever the script printed; to get a value "
                "back the script must call rcon.print(...)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "code", "type": "string", "required": True},
            ],
        },
        # ---- act: player body (RCON + game-bot-control mod) ----
        {
            "name": "body_spawn",
            "tool": "rlm_factorio_body_spawn",
            "description": (
                "Spawn the agent's character body in the live world (idempotent — "
                "returns the existing body if already spawned). Returns "
                "{position, surface}. The body persists across save/load/new."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "surface", "type": "string", "required": False},
                {"name": "x", "type": "number", "required": False},
                {"name": "y", "type": "number", "required": False},
            ],
        },
        {
            "name": "body_move",
            "tool": "rlm_factorio_body_move",
            "description": (
                "Walk the body to (x,y) via engine pathfinding (routes around "
                "obstacles) and return immediately. Poll observe or watch the "
                "emitter for 'arrived' / 'path_blocked'. Spawn first."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
            ],
        },
        {
            "name": "body_stop",
            "tool": "rlm_factorio_body_stop",
            "description": "Clear the body's walk target and halt it in place.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        # ---- act: gameplay (RCON + game-bot-control mod; all reach-gated) ----
        {
            "name": "body_mine",
            "tool": "rlm_factorio_body_mine",
            "description": (
                "Mine the resource/tree/rock nearest (x,y) into the body's "
                "inventory (must be within reach — walk there first). For ore "
                "patches, count = units to extract (default 1); returns "
                "{mined: {item: n}, remaining}."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "name", "type": "string", "required": False},
                {"name": "count", "type": "integer", "required": False},
            ],
        },
        {
            "name": "body_craft",
            "tool": "rlm_factorio_body_craft",
            "description": (
                "Queue a handcraft on the body (engine-native: consumes "
                "ingredients, ticks down, yields into inventory). Returns "
                "{queued}; watch progress via observe's crafting list."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "recipe", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
            ],
        },
        {
            "name": "body_build",
            "tool": "rlm_factorio_body_build",
            "description": (
                "Place an item from the body's inventory as an entity at (x,y) "
                "(within reach, collision-checked; the item is consumed only on "
                "success). direction: north/east/south/west (default north)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "direction", "type": "string", "required": False},
            ],
        },
        {
            "name": "body_insert",
            "tool": "rlm_factorio_body_insert",
            "description": (
                "Move items from the body's inventory into the entity at (x,y) "
                "(within reach). The engine routes to the right slot — coal "
                "into a furnace lands in fuel, ore in the smelt slot. Optional "
                "target narrows by entity name. Returns {inserted, target}."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "x", "type": "number", "required": True},
                {"name": "y", "type": "number", "required": True},
                {"name": "name", "type": "string", "required": True},
                {"name": "count", "type": "integer", "required": False},
                {"name": "target", "type": "string", "required": False},
            ],
        },
        {
            "name": "body_take",
            "tool": "rlm_factorio_body_take",
            "description": (
                "Take items from the entity at (x,y) into the body's inventory "
                "(within reach; e.g. plates out of a furnace). Default count = "
                "all available. Returns {taken, from}."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
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
                "Unlock a technology directly (CHEAT path — script-crafted "
                "items never fire trigger-tech counters and no lab chain "
                "exists, so this flips the flag and marks the result "
                "cheated:true). Prerequisites are NOT auto-unlocked."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "name", "type": "string", "required": True},
            ],
        },
        # ---- perceive: catalog queries ----
        {
            "name": "recipes",
            "tool": "rlm_factorio_recipes",
            "description": (
                "List the force's unlocked recipes ({name, ingredients, "
                "products}). Bounded: pass search (substring) and/or limit "
                "(default 40); truncated:true means narrow the search."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
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
                {"name": "session_id", "type": "string", "required": True},
                {"name": "search", "type": "string", "required": False},
                {"name": "only_unresearched", "type": "boolean", "required": False},
                {"name": "limit", "type": "integer", "required": False},
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
                "verbs) plus the mod's in-world events ('spawned', 'arrived', "
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
    sid = args["session_id"]
    row = _require_session(sid)
    env = appliance.compose_env(row)
    appliance.compose_down(row["compose_project"] or appliance.PROJECT, env)
    dao.FactorioDAO().set_status(sid, "stopped")
    return {"released": True, "session_id": sid}


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


# ---- act/perceive: RCON-backed -------------------------------------------

def _observe(args: dict) -> dict:
    """Snapshot the live world over RCON (real perception, replacing the stub).

    The supervisor's /observe route asks the game-bot-control mod for the world/
    body state; we return it as {snapshot, screenshot} (screenshot is always None
    — not captured)."""
    row = _require_session(args["session_id"])
    body = {"radius": args["radius"]} if args.get("radius") is not None else {}
    snapshot = appliance.control_post(row, "/observe", body)
    return {"snapshot": snapshot, "screenshot": None}


def _observe_events(args: dict) -> dict:
    sid = args["session_id"]
    row = _require_session(sid)
    _collect_and_fire(row)          # freshness: drain the world buffer now too
    with _EVENTS_LOCK:
        events = _EVENT_BUF.pop(sid, [])
    return {"events": events}


def _pause(args: dict) -> dict:
    row = _require_session(args["session_id"])
    return appliance.control_post(row, "/pause", {"paused": bool(args["paused"])})


def _exec_lua(args: dict) -> dict:
    row = _require_session(args["session_id"])
    return appliance.control_post(row, "/exec-lua", {"code": args["code"]})


def _body_spawn(args: dict) -> dict:
    row = _require_session(args["session_id"])
    body = {k: args[k] for k in ("surface", "x", "y") if args.get(k) is not None}
    return appliance.control_post(row, "/body/spawn", body)


def _body_move(args: dict) -> dict:
    row = _require_session(args["session_id"])
    return appliance.control_post(row, "/body/move", {"x": args["x"], "y": args["y"]})


def _body_stop(args: dict) -> dict:
    row = _require_session(args["session_id"])
    return appliance.control_post(row, "/body/stop", {})


def _passthrough(path: str, *keys: str):
    """Handler factory for the gameplay/catalog verbs: forward the named args
    (when present) to a supervisor route; validation is mod-side."""
    def handler(args: dict) -> dict:
        row = _require_session(args["session_id"])
        body = {k: args[k] for k in keys if args.get(k) is not None}
        return appliance.control_post(row, path, body)
    return handler


_body_mine = _passthrough("/body/mine", "x", "y", "name", "count")
_body_craft = _passthrough("/body/craft", "recipe", "count")
_body_build = _passthrough("/body/build", "name", "x", "y", "direction")
_body_insert = _passthrough("/body/insert", "x", "y", "name", "count", "target")
_body_take = _passthrough("/body/take", "x", "y", "name", "count", "target")
_research = _passthrough("/research", "name")
_recipes = _passthrough("/observe/recipes", "search", "limit")
_technologies = _passthrough(
    "/observe/technologies", "search", "only_unresearched", "limit")


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "observe": _observe,
    "observe_events": _observe_events,
    "world_new": _world_new,
    "world_save": _world_save,
    "world_load": _world_load,
    "pause": _pause,
    "exec_lua": _exec_lua,
    "body_spawn": _body_spawn,
    "body_move": _body_move,
    "body_stop": _body_stop,
    "body_mine": _body_mine,
    "body_craft": _body_craft,
    "body_build": _body_build,
    "body_insert": _body_insert,
    "body_take": _body_take,
    "research": _research,
    "recipes": _recipes,
    "technologies": _technologies,
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
    _LOOP = asyncio.get_running_loop()
    pump = asyncio.create_task(_events_pump(adapter))
    try:
        await adapter.run()
    finally:
        pump.cancel()


if __name__ == "__main__":
    asyncio.run(main())
