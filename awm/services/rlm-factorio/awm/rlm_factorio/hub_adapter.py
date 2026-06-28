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
save/load because it lives in the mod's ``storage``. The ``factorio`` emitter is
declared but not fired yet — events land with a later pass (the mod already keeps
a bounded events ring buffer for it).

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
                "body is spawned) its position/health/inventory plus a capped "
                "nearby-entity summary. screenshot is null (not captured)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "radius", "type": "integer", "required": False},
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
                "Set the body's walk target and return immediately (one-shot). "
                "Poll observe to watch it converge then stop. Spawn first."
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
    ],
    "emitters": [
        {
            "topic": "factorio",
            "description": (
                "Fires on a realm-side world event. Payload {session_id, kind, "
                "data} where kind is e.g. 'world_loaded', 'world_saved' or "
                "'error' — projected as rlm.factorio.<kind>. Not fired yet in "
                "this slice (emitters land with the events pass)."
            ),
        },
    ],
    "sessions": [],
}

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
    result = appliance.control_post(row, "/new", {})
    dao.FactorioDAO().set_runtime(sid, status="ready", current_world=None)
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

def _world_new(args: dict) -> dict:
    row = _require_session(args["session_id"])
    body = {"seed": args["seed"]} if args.get("seed") is not None else {}
    result = appliance.control_post(row, "/new", body)
    dao.FactorioDAO().set_runtime(args["session_id"], current_world=None)
    return result


def _world_save(args: dict) -> dict:
    row = _require_session(args["session_id"])
    body = {"name": args["name"], "overwrite": bool(args.get("overwrite", False))}
    result = appliance.control_post(row, "/save", body)
    dao.FactorioDAO().set_runtime(args["session_id"], current_world=result.get("saved"))
    return result


def _world_load(args: dict) -> dict:
    row = _require_session(args["session_id"])
    result = appliance.control_post(row, "/load", {"name": args["name"]})
    dao.FactorioDAO().set_runtime(args["session_id"], current_world=result.get("world"))
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


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "observe": _observe,
    "world_new": _world_new,
    "world_save": _world_save,
    "world_load": _world_load,
    "pause": _pause,
    "exec_lua": _exec_lua,
    "body_spawn": _body_spawn,
    "body_move": _body_move,
    "body_stop": _body_stop,
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
    await ServiceAdapter(
        "rlm-factorio", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
