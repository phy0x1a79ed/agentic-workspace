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

The perceive/extra-act verbs ``observe`` / ``exec_lua`` / ``pause`` are declared
in the contract now but answer with an honest stub: they need RCON, which the
POC probed and shelved. Wiring them is a later pass (add ``--rcon-port`` to the
supervisor + ``/exec-lua`` / ``/observe`` / ``/pause`` routes) — no manifest
change required, so the contract is stable. The ``factorio`` emitter is declared
now for the same reason.

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
                "Observe a session: returns {snapshot, screenshot?} of the live "
                "world. STUB — needs RCON+lua state queries (later pass); returns "
                "a placeholder."
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
        # ---- act: live control (RCON-backed; stubbed) ----
        {
            "name": "pause",
            "tool": "rlm_factorio_pause",
            "description": (
                "Pause/unpause the live world (game.tick_paused). STUB — needs "
                "RCON (later pass)."
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
                "Submit a lua script to the running world, returning its result. "
                "STUB — needs RCON /silent-command (later pass)."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "code", "type": "string", "required": True},
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

# Placeholder perception payload returned by ``observe`` until RCON lands.
_PLACEHOLDER_SNAPSHOT = {
    "tick": None,
    "paused": None,
    "players": [],
    "note": "placeholder snapshot; RCON+lua perception not wired yet",
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


# ---- act/perceive: RCON-backed (stubbed until the RCON pass) --------------

def _stub(verb: str, args: dict) -> dict:
    """Validate the session and return an honest 'not wired yet' ack."""
    _require_session(args["session_id"])
    echoed = {k: v for k, v in args.items() if k != "session_id"}
    return {"ok": True, "verb": verb, "session_id": args["session_id"],
            "args": echoed, "note": "stub; needs RCON (later pass)"}


def _observe(args: dict) -> dict:
    _require_session(args["session_id"])
    return {"snapshot": dict(_PLACEHOLDER_SNAPSHOT), "screenshot": None}


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "observe": _observe,
    "world_new": _world_new,
    "world_save": _world_save,
    "world_load": _world_load,
    "pause": lambda args: _stub("pause", args),
    "exec_lua": lambda args: _stub("exec_lua", args),
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
